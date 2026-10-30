import pytest
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from integration_harness.client import ApiError
from integration_harness.orchestrator import (
    CN_TZ,
    Orchestrator,
    _AuthVerificationFailed,
    _NoUnfinishedLearning,
    _TaskCompletedAwaitResume,
)


class _DummyLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


def test_heartbeat_needs_restart_when_status_message_is_not_success():
    response = {
        "status": {"code": 0, "message": "学习过程中不允许打开多个窗口"},
        "data": {},
    }

    assert Orchestrator._heartbeat_needs_restart(response) is True


def test_heartbeat_needs_restart_when_new_course_will_close_page():
    response = {
        "status": {
            "code": 0,
            "message": "您正在学习新课程《会计人员职业道德规范》，当前视频页面将要关闭。",
        },
        "data": {},
    }

    assert Orchestrator._heartbeat_needs_restart(response) is True


def test_heartbeat_does_not_restart_on_success():
    response = {
        "status": {"code": 0, "message": "SUCCESS"},
        "data": {"code": 1},
    }

    assert Orchestrator._heartbeat_needs_restart(response) is False


def test_heartbeat_does_not_restart_when_status_message_is_missing():
    response = {"data": {"code": -20, "message": "心跳间隔过短"}}

    assert Orchestrator._heartbeat_needs_restart(response) is False


def test_update_interval_too_short_message_is_detected():
    assert Orchestrator._is_update_interval_too_short("更新间隔太短，请稍后再试") is True
    assert Orchestrator._is_update_interval_too_short("SUCCESS") is False


def test_is_daily_limit_message_is_detected():
    assert (
        Orchestrator._is_daily_limit_message(
            "您今天学习时长已经超过8小时，不继续累计时长！"
        )
        is True
    )
    assert Orchestrator._is_daily_limit_message("SUCCESS") is False


def test_coerce_data_handles_string_and_missing_data():
    assert Orchestrator._coerce_data({"data": "unexpected"}) == {}
    assert Orchestrator._coerce_data({"data": {}}) == {}
    assert Orchestrator._coerce_data({"data": {"code": -20}}) == {"code": -20}


def test_coerce_data_logs_warning_for_string_data(caplog):
    with caplog.at_level(logging.WARNING, logger="integration_harness.orchestrator"):
        Orchestrator._coerce_data({"data": "unexpected"})

    assert "响应 data 不是对象" in caplog.text


def test_select_course_and_doc_skips_when_all_docs_are_finished():
    course_list = {
        "data": {
            "list": [
                {
                    "courseInfo": {
                        "body": [
                            {
                                "child": [
                                    {"docId": "first-doc"},
                                ]
                            }
                        ]
                    },
                    "mycourseInfo": {
                        "learned": {"first-doc": {"status": "finish"}},
                        "lastDocId": "last-doc",
                    },
                }
            ]
        }
    }

    with pytest.raises(_NoUnfinishedLearning, match="课程小节已全部完成"):
        Orchestrator._select_course_and_doc(None, course_list)


def test_select_course_and_doc_returns_first_unfinished_doc():
    course_list = {
        "data": {
            "list": [
                {
                    "courseInfo": {
                        "body": [
                            {
                                "child": [
                                    {"docId": "finished-doc"},
                                    {"docId": "unfinished-doc"},
                                ]
                            }
                        ]
                    },
                    "mycourseInfo": {
                        "learned": {"finished-doc": {"status": "finish"}},
                    },
                }
            ]
        }
    }

    course_info, doc_id = Orchestrator._select_course_and_doc(None, course_list)

    assert doc_id == "unfinished-doc"


def test_select_course_and_doc_allows_finished_doc_when_replay_true():
    course_list = {
        "data": {
            "list": [
                {
                    "courseInfo": {
                        "body": [{"child": [{"docId": "finished-doc"}]}]
                    },
                    "mycourseInfo": {
                        "learnedStatus": 2,
                        "learnedProgress": 100,
                        "learned": {"finished-doc": {"status": "finish"}},
                    },
                }
            ]
        }
    }
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.replay = True

    course_info, doc_id = orchestrator._select_course_and_doc(course_list)

    assert doc_id == "finished-doc"


def test_select_course_and_doc_skips_finished_course_marker():
    course_list = {
        "data": {
            "list": [
                {
                    "courseInfo": {
                        "body": [{"child": [{"docId": "doc-1"}]}]
                    },
                    "mycourseInfo": {
                        "learnedStatus": 2,
                        "learnedProgress": 100,
                        "learned": {},
                    },
                },
                {
                    "courseInfo": {
                        "body": [{"child": [{"docId": "doc-2"}]}]
                    },
                    "mycourseInfo": {
                        "learnedStatus": 1,
                        "learnedProgress": 20,
                        "learned": {},
                    },
                },
            ]
        }
    }

    course_info, doc_id = Orchestrator._select_course_and_doc(None, course_list)

    assert doc_id == "doc-2"


def test_course_is_finished_recognizes_status_and_progress():
    assert Orchestrator._course_is_finished({"learnedStatus": 2}) is True
    assert Orchestrator._course_is_finished({"learnedProgress": 100}) is True
    assert Orchestrator._course_is_finished({"learnedStatus": 1}) is False


def test_next_unfinished_course_id_uses_learned_progress():
    detail = {
        "data": {
            "mytaskInfo": {
                "learned": {
                    "finished-course": {"lp": 100},
                    "unfinished-course": {"lp": 27},
                }
            }
        }
    }

    assert (
        Orchestrator._next_unfinished_course_id(None, detail)
        == "unfinished-course"
    )


def test_next_unfinished_course_id_excludes_skipped_courses():
    detail = {
        "data": {
            "mytaskInfo": {
                "learned": {
                    "course-a": {"lp": 20},
                    "course-b": {"lp": 30},
                }
            }
        }
    }

    assert (
        Orchestrator._next_unfinished_course_id(
            None,
            detail,
            exclude={"course-a"},
        )
        == "course-b"
    )


def test_find_course_group_returns_matching_group():
    detail = {
        "data": {
            "taskInfo": {
                "courseConfig": [
                    {
                        "id": "config-1",
                        "courseGroup": [
                            {
                                "id": "group-1",
                                "courseIds": ["course-a", "course-b"],
                            }
                        ],
                    }
                ]
            }
        }
    }

    assert Orchestrator._find_course_group(None, detail, "course-b") == (
        "config-1",
        ["course-b"],
    )


def test_find_course_group_returns_matching_flat_course_ids():
    detail = {
        "data": {
            "taskInfo": {
                "courseConfig": [
                    {
                        "_id": "config-1",
                        "courseIds": ["course-a", "course-b"],
                    }
                ]
            }
        }
    }

    assert Orchestrator._find_course_group(None, detail, "course-b") == (
        "config-1",
        ["course-b"],
    )


def test_default_course_target_uses_mycourse_course_list_first():
    orchestrator = Orchestrator.__new__(Orchestrator)
    detail = {
        "data": {
            "mytaskInfo": {
                "courseList": [
                    {"id": "selected-list", "courseIds": ["selected-course"]}
                ]
            },
            "taskInfo": {
                "courseConfig": [
                    {"id": "config-list", "courseIds": ["unselected-course"]}
                ]
            },
        }
    }

    assert orchestrator._default_course_target(detail) == (
        "selected-list",
        "selected-course",
    )


def test_default_course_target_falls_back_to_course_group():
    orchestrator = Orchestrator.__new__(Orchestrator)
    detail = {
        "data": {
            "mytaskInfo": {"courseList": []},
            "taskInfo": {
                "courseConfig": [
                    {
                        "id": "config-list",
                        "courseGroup": [
                            {"courseIds": ["group-course"]},
                        ],
                    }
                ]
            },
        }
    }

    assert orchestrator._default_course_target(detail) == (
        "config-list",
        "group-course",
    )


def test_default_course_target_skips_finished_course():
    orchestrator = Orchestrator.__new__(Orchestrator)
    detail = {
        "data": {
            "mytaskInfo": {
                "courseList": [],
                "learned": {
                    "finished-course": {"lp": 100},
                    "unfinished-course": {"lp": 50},
                },
            },
            "taskInfo": {
                "courseConfig": [
                    {
                        "id": "config-list",
                        "courseIds": ["finished-course", "unfinished-course"],
                    }
                ]
            },
        }
    }

    assert orchestrator._default_course_target(detail) == (
        "config-list",
        "unfinished-course",
    )


def test_find_target_seconds_prefers_learned_total_time():
    course_detail = {
        "data": {
            "docInfo": {"duration": 999},
            "mycourseInfo": {
                "learned": {
                    "doc-id": {"totalTime": 1234},
                }
            },
        }
    }

    assert Orchestrator._find_target_seconds(None, course_detail, "doc-id") == 1234


def test_find_target_seconds_falls_back_to_doc_duration():
    course_detail = {
        "data": {
            "docInfo": {"duration": 888},
            "mycourseInfo": {"learned": {}},
        }
    }

    assert Orchestrator._find_target_seconds(None, course_detail, "doc-id") == 888


def test_find_learned_seconds_prefers_learned_time():
    course_detail = {
        "data": {
            "mycourseInfo": {
                "learned": {
                    "doc-id": {"learnedTime": 2561, "lastTime": 0},
                }
            }
        }
    }

    assert Orchestrator._find_learned_seconds(None, course_detail, "doc-id") == 2561


def test_find_learned_seconds_falls_back_to_last_time_and_zero():
    assert (
        Orchestrator._find_learned_seconds(
            None,
            {"data": {"mycourseInfo": {"learned": {"doc-id": {"lastTime": 90}}}}},
            "doc-id",
        )
        == 90
    )
    assert (
        Orchestrator._find_learned_seconds(
            None,
            {"data": {"mycourseInfo": {"learned": {}}}},
            "doc-id",
        )
        == 0
    )


def test_resolve_current_doc_id_prefers_doc_info():
    course_detail = {
        "data": {
            "docInfo": {"id": "actual-doc"},
            "mycourseInfo": {"lastDocId": "fallback-doc"},
        }
    }

    assert Orchestrator._resolve_current_doc_id(None, course_detail) == "actual-doc"


def test_resolve_current_doc_id_falls_back_to_last_doc_id():
    course_detail = {
        "data": {
            "mycourseInfo": {"lastDocId": "fallback-doc"},
        }
    }

    assert Orchestrator._resolve_current_doc_id(None, course_detail) == "fallback-doc"


def test_qr_expires_at_uses_check_end_time():
    created_at = datetime(2026, 9, 17, 1, 0, 0, tzinfo=timezone.utc)
    orchestrator = Orchestrator.__new__(Orchestrator)

    expires_at = orchestrator._qr_expires_at(
        {"checkEndTime": 1789454606189},
        created_at,
    )

    assert expires_at.timestamp() == 1789454606.189


def test_qr_expires_at_ignores_zero_check_end_time():
    created_at = datetime(2026, 9, 17, 10, 0, 0, tzinfo=timezone.utc)
    orchestrator = Orchestrator.__new__(Orchestrator)

    expires_at = orchestrator._qr_expires_at(
        {"checkEndTime": 0, "validity": 300},
        created_at,
    )

    assert expires_at.timestamp() == (created_at + timedelta(seconds=300)).timestamp()


def test_learned_complete_detection_from_response_data():
    assert Orchestrator._is_learned_complete({"learnedStatus": 2}) is True
    assert Orchestrator._is_learned_complete(
        {"mycourseInfo": {"learnedStatus": 2}}
    ) is True
    assert Orchestrator._is_learned_complete({"learnedStatus": 1}) is False


def test_get_task_cert_synced_matches_task_id():
    runner = _poll_runner()
    runner.id_card = "id-1"
    runner.name = "张三"
    runner._post_json = lambda path, payload, event_type: {
        "data": [
            {"mytask": {"_id": "task-1", "extra": {"synced": 1}}},
            {"mytask": {"_id": "task-2", "extra": {"synced": 0}}},
            {"mytask": {"_id": "task-4", "extra": {}}},
        ]
    }

    assert runner._get_task_cert_synced("task-1") == 1
    assert runner._get_task_cert_synced("task-2") == 0
    assert runner._get_task_cert_synced("task-3") == 0
    assert runner._get_task_cert_synced("task-4") == 0


def test_get_task_cert_synced_and_watch_target_from_top_level_item():
    runner = _poll_runner()
    runner.id_card = "id-1"
    runner.name = "张三"
    runner._post_json = lambda path, payload, event_type: {
        "data": [
            {
                "_id": "6a7d1eb50a8d2ffed6ded67c",
                "taskId": "6a3c8f355c97aa9d1eb5d8e4",
                "courseList": [
                    {
                        "id": "6a3c8f355c97aa9d1eb5d8ee",
                        "courseIds": ["6a263c6734ccc04a8da48072"],
                    }
                ],
                "learnedStatus": 2,
                "extra": {
                    "PUB_02": {"finishSupervision": False},
                },
            }
        ]
    }

    assert runner._get_task_cert_synced("6a3c8f355c97aa9d1eb5d8e4") == 0
    assert runner._cert_watch_target("6a3c8f355c97aa9d1eb5d8e4") == (
        "6a3c8f355c97aa9d1eb5d8ee",
        "6a263c6734ccc04a8da48072",
    )


def _poll_runner():
    runner = Orchestrator.__new__(Orchestrator)
    runner.config = SimpleNamespace(verify_poll_interval_seconds=5)
    runner.logger = _DummyLogger()
    runner.notifier = SimpleNamespace(send_markdown=lambda content: True)
    runner.id_card = "id-1"
    runner.name = "张三"
    return runner


def test_poll_until_verified_returns_on_success():
    runner = _poll_runner()
    runner._gd_call = lambda method, **kwargs: {"data": {"verifyResult": "1"}}
    qr_state = SimpleNamespace()

    runner._poll_until_verified(
        mycourse_id="course-1",
        study_token="token-1",
        point_code="point-1",
        qr_state=qr_state,
    )

    assert qr_state.verify_status == "VERIFIED"


def test_poll_until_verified_stops_and_notifies_on_timeout(monkeypatch):
    runner = _poll_runner()
    runner._gd_call = lambda method, **kwargs: {"data": {"verifyResult": "0"}}
    notified = []
    runner._notify_verification_failed = lambda **kwargs: notified.append(kwargs)
    stopped = []
    runner._mark_account_stopped = lambda: stopped.append(True)
    monkeypatch.setattr(
        "integration_harness.orchestrator.time.monotonic",
        iter([0, 2000]).__next__,
    )
    monkeypatch.setattr(
        "integration_harness.orchestrator.time.sleep",
        lambda seconds: None,
    )
    qr_state = SimpleNamespace()

    with pytest.raises(_AuthVerificationFailed):
        runner._poll_until_verified(
            mycourse_id="course-1",
            study_token="token-1",
            point_code="point-1",
            qr_state=qr_state,
        )

    assert notified[0]["point_code"] == "point-1"
    assert stopped == [True]


class _FakeOssUploader:
    def __init__(self) -> None:
        self.bytes = []
        self.texts = []

    def upload_bytes(self, key, content):
        self.bytes.append((key, content))
        return type("Result", (), {"key": key, "success": True, "error": None})()

    def upload_text(self, key, content):
        self.texts.append((key, content))
        return type("Result", (), {"key": key, "success": True, "error": None})()


def test_upload_qr_artifacts_writes_png_and_required_json():
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.id_card = "id-1"
    orchestrator.logger = _DummyLogger()
    orchestrator.oss_uploader = _FakeOssUploader()

    orchestrator._upload_qr_artifacts(
        point_code="point-1",
        qrcode_url="https://example.com/verify",
        trigger_data={},
    )

    assert len(orchestrator.oss_uploader.bytes) == 1
    png_key, png_bytes = orchestrator.oss_uploader.bytes[0]
    assert png_key == "hxacc/account/id-1/point-1.png"
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"

    assert len(orchestrator.oss_uploader.texts) == 1
    json_key, json_text = orchestrator.oss_uploader.texts[0]
    assert json_key == "hxacc/account/id-1/point-1.json"
    payload = json.loads(json_text)
    assert payload["task_id"] == "point-1"
    assert payload["created_at"].endswith("+08:00")
    assert payload["expires_at"].endswith("+08:00")
    assert payload["sha256"] == hashlib.sha256(png_bytes).hexdigest()


def test_mark_account_stopped_writes_stop_key():
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.id_card = "id-1"
    orchestrator.logger = _DummyLogger()
    orchestrator.oss_uploader = _FakeOssUploader()

    orchestrator._mark_account_stopped()

    assert orchestrator.oss_uploader.texts == [("hxacc/account/id-1/stop", "")]


def test_mark_daily_limit_writes_marker():
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.id_card = "id-1"
    orchestrator.logger = _DummyLogger()
    orchestrator.oss_uploader = _FakeOssUploader()

    orchestrator._mark_daily_limit()

    assert orchestrator.oss_uploader.texts == [
        (
            "hxacc/account/id-1/daily_limit",
            datetime.now(CN_TZ).strftime("%Y-%m-%d"),
        )
    ]


def test_on_task_completed_stops_and_notifies_when_not_auto_next():
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.auto_next_course = False
    orchestrator.config = SimpleNamespace(
        app_id="app-1",
        token_prefix="token-prefix",
        device_id="device-1",
    )
    orchestrator.session_id = "session-1"
    orchestrator.id_card = "id-1"
    orchestrator.name = "张三"
    orchestrator.logger = _DummyLogger()
    orchestrator.oss_uploader = _FakeOssUploader()
    sent = []
    orchestrator.notifier = SimpleNamespace(
        send_task_completed=lambda **kwargs: sent.append(kwargs) or True
    )
    orchestrator._task_completed_courses = lambda task_id, detail: ["课程A"]
    orchestrator._next_task_target = lambda task_id: "专业课：课程B"

    with pytest.raises(_TaskCompletedAwaitResume):
        orchestrator._on_task_completed(
            {"taskInfo": {"id": "task-1", "title": "公需课"}},
            {},
        )

    assert sent[0]["completed_courses"] == ["课程A"]
    assert sent[0]["next_target"] == "专业课：课程B"
    assert orchestrator.oss_uploader.texts == [
        ("hxacc/account/id-1/stop", "task_completed")
    ]


def test_next_task_target_skips_current_and_returns_next_unfinished():
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.tasks = [
        {"taskInfo": {"id": "task-1", "title": "公需课"}},
        {"taskInfo": {"id": "task-2", "title": "专业课"}},
    ]
    orchestrator._get_task_detail = lambda task_id: {
        "data": {
            "mytaskInfo": {
                "synced": 0,
                "learned": {"course-x": {"lp": 50}},
            }
        }
    }
    orchestrator._course_titles = lambda task_id, detail, course_ids: {
        "course-x": "课程X"
    }

    assert orchestrator._next_task_target("task-1") == "专业课：课程X"


def test_signal_relogin_writes_signal(monkeypatch):
    monkeypatch.setattr("integration_harness.orchestrator.time.sleep", lambda s: None)
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.id_card = "id-1"
    orchestrator.logger = _DummyLogger()
    orchestrator.oss_uploader = _FakeOssUploader()

    orchestrator._signal_relogin()

    assert orchestrator.oss_uploader.texts == [
        ("hxacc/account/id-1/relogin", '{"attempts": 0}'),
        ("hxacc/account/id-1/stop", ""),
    ]


def test_signal_relogin_deletes_local_account_file(tmp_path, monkeypatch):
    monkeypatch.setattr("integration_harness.orchestrator.time.sleep", lambda s: None)
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.id_card = "id-1"
    orchestrator.logger = _DummyLogger()
    orchestrator.oss_uploader = _FakeOssUploader()
    orchestrator.config = SimpleNamespace(account_dir=tmp_path)

    account_path = tmp_path / "id-1.account"
    account_path.write_text("{}", encoding="utf-8")

    orchestrator._signal_relogin()

    assert not account_path.exists()


def test_post_json_signals_relogin_on_401():
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.logger = _DummyLogger()
    orchestrator.client = SimpleNamespace(
        post_learn_json=lambda path, payload: (_ for _ in ()).throw(
            ApiError("unauthorized", status_code=401)
        )
    )
    signaled: list[bool] = []
    orchestrator._signal_relogin = lambda exc, context: signaled.append(True)

    with pytest.raises(ApiError):
        orchestrator._post_json(
            "/api/mycourse/postUpdateTimeGuangdong",
            {},
            event_type="postUpdateTimeGuangdong",
        )

    assert signaled == [True]


def test_select_course_and_doc_filters_by_course_id():
    course_list = {
        "data": {
            "list": [
                {
                    "courseInfo": {
                        "id": "course-a",
                        "body": [{"child": [{"docId": "doc-a"}]}],
                    },
                    "mycourseInfo": {"learnedStatus": 1, "learned": {}},
                },
                {
                    "courseInfo": {
                        "id": "course-b",
                        "body": [{"child": [{"docId": "doc-b"}]}],
                    },
                    "mycourseInfo": {"learnedStatus": 1, "learned": {}},
                },
            ]
        }
    }

    course_info, doc_id = Orchestrator._select_course_and_doc(
        None,
        course_list,
        course_id="course-b",
    )

    assert course_info["id"] == "course-b"
    assert doc_id == "doc-b"


def test_find_random_prefers_doc_info_over_learned_state():
    course_detail = {
        "data": {
            "docInfo": {"random": "doc-info-random"},
            "mycourseInfo": {
                "learned": {
                    "doc-id": {"random": "learned-random"},
                }
            },
        }
    }

    assert Orchestrator._find_random(None, course_detail, "doc-id") == "doc-info-random"


def test_select_tasks_returns_every_item_returned_by_api():
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.config = type("Config", (), {"app_id": "app-id"})()
    orchestrator._post_json = lambda *args, **kwargs: {
        "data": {
            "list": [
                {"taskInfo": {"id": "task-1", "title": "公需课", "status": 1}},
                {"taskInfo": {"id": "task-2", "title": "专业课", "status": 1}},
            ]
        }
    }

    tasks = orchestrator._select_tasks()

    assert [item["taskInfo"]["id"] for item in tasks] == ["task-1", "task-2"]


def test_select_tasks_raises_when_api_returns_empty_list():
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.config = type("Config", (), {"app_id": "app-id"})()
    orchestrator._post_json = lambda *args, **kwargs: {"data": {"list": []}}

    with pytest.raises(RuntimeError, match="接口未返回任何任务"):
        orchestrator._select_tasks()


def test_run_processes_all_tasks_sequentially():
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.logger = _DummyLogger()
    orchestrator._set_state = lambda *args, **kwargs: None
    orchestrator._select_tasks = lambda: [
        {"taskInfo": {"id": "task-1", "title": "公需课"}},
        {"taskInfo": {"id": "task-2", "title": "专业课"}},
    ]

    processed = []

    def fake_run_task_with_restarts(task, *, index, total):
        processed.append(task["taskInfo"]["id"])
        return True

    orchestrator._run_task_with_restarts = fake_run_task_with_restarts

    orchestrator.run()

    assert processed == ["task-1", "task-2"]

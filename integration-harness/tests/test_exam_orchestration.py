import logging
from types import SimpleNamespace

import pytest

from integration_harness.client import ApiError
from integration_harness.exam import ExamBlocked, ExamDeferred, ExamPending
from integration_harness.orchestrator import Orchestrator


def runner():
    value = Orchestrator.__new__(Orchestrator)
    value.logger = logging.getLogger("test.exam")
    value._set_state = lambda state: None
    value.config = SimpleNamespace(exam_task_map_json="{}", runs_dir=None, exam_base_url="https://example.test")
    return value


@pytest.mark.parametrize("exam_status", [None, 1, "1", 2])
def test_non_exam_task_keeps_learning_flow(exam_status):
    value = runner()
    assert value._run_exam_if_required("task", {"data": {"mytaskInfo": {"examStatus": exam_status}, "taskInfo": {"examId": "exam"}}}) is False


def configure_loop(value, *, synced, exam_status=1):
    calls = []
    detail = {"data": {"mytaskInfo": {"examStatus": exam_status}}}
    value._get_task_detail = lambda task_id: calls.append("refresh") or detail
    value._run_exam_if_required = lambda task_id, detail: calls.append("exam") or bool(exam_status)
    completion = iter(synced)
    value._is_task_completed = lambda task_id: calls.append("synced") or next(completion)
    value._next_unfinished_course_id = lambda detail, exclude: "course"
    value._find_course_group = lambda detail, course_id: ("group", [course_id])
    value._get_course_list = lambda *args: {}
    value._select_course_and_doc = lambda *args, **kw: ({"title": "course"}, "doc")
    value._watch_doc = lambda **kw: calls.append("watch")
    value._on_task_completed = lambda *args: calls.append("complete")
    return calls


def test_passed_exam_unsynced_continues_watching():
    value = runner()
    calls = configure_loop(value, synced=[False, True])
    value._run_task({"taskInfo": {"id": "task"}})
    assert calls == ["refresh", "exam", "refresh", "synced", "watch",
                     "refresh", "exam", "refresh", "synced", "complete"]


def test_exam_branch_is_checked_before_synced_completion():
    value = runner()
    calls = configure_loop(value, synced=[True])
    value._run_task({"taskInfo": {"id": "task"}})
    assert calls == ["refresh", "exam", "refresh", "synced", "complete"]


def test_exam_failure_cannot_mark_task_completed():
    value = runner()
    calls = configure_loop(value, synced=[True])

    def pending(*args):
        raise ExamPending("等待成绩")

    value._run_exam_if_required = pending
    with pytest.raises(ExamPending):
        value._run_task({"taskInfo": {"id": "task"}})
    assert "complete" not in calls
    assert "watch" not in calls


def test_missing_course_with_synced_zero_is_not_success():
    value = runner()
    calls = configure_loop(value, synced=[False], exam_status=0)
    value._next_unfinished_course_id = lambda *args, **kw: ""
    value._cert_watch_target = lambda task: ("", "")
    value._default_course_target = lambda detail: ("", "")
    with pytest.raises(RuntimeError, match="保留未完成"):
        value._run_task({"taskInfo": {"id": "task"}})
    assert "complete" not in calls


def test_missing_exam_id_keeps_learning():
    value = runner()
    assert value._run_exam_if_required("task", {"data": {"mytaskInfo": {"examStatus": 0}}}) is False


def test_next_task_with_pending_exam_is_not_skipped_for_lack_of_courses():
    value = runner()
    value.tasks = [{"taskInfo": {"id": "current"}}, {"taskInfo": {"id": "next", "title": "公需课"}}]
    value._get_task_detail = lambda _: {"data": {"mytaskInfo": {"examStatus": 0, "synced": 0}, "taskInfo": {"examId": "exam"}}}
    assert value._next_task_target("current") == "公需课：检查正式考试状态"
    value._get_task_detail = lambda _: {"data": {"mytaskInfo": {"examStatus": 0, "synced": 1}, "taskInfo": {"examId": "exam"}}}
    assert value._next_task_target("current") is None


@pytest.mark.parametrize("error", [ApiError("expired", status_code=401), ExamBlocked("次数用尽")])
def test_exam_errors_use_existing_relogin_and_stop_paths(monkeypatch, error):
    value = runner()
    value.id_card = "account"
    value.name = "测试"
    value.oss_uploader = object()
    value.client = SimpleNamespace(post_exam_json=lambda *args: None)
    value.notifier = SimpleNamespace(send_markdown=lambda _: True)
    signals = []
    value._signal_relogin = lambda: signals.append("relogin")
    value._mark_account_stopped = signals.append
    monkeypatch.setattr("integration_harness.orchestrator.ExamStateStore", lambda *args: object())

    class FakeExam:
        def __init__(self, **kw):
            pass

        def ensure_passed(self):
            raise error

    monkeypatch.setattr("integration_harness.orchestrator.ExamRunner", FakeExam)
    detail = {"data": {"mytaskInfo": {"examStatus": 0}, "taskInfo": {"examId": "exam"}}}
    with pytest.raises(type(error)):
        value._run_exam_if_required("task", detail)
    assert signals == (["relogin"] if isinstance(error, ApiError) else ["exam_blocked"])


@pytest.mark.parametrize("exam_status", [0, "0"])
@pytest.mark.parametrize("learned_status", [1, 2, "2", None])
def test_exam_entry_checks_fresh_learning_and_defers_without_stopping(monkeypatch, exam_status, learned_status):
    value = runner()
    value.id_card = "account"
    value.name = "测试"
    value.oss_uploader = None
    value.client = SimpleNamespace(post_exam_json=lambda *args: None)
    value.notifier = SimpleNamespace(send_markdown=lambda _: True)
    value._get_task_detail = lambda _: {"data": {"mytaskInfo": {"learnedStatus": learned_status}}}
    states = []
    value._set_state = states.append
    monkeypatch.setattr("integration_harness.orchestrator.ExamStateStore", lambda *args: object())
    class FakeExam:
        def __init__(self, **kw):
            assert kw["can_start"]() == (learned_status in (2, "2"))
        def ensure_passed(self):
            raise ExamDeferred("未满足开考条件")
    monkeypatch.setattr("integration_harness.orchestrator.ExamRunner", FakeExam)
    detail = {"data": {"taskInfo": {"examId": "exam"}, "mytaskInfo": {"examStatus": exam_status}}}
    assert value._run_exam_if_required("task", detail) is True
    assert states == ["exam", "learning"]


def test_deferred_exam_unsynced_continues_learning():
    value = runner()
    calls = configure_loop(value, synced=[False, True])
    value._run_task({"taskInfo": {"id": "task"}})
    assert "watch" in calls
    assert calls[-1] == "complete"

import copy
import json
from types import SimpleNamespace

import pytest

from integration_harness.client import ApiError
from integration_harness.exam import ExamBlocked, ExamDeferred, ExamBusinessError, ExamPending, ExamRunner, flag, resolve_exam_ids
from integration_harness.exam_answers import questions_from_detail, question_fingerprint, valid_answer
from integration_harness.exam_state import ExamStateStore


QUESTIONS = [
    {"id": qid, "type": "single", "title": qid,
     "body": [{"value": "A", "label": "甲"}, {"value": "B", "label": "乙"}]}
    for qid in ("q1", "q2")
]


class Oss:
    def __init__(self):
        self.objects = {}
        self.fail_upload = False

    def object_exists(self, key):
        return key in self.objects

    def get_object_text(self, key):
        return self.objects[key]

    def upload_text(self, key, text):
        if not self.fail_upload:
            self.objects[key] = text
        return SimpleNamespace(success=not self.fail_upload)


class Server:
    """Stateful fake: mutations survive simulated lost responses and restarts."""
    def __init__(self):
        self.calls = []
        self.grades = {}
        self.current = None
        self.scores = [80]
        self.block_start = False
        self.lost_answer = False
        self.lost_start = False
        self.lost_submit = False
        self.wait_grade = False
        self.info = {"passedScore": 60, "heartTimeLimit": 60}

    def seed(self, phase="paper", score=80, answers=None, kind="normal"):
        gid = f"g{len(self.grades) + 1}"
        self.grades[gid] = {"phase": phase, "score": score, "answers": answers or {}, "kind": kind}
        self.current = gid
        return gid

    def detail(self, gid, check=False):
        grade = self.grades[gid]
        if grade["phase"] == "submitted" and check and not self.wait_grade:
            grade["phase"] = "result"
        info = {"examType": grade["kind"], "myAnswers": dict(grade["answers"]),
                "questionDetails": copy.deepcopy(QUESTIONS)}
        action = "gradeDetail"
        if grade["phase"] == "result":
            action = "gradeResult"
            info.update(endScore=grade["score"], isPassed=grade["score"] >= 60)
        elif grade["phase"] == "submitted":
            action = "grading"
        return {"action": action, "mygradeInfo": info, "examInfo": self.info}

    def __call__(self, method, payload):
        self.calls.append((method, copy.deepcopy(payload)))
        data = {}
        if method == "postViewExam":
            grade = self.grades.get(self.current, {})
            data = {"examInfo": self.info, "myexamInfo": {
                "status": 1 if grade.get("phase") == "paper" else 0,
                "myexamLeftSecond": 600,
                "isPassed": any(g["phase"] == "result" and g["score"] >= 60 and g["kind"] == "normal"
                                for g in self.grades.values()),
            }}
        elif method == "getGradeList":
            records = [{"id": gid, "examType": g["kind"], "gradeNumber": n,
                        "isPassed": g["phase"] == "result" and g["score"] >= 60,
                        "endScore": g["score"] if g["phase"] == "result" else None}
                       for n, (gid, g) in enumerate(self.grades.items(), 1)]
            data = {"list": records, "pagination": {"total": len(records)}}
        elif method == "getExamState":
            data = {"action": "showPaper", "mygradeId": self.current} if self.current else {"action": "reload"}
        elif method == "getMygradeDetail":
            data = self.detail(payload["mygradeId"], payload.get("type") == "checkState")
        elif method == "checkStartCondition":
            if self.block_start:
                return {"status": {"code": 7, "message": "次数用尽"}, "data": {"remaining": 0}}
            data = {"pass": True}
        elif method == "postStartExam":
            score = self.scores.pop(0)
            self.seed(score=score)
            if self.lost_start:
                self.lost_start = False
                raise ApiError("开考响应丢失")
            data = {"action": "showPaper", "mygradeId": self.current}
        elif method == "postSubmitAnswer":
            self.grades[payload["mygradeId"]]["answers"][payload["questionId"]] = payload["answer"]
            if self.lost_answer:
                self.lost_answer = False
                raise ApiError("答案响应丢失")
        elif method == "postSubmitExam":
            self.grades[payload["mygradeId"]]["phase"] = "submitted"
            if self.lost_submit:
                self.lost_submit = False
                raise ApiError("交卷响应丢失")
        elif method != "postHeartCheck":
            raise AssertionError(method)
        return {"status": {"code": 0}, "data": copy.deepcopy(data)}


@pytest.fixture
def setup(tmp_path):
    server, oss, llm, notices = Server(), Oss(), [], []
    config = SimpleNamespace(exam_poll_attempts=2, exam_poll_seconds=1,
                             exam_heartbeat_seconds=20, exam_submit_min_seconds=0)
    store = ExamStateStore(tmp_path, "account", "task", "exam", oss)

    def answer(question):
        llm.append(question["id"])
        return "A"

    def runner(api=None, root=None, answerer=None):
        checkpoint = store if root is None else ExamStateStore(root, "account", "task", "exam", oss)
        return ExamRunner(can_start=lambda: True, config=config, api=api or server, state_store=checkpoint,
                          answerer=answerer or answer, notify=notices.append, sleep=lambda _: None)

    return SimpleNamespace(server=server, oss=oss, llm=llm, notices=notices, store=store, runner=runner)


def names(server):
    return [name for name, _ in server.calls]


def test_new_exam_passes_and_keeps_result(setup):
    setup.runner().ensure_passed()
    state = setup.store.load()
    assert state["phase"] == "passed"
    assert state["score"] == 80
    assert state["attempts"]["g1"]["phase"] == "passed"
    assert setup.llm == ["q1", "q2"]
    assert names(setup.server).count("postSubmitExam") == 1
    assert setup.notices


def test_resume_active_skips_server_answers(setup):
    setup.server.seed(answers={"q1": "B"})
    setup.runner().ensure_passed()
    assert setup.llm == ["q2"]
    assert "postStartExam" not in names(setup.server)
    submitted = next(p for m, p in setup.server.calls if m == "postSubmitExam")
    assert submitted["myAnswers"] == {"q1": "B", "q2": "A"}


def test_passed_history_skips_start_and_llm(setup):
    setup.server.seed(phase="result")
    setup.runner().ensure_passed()
    assert setup.llm == []
    assert names(setup.server) == ["postViewExam", "getGradeList"]


def test_mock_pass_does_not_count_as_formal_pass(setup):
    setup.server.seed(phase="result", kind="mock")
    setup.runner().ensure_passed()
    assert names(setup.server).count("postStartExam") == 1
    assert setup.llm == ["q1", "q2"]


def test_failed_exam_retries_and_preserves_attempts(setup):
    setup.server.scores = [50, 80]
    setup.runner().ensure_passed()
    assert names(setup.server).count("postStartExam") == 2
    assert names(setup.server).count("checkStartCondition") == 2
    assert setup.store.load()["attempts"] == {
        "g1": {"phase": "failed", "score": 50, "passedScore": 60},
        "g2": {"phase": "passed", "score": 80, "passedScore": 60},
    }
    assert setup.llm == ["q1", "q2", "q1", "q2"]


def test_failed_exam_with_no_attempts_left_returns_to_learning(setup):
    setup.server.seed(phase="result", score=50)
    setup.server.block_start = True
    with pytest.raises(ExamDeferred, match="次数用尽") as error:
        setup.runner().ensure_passed()
    assert error.value.__cause__.code == 7
    assert error.value.__cause__.data == {"remaining": 0}
    assert "postStartExam" not in names(setup.server)
    assert setup.store.load()["phase"] == "failed"


def test_lost_start_response_recovers_without_second_start(setup, tmp_path):
    setup.server.lost_start = True
    with pytest.raises(ApiError):
        setup.runner().ensure_passed()
    assert setup.store.load()["phase"] == "starting"
    setup.runner(root=tmp_path / "other-device").ensure_passed()
    assert names(setup.server).count("postStartExam") == 1


def test_lost_answer_response_recovers_server_answer_without_more_llm(setup, tmp_path):
    setup.server.lost_answer = True
    with pytest.raises(ApiError):
        setup.runner().ensure_passed()
    setup.runner(root=tmp_path / "other-device").ensure_passed()
    assert setup.llm == ["q1", "q2"]
    assert names(setup.server).count("postSubmitAnswer") == 2


def test_unsubmitted_cached_answer_survives_failed_request(setup):
    failed = False

    def api(method, payload):
        nonlocal failed
        if method == "postSubmitAnswer" and not failed:
            failed = True
            raise ApiError("请求尚未送达")
        return setup.server(method, payload)

    with pytest.raises(ApiError):
        setup.runner(api=api).ensure_passed()
    setup.runner().ensure_passed()
    assert setup.llm == ["q1", "q2"]


def test_lost_submit_response_only_queries_result_on_resume(setup):
    setup.server.lost_submit = True
    with pytest.raises(ApiError):
        setup.runner().ensure_passed()
    setup.runner().ensure_passed()
    assert names(setup.server).count("postSubmitExam") == 1
    assert names(setup.server).count("postStartExam") == 1
    assert setup.llm == ["q1", "q2"]


def test_pending_result_is_not_failed_or_restarted(setup):
    setup.server.wait_grade = True
    with pytest.raises(ExamPending, match="成绩尚未确定"):
        setup.runner().ensure_passed()
    assert setup.store.load()["phase"] == "submitted"
    with pytest.raises(ExamPending):
        setup.runner().ensure_passed()
    assert names(setup.server).count("postSubmitExam") == 1
    assert names(setup.server).count("postStartExam") == 1
    setup.server.wait_grade = False
    setup.runner().ensure_passed()
    assert setup.store.load()["phase"] == "passed"


def test_restart_discovers_already_submitted_exam_from_history(setup):
    setup.server.seed(phase="submitted")
    setup.runner().ensure_passed()
    assert "postStartExam" not in names(setup.server)
    assert "postSubmitExam" not in names(setup.server)
    assert not setup.llm


def test_missing_result_fields_cannot_become_failed(setup):
    setup.server.seed(phase="result")

    def api(method, payload):
        response = setup.server(method, payload)
        if method == "postViewExam":
            response["data"]["myexamInfo"]["isPassed"] = False
        if method == "getGradeList":
            response["data"]["list"][0]["isPassed"] = False
        if method == "getMygradeDetail":
            response["data"]["mygradeInfo"] = {}
        return response

    with pytest.raises(ExamPending, match="成绩缺少"):
        setup.runner(api=api).ensure_passed()
    assert "postStartExam" not in names(setup.server)


def test_string_false_is_not_passed_even_with_high_score(setup):
    runner = setup.runner()
    runner.state["mygradeId"] = "g1"
    assert runner._record_result({"mygradeInfo": {"isPassed": "false", "endScore": 80},
                                  "examInfo": {"passedScore": 60}}) is False


def test_history_paginates_to_find_formal_pass(setup):
    def api(method, payload):
        if method != "getGradeList":
            return setup.server(method, payload)
        records = [{"id": f"mock{i}", "examType": "mock", "isPassed": True} for i in range(30)]
        if payload["page"] == 2:
            records = [{"id": "formal", "examType": "normal", "isPassed": "1", "endScore": 90}]
        return {"status": {"code": 0}, "data": {"list": records, "pagination": {"total": 31}}}

    setup.runner(api=api).ensure_passed()
    assert setup.store.load()["mygradeId"] == "formal"
    assert not setup.llm


@pytest.mark.parametrize("missing", ["status", "examInfo"])
def test_incomplete_view_never_starts_exam(setup, missing):
    def api(method, payload):
        response = setup.server(method, payload)
        if method == "postViewExam":
            (response if missing == "status" else response["data"]).pop(missing)
        return response

    with pytest.raises(ExamPending):
        setup.runner(api=api).ensure_passed()
    assert "postStartExam" not in names(setup.server)


def test_optional_myexam_summary_still_checks_history_and_start_condition(setup):
    def api(method, payload):
        response = setup.server(method, payload)
        if method == "postViewExam":
            response["data"].pop("myexamInfo")
        return response

    setup.runner(api=api).ensure_passed()
    ordered = names(setup.server)
    assert ordered.index("getGradeList") < ordered.index("checkStartCondition") < ordered.index("postStartExam")


def test_invalid_answer_does_not_submit_partial_paper(setup):
    with pytest.raises(ExamBlocked, match="没有有效答案"):
        setup.runner(answerer=lambda q: "A" if q["id"] == "q1" else "Z").ensure_passed()
    assert "postSubmitExam" not in names(setup.server)
    assert setup.store.load()["answers"]["q1"]["submitted"] is True


def test_missing_llm_config_blocks_before_start(setup):
    class MissingKey:
        def ready(self):
            raise ValueError("未配置 LLM_API_KEY")

    with pytest.raises(ExamBlocked, match="LLM_API_KEY"):
        setup.runner(answerer=MissingKey()).ensure_passed()
    assert "postStartExam" not in names(setup.server)


def test_oss_failure_preserves_local_but_does_not_start(setup):
    setup.oss.fail_upload = True
    with pytest.raises(RuntimeError, match="上传失败"):
        setup.runner().ensure_passed()
    assert setup.store.path.exists()
    assert "postStartExam" not in names(setup.server)


def test_identity_mismatch_does_not_load_checkpoint(setup):
    setup.oss.objects[setup.store.key] = json.dumps({"taskId": "other", "examId": "exam"})
    with pytest.raises(RuntimeError, match="身份不匹配"):
        setup.store.load()


def test_exam_mapping_accepts_only_explicit_ids():
    detail = {"data": {"taskInfo": {"examConfig": [{"id": "config", "examIds": ["e1", "e2"]}]}}}
    assert resolve_exam_ids(detail, "task") == ["e1", "e2"]
    assert resolve_exam_ids(detail, "task", '{"task":"override"}') == ["override"]
    with pytest.raises(ExamBlocked, match="无法确定"):
        resolve_exam_ids({"data": {"taskInfo": {"examConfig": [{"id": "config"}]}}}, "task")


def test_answer_validation_and_fingerprint():
    question = questions_from_detail({"mygradeInfo": {"questionDetails": QUESTIONS}})[0]
    assert valid_answer(question, "B") == "B"
    assert not valid_answer(question, "AB")
    assert not valid_answer(question, "答案 A")
    changed = {**question, "title": "另一题"}
    assert question_fingerprint(question) != question_fingerprint(changed)
    assert flag("false") is False
    assert flag(None) is None


def test_newer_submitted_record_supersedes_local_failed_attempt(setup):
    old = setup.server.seed(phase="result", score=50)
    state = setup.store.load()
    setup.store.save({**state, "mygradeId": old, "phase": "failed"})
    new = setup.server.seed(phase="submitted", score=80)
    setup.runner().ensure_passed()
    assert setup.store.load()["mygradeId"] == new
    assert "postStartExam" not in names(setup.server)
    assert not setup.llm


def test_cached_answer_is_discarded_when_question_changes(setup):
    gid = setup.server.seed()
    state = setup.store.load()
    setup.store.save({**state, "mygradeId": gid, "phase": "answering",
                      "answers": {"q1": {"answer": "B", "fingerprint": "old-question", "submitted": False}}})
    setup.runner().ensure_passed()
    assert setup.llm == ["q1", "q2"]
    assert setup.server.grades[gid]["answers"]["q1"] == "A"


def test_unknown_start_outcome_does_not_retry_start(setup):
    state = setup.store.load()
    setup.store.save({**state, "phase": "starting"})
    with pytest.raises(ExamPending, match="定位进行中"):
        setup.runner().ensure_passed()
    assert "postStartExam" not in names(setup.server)


def test_explicit_start_rejection_can_be_resumed_after_fix(setup):
    def api(method, payload):
        if method == "postStartExam":
            return {"status": {"code": 9, "message": "尚不满足开考条件"}, "data": {}}
        return setup.server(method, payload)

    with pytest.raises(ExamBusinessError):
        setup.runner(api=api).ensure_passed()
    assert setup.store.load()["phase"] == "not_started"
    setup.runner().ensure_passed()
    assert setup.store.load()["phase"] == "passed"


@pytest.mark.parametrize("ack", [None, True, False, 1, 0, "", "SUCCESS", [], [1]])
def test_success_payload_for_mutations_is_accepted(setup, ack):
    def api(method, payload):
        response = setup.server(method, payload)
        if method in {"postSubmitAnswer", "postSubmitExam", "postHeartCheck"}:
            response["data"] = ack
        return response

    setup.runner(api=api).ensure_passed()
    assert setup.store.load()["phase"] == "passed"


def test_face_detection_blocks_without_answering(setup):
    def api(method, payload):
        if method == "postStartExam":
            return {"status": {"code": 0}, "data": {"action": "showFaceDetect"}}
        return setup.server(method, payload)

    with pytest.raises(ExamBlocked, match="人脸认证"):
        setup.runner(api=api).ensure_passed()
    assert not setup.llm


def test_heartbeat_401_prevents_answer_mutations(setup):
    def api(method, payload):
        if method == "postHeartCheck":
            raise ApiError("expired", status_code=401)
        return setup.server(method, payload)

    with pytest.raises(ApiError) as error:
        setup.runner(api=api).ensure_passed()
    assert error.value.status_code == 401
    assert not setup.llm
    assert "postSubmitExam" not in names(setup.server)


def test_remote_revision_wins_over_stale_local_clock(setup):
    identity = setup.store.load()
    setup.store.path.parent.mkdir(parents=True)
    setup.store.path.write_text(json.dumps({**identity, "revision": 1, "updatedAtNs": 999999999999999999999,
                                            "phase": "answering"}))
    setup.oss.objects[setup.store.key] = json.dumps({**identity, "revision": 2, "updatedAtNs": 1,
                                                   "phase": "submitted"})
    assert setup.store.load()["phase"] == "submitted"


@pytest.mark.parametrize("phase", [None, "result"])
def test_incomplete_learning_prevents_start_and_retry(setup, phase):
    if phase:
        setup.server.seed(phase=phase, score=50)
    runner = setup.runner()
    runner.can_start = lambda: False
    with pytest.raises(ExamDeferred, match="学习尚未完成"):
        runner.ensure_passed()
    assert "checkStartCondition" not in names(setup.server)
    assert "postStartExam" not in names(setup.server)
    assert not setup.llm


@pytest.mark.parametrize("data", [{"pass": False}, {}, None, {"pass": "true"}, {"pass": 1}])
def test_start_requires_explicit_pass_true(setup, data):
    def api(method, payload):
        response = setup.server(method, payload)
        if method == "checkStartCondition":
            response["data"] = data
        return response
    with pytest.raises(ExamDeferred):
        setup.runner(api=api).ensure_passed()
    assert "postStartExam" not in names(setup.server)
    assert not setup.llm


@pytest.mark.parametrize("phase", ["paper", "submitted", "result"])
def test_existing_exam_progress_does_not_require_new_start_permission(setup, phase):
    setup.server.seed(phase=phase, answers={"q1": "A", "q2": "A"})
    runner = setup.runner()
    runner.can_start = lambda: False
    runner.ensure_passed()
    assert setup.store.load()["phase"] == "passed"
    assert "postStartExam" not in names(setup.server)
    assert not setup.llm


def test_learning_completion_rechecked_before_retry(setup):
    setup.server.scores = [50, 80]
    checks = iter([True, False])
    runner = setup.runner()
    runner.can_start = lambda: next(checks)
    with pytest.raises(ExamDeferred):
        runner.ensure_passed()
    assert names(setup.server).count("postStartExam") == 1
    assert setup.store.load()["phase"] == "failed"


@pytest.mark.parametrize("local", [False, True])
def test_remote_checkpoint_get_404_preserves_local_or_reconciles_server(setup, local, monkeypatch):
    import oss2
    gid = setup.server.seed(answers={"q1": "A", "q2": "A"})
    if local:
        setup.store.save({**setup.store.identity, "mygradeId": gid, "phase": "answering",
                          "answers": {"q1": {"answer": "A"}}})
    monkeypatch.setattr(setup.oss, "object_exists", lambda key: True)
    def missing(key):
        raise oss2.exceptions.NotFound(404, {}, "", {})
    monkeypatch.setattr(setup.oss, "get_object_text", missing)
    state = setup.store.load()
    if local:
        assert state["mygradeId"] == gid
        assert state["answers"]["q1"]["answer"] == "A"
    setup.runner().ensure_passed()
    assert "postStartExam" not in names(setup.server)
    assert not setup.llm


@pytest.mark.parametrize("kind", ["NoSuchBucket", "AccessDenied", "RequestError"])
def test_remote_checkpoint_other_failures_remain_errors(setup, monkeypatch, kind):
    import oss2
    monkeypatch.setattr(setup.oss, "object_exists", lambda key: True)
    error_type = getattr(oss2.exceptions, kind)
    def failed(key):
        if kind == "RequestError":
            raise error_type("network unavailable")
        raise error_type(404 if kind == "NoSuchBucket" else 403, {}, "", {})
    monkeypatch.setattr(setup.oss, "get_object_text", failed)
    with pytest.raises(error_type):
        setup.store.load()


@pytest.mark.parametrize("method", ["postSubmitAnswer", "postSubmitExam", "postHeartCheck"])
def test_mutation_error_status_is_not_accepted(setup, method):
    runner = setup.runner(api=lambda *args: {"status": {"code": 7}, "data": True})
    with pytest.raises(ExamBusinessError):
        runner._call(method, {})


@pytest.mark.parametrize("method", ["postViewExam", "getMygradeDetail", "getGradeList", "postStartExam", "checkStartCondition"])
def test_query_and_start_data_still_require_objects(setup, method):
    runner = setup.runner(api=lambda *args: {"status": {"code": 0}, "data": True})
    with pytest.raises(ExamPending, match="data 不是对象"):
        runner._call(method, {})

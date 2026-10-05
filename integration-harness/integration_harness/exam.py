"""Formal exam branch. Passing an exam never marks a learning task complete."""
from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timezone, timedelta

from .exam_answers import question_fingerprint, questions_from_detail, valid_answer


class ExamBlocked(RuntimeError):
    """Needs configuration or human action; stop watch from repeatedly opening exams."""


class ExamDeferred(RuntimeError):
    """Start conditions are unmet; return to learning without stopping the account."""


class ExamPending(RuntimeError):
    """Server state is unresolved. Preserve the checkpoint and reconcile on resume."""


class ExamBusinessError(ExamBlocked):
    def __init__(self, method: str, status: dict, data):
        self.code = status.get("code")
        self.data = data
        self.message = str(status.get("message") or "未知业务错误")
        super().__init__(f"{method}: {self.message}（code={self.code}）")


def flag(value) -> bool | None:
    if value is True or value == 1 or value in ("1", "true"):
        return True
    if value is False or value == 0 or value in ("0", "false"):
        return False
    return None


def number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def resolve_exam_ids(detail: dict, task_id: str, mapping_json: str = "{}") -> list[str]:
    """Only explicitly named exam IDs; never confuse a config/group ID with an exam."""
    try:
        mapping = json.loads(mapping_json)
    except ValueError as exc:
        raise ExamBlocked("EXAM_TASK_MAP_JSON 不是合法 JSON") from exc
    if not isinstance(mapping, dict):
        raise ExamBlocked("EXAM_TASK_MAP_JSON 必须是 taskId 到 examId 列表的映射")
    ids: list[str] = []

    def add(value):
        values = value if isinstance(value, list) else [value]
        for item in values:
            if not isinstance(item, str) or not item.strip():
                raise ExamBlocked("考试 ID 必须是非空字符串")
            if item not in ids:
                ids.append(item)

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"examId", "examIds"}:
                    add(item)
                elif isinstance(item, (dict, list)):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    if task_id in mapping:
        add(mapping[task_id])
    else:
        task = (detail.get("data") or {}).get("taskInfo") or {}
        visit(task.get("examConfig"))
        for key in ("examId", "examIds"):
            if key in task:
                add(task[key])
    if not ids:
        raise ExamBlocked(f"任务 {task_id} 需要考试，但无法确定 examId；请配置 EXAM_TASK_MAP_JSON")
    return ids


class _Heartbeat:
    def __init__(self, call, exam_id, grade_id, interval):
        self.call = call
        self.payload = {"examId": exam_id, "mygradeId": grade_id}
        self.interval = interval
        self.stop = threading.Event()
        self.error: Exception | None = None
        self.thread = threading.Thread(target=self._loop, daemon=True, name="exam-heartbeat")

    def _beat(self):
        self.call("postHeartCheck", {**self.payload,
                  "updatedAt": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
                  "timestamp": int(time.time() * 1000)})

    def _loop(self):
        while not self.stop.wait(self.interval):
            try:
                self._beat()
            except Exception as exc:
                self.error = exc
                return

    def check(self):
        if self.error:
            raise self.error

    def __enter__(self):
        self._beat()
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join()


class ExamRunner:
    def __init__(self, *, config, api, state_store, answerer, notify=lambda message: None,
                 sleep=time.sleep, can_start=lambda: False):
        self.config = config
        self.api = api
        self.store = state_store
        self.answerer = answerer
        self.notify = notify
        self.sleep = sleep
        self.can_start = can_start
        self.state = self.store.load()
        self.exam_id = self.state["examId"]

    def _call(self, method, payload):
        response = self.api(method, payload)
        status = response.get("status")
        if not isinstance(status, dict) or "code" not in status:
            raise ExamPending(f"{method} 缺少业务状态，不能推断考试进度")
        if status["code"] not in (0, "0"):
            raise ExamBusinessError(method, status, response.get("data"))
        data = response.get("data")
        # These acknowledgements are consumed only for their business status.
        # Unlike query/start responses, their data may be a scalar or a list.
        if method in {"postHeartCheck", "postSubmitAnswer", "postSubmitExam"} and not isinstance(data, dict):
            data = {}
        if data is None and method == "checkStartCondition":
            data = {}
        if not isinstance(data, dict):
            raise ExamPending(f"{method} 返回的 data 不是对象")
        info = data.get("examInfo")
        if isinstance(info, dict):
            identity = info.get("id") or info.get("_id")
            if identity and str(identity) != self.exam_id:
                raise ExamPending(f"{method} 返回了其他考试的信息")
        grade = data.get("mygradeInfo")
        if isinstance(grade, dict):
            identity = grade.get("id") or grade.get("_id")
            if identity and "mygradeId" in payload and str(identity) != str(payload["mygradeId"]):
                raise ExamPending(f"{method} 返回了其他试卷的信息")
        return data

    def _save(self, **changes):
        self.state.update(changes)
        self.store.save(self.state)

    def _history(self):
        records, seen = [], set()
        page_size = 30
        for page in range(1, 101):
            data = self._call("getGradeList", {"page": page, "pageSize": page_size,
                              "filter": {"examId": self.exam_id}})
            batch = data.get("list")
            if not isinstance(batch, list):
                raise ExamPending("考试历史缺少 list，不能当作没有考试记录")
            for record in batch:
                if not isinstance(record, dict):
                    raise ExamPending("考试历史记录格式异常")
                record_id = record.get("id") or record.get("_id")
                if not record_id or record_id in seen:
                    raise ExamPending("考试历史记录 ID 缺失或分页重复")
                seen.add(record_id)
                if record.get("examId") not in (None, self.exam_id):
                    raise ExamPending("考试历史返回了其他考试的记录")
                if record.get("examType") == "normal":
                    records.append(record)
                elif record.get("examType") != "mock":
                    raise ExamPending("考试历史未标明正式或模拟考试类型")
            pagination = data.get("pagination") or {}
            total = number(pagination.get("total"))
            if total is not None:
                if len(seen) >= total:
                    return records
                if not batch:
                    raise ExamPending("考试历史分页尚未读完却返回空页")
            elif len(batch) < page_size:
                return records
        raise ExamPending("考试历史分页超限，未尝试开考")

    def _grade_id(self, record):
        return str(record.get("mygradeId") or record.get("id") or record.get("_id") or "")

    def _latest_record(self, records):
        if len(records) == 1:
            return records[0]
        if any(number(record.get("gradeNumber")) is None for record in records):
            raise ExamPending("多份正式考试记录缺少考试次数，无法确定最新进度")
        return max(records, key=lambda record: number(record["gradeNumber"]))

    def _paper_id(self):
        for _ in range(self.config.exam_poll_attempts):
            data = self._call("getExamState", {"examId": self.exam_id})
            if data.get("action") == "showFaceDetect":
                raise ExamBlocked("考试需要人脸认证，请完成认证后恢复")
            if data.get("action") == "showPaper" and data.get("mygradeId"):
                return str(data["mygradeId"])
            self.sleep(self.config.exam_poll_seconds)
        raise ExamPending("等待试卷或定位进行中考试超时，保留开考状态")

    def _record_result(self, detail):
        grade = detail.get("mygradeInfo") or {}
        if grade.get("examType") not in (None, "normal"):
            raise ExamPending("成绩不是正式考试成绩")
        score = number(grade.get("endScore"))
        threshold = number((detail.get("examInfo") or {}).get("passedScore"))
        passed = flag(grade.get("isPassed"))
        if passed is None:
            if score is None or threshold is None or threshold <= 0:
                raise ExamPending("成绩缺少通过标记或有效分数，不能判定不及格")
            passed = score >= threshold
        phase = "passed" if passed else "failed"
        changed = self.state.get("phase") != phase
        attempts = dict(self.state.get("attempts") or {})
        attempts[self.state["mygradeId"]] = {"phase": phase, "score": score, "passedScore": threshold}
        self._save(phase=phase, score=score, passedScore=threshold, attempts=attempts)
        if changed:
            self.notify(f"考试{'通过' if passed else '不及格'}：{self.exam_id}，成绩：{score}，及格线：{threshold}")
        return passed

    def _wait_result(self):
        for _ in range(self.config.exam_poll_attempts):
            data = self._call("getMygradeDetail", {"mygradeId": self.state["mygradeId"], "type": "checkState"})
            if data.get("action") == "gradeResult":
                return self._record_result(data)
            self.sleep(self.config.exam_poll_seconds)
        raise ExamPending("已交卷，成绩尚未确定；恢复后继续查分，不重新开考")

    def _ready(self):
        ready = getattr(self.answerer, "ready", None)
        if ready:
            try:
                ready()
            except ValueError as exc:
                raise ExamBlocked(str(exc)) from exc

    def ensure_passed(self):
        """Return only after a server-confirmed formal pass; otherwise preserve state."""
        failed_this_run = set()
        while True:
            view = self._call("postViewExam", {"examId": self.exam_id})
            if (not isinstance(view.get("examInfo"), dict)
                    or (view.get("myexamInfo") is not None and not isinstance(view["myexamInfo"], dict))):
                raise ExamPending("考试信息缺少 examInfo 或 myexamInfo 格式异常，未尝试开考")
            info = view["examInfo"]
            mine = view.get("myexamInfo") or {}
            records = self._history()
            passed_records = [r for r in records if flag(r.get("isPassed")) is True]
            if flag(mine.get("isPassed")) is True or passed_records:
                checkpoint = {"phase": "passed", "serverConfirmed": True}
                if passed_records:
                    record = max(passed_records, key=lambda r: number(r.get("gradeNumber")) or 0)
                    checkpoint.update(mygradeId=self._grade_id(record), score=number(record.get("endScore")))
                self._save(**checkpoint)
                return
            mock = view.get("mockGradeInfo") or {}
            if flag(mock.get("status")) is True:
                raise ExamBlocked("存在进行中的模拟考试，请先完成后恢复正式考试")

            grade_id = str(self.state.get("mygradeId") or "")
            active = flag(mine.get("status")) is True and (number(mine.get("myexamLeftSecond")) or 0) > 0
            if active:
                grade_id = self._paper_id()
            elif self.state.get("phase") == "starting":
                # postStartExam may have succeeded despite a transport error.
                grade_id = self._paper_id()
            elif records:
                record = self._latest_record(records)
                if not grade_id or grade_id in {self._grade_id(r) for r in records}:
                    grade_id = self._grade_id(record)

            if grade_id:
                if grade_id != self.state.get("mygradeId"):
                    self._save(mygradeId=grade_id, phase="answering", answers={})
                detail = self._call("getMygradeDetail", {"mygradeId": grade_id})
                if detail.get("action") == "gradeResult":
                    if self._record_result(detail):
                        return
                elif self.state.get("phase") == "submitted":
                    if self._wait_result():
                        return
                elif detail.get("action") == "gradeDetail":
                    if self.state.get("phase") in {"submitting", "failed", "passed"}:
                        checked = self._call("getMygradeDetail", {"mygradeId": grade_id, "type": "checkState"})
                        if checked.get("action") == "gradeResult":
                            if self._record_result(checked):
                                return
                        elif checked.get("action") == "gradeDetail" and self.state.get("phase") == "submitting":
                            if self._answer_and_submit(checked, info):
                                return
                        else:
                            raise ExamPending("考试阶段尚未确认，保留记录等待恢复")
                    elif self._answer_and_submit(detail, info):
                        return
                else:
                    if self._wait_result():
                        return
                if grade_id in failed_this_run:
                    raise ExamPending("服务端仍返回上一份不及格试卷，未重复开考")
                failed_this_run.add(grade_id)

            # Only no previous attempt or an explicitly failed result reaches here.
            if not self.can_start():
                raise ExamDeferred("学习尚未完成，继续原有学习流程")
            try:
                condition = self._call("checkStartCondition", {"examId": self.exam_id, "examType": "normal"})
            except ExamBusinessError as exc:
                if any(word in exc.message for word in ("进行中", "请先交卷", "请先提交", "继续答题")):
                    grade_id = self._paper_id()
                    self._save(mygradeId=grade_id, phase="answering",
                               answers=self.state.get("answers", {}) if grade_id == self.state.get("mygradeId") else {})
                    continue
                raise ExamDeferred(str(exc)) from exc
            if condition.get("pass") is not True:
                raise ExamDeferred("开考条件未明确通过，继续原有学习流程")
            self._ready()
            previous_phase = self.state.get("phase", "not_started")
            self._save(phase="starting")
            try:
                start = self._call("postStartExam", {"examId": self.exam_id, "examType": "normal"})
            except ExamBusinessError:
                # Explicit rejection is different from an unknown network outcome.
                self._save(phase=previous_phase)
                raise
            if start.get("action") == "showFaceDetect":
                raise ExamBlocked("考试需要人脸认证，请完成认证后恢复")
            new_id = str(start.get("mygradeId") or self._paper_id())
            if new_id in failed_this_run:
                raise ExamPending("开考接口返回已失败的试卷，保留状态等待核实")
            self._save(mygradeId=new_id, phase="answering", answers={})
            detail = self._call("getMygradeDetail", {"mygradeId": new_id})
            if detail.get("action") == "gradeResult":
                if self._record_result(detail):
                    return
            elif detail.get("action") == "gradeDetail":
                if self._answer_and_submit(detail, info):
                    return
            else:
                if self._wait_result():
                    return
            # Refresh view/history and recheck server conditions before a retry.

    def _answer_and_submit(self, detail, info):
        grade = detail.get("mygradeInfo") or {}
        if grade.get("examType") not in (None, "normal"):
            raise ExamBlocked("拒绝对模拟考试试卷执行正式考试流程")
        try:
            questions = questions_from_detail(detail)
        except ValueError as exc:
            raise ExamBlocked(str(exc)) from exc
        server_answers = grade.get("myAnswers") or {}
        if not isinstance(server_answers, dict):
            raise ExamPending("服务端已答题信息格式异常")
        cache = dict(self.state.get("answers") or {})
        answers = {}
        for question in questions:
            answer = valid_answer(question, server_answers.get(question["id"]))
            if answer:
                answers[question["id"]] = answer
                cache[question["id"]] = {"answer": answer, "fingerprint": question_fingerprint(question), "submitted": True}
        self._save(phase="answering", answers=cache)
        heartbeat_limit = number(info.get("heartTimeLimit"))
        interval = self.config.exam_heartbeat_seconds
        if heartbeat_limit and heartbeat_limit > 0:
            interval = min(interval, heartbeat_limit / 2)
        grade_id = self.state["mygradeId"]
        with _Heartbeat(self._call, self.exam_id, grade_id, interval) as heartbeat:
            started = time.monotonic()
            for question in questions:
                heartbeat.check()
                qid = question["id"]
                if qid in answers:
                    continue
                fingerprint = question_fingerprint(question)
                entry = cache.get(qid) or {}
                answer = valid_answer(question, entry.get("answer")) if entry.get("fingerprint") == fingerprint else ""
                question_started = time.monotonic()
                wall_started = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                if not answer:
                    self._ready()
                    try:
                        answer = valid_answer(question, self.answerer(question))
                    except ValueError as exc:
                        raise ExamBlocked(str(exc)) from exc
                if not answer:
                    raise ExamBlocked(f"题目 {qid} 没有有效答案，保留进度，不提交空白试卷")
                cache[qid] = {"answer": answer, "fingerprint": fingerprint, "submitted": False}
                self._save(answers=cache)
                heartbeat.check()
                self._call("postSubmitAnswer", {"answer": answer, "mygradeId": grade_id,
                           "questionId": qid, "examId": self.exam_id, "type": question["type"],
                           "duration": max(0, int((time.monotonic() - question_started) * 1000)),
                           "startTime": wall_started})
                answers[qid] = answer
                cache[qid]["submitted"] = True
                self._save(answers=cache)
            if len(answers) != len(questions):
                raise ExamBlocked("仍有未答题目，不交卷")
            # The reference client uses a seconds configuration; do not guess
            # units for unverified server fields.
            minimum = self.config.exam_submit_min_seconds
            while time.monotonic() - started < minimum:
                heartbeat.check()
                self.sleep(max(0, min(1, minimum - (time.monotonic() - started))))
            heartbeat.check()
            self._save(phase="submitting")
            self._call("postSubmitExam", {"myAnswers": answers, "mygradeId": grade_id, "examId": self.exam_id})
            self._save(phase="submitted")
        return self._wait_result()

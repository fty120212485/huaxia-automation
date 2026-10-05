from __future__ import annotations

import hashlib
import json
import logging
import struct
import threading
import time
import webbrowser
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any

import qrcode

from .client import ApiError, HxaccClient, VideoDownloadError
from .config import Config
from .exam import ExamBlocked, ExamDeferred, ExamRunner, resolve_exam_ids
from .exam_answers import LlmAnswerer
from .exam_state import ExamStateStore
from .oss_upload import OssAccountUploader
from .qr_page import QRPageState, QRServerHandle, start_qr_server
from .storage import RunStore
from .video import choose_m3u8_url, parse_m3u8
from .wechat import WeChatNotifier


logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode_png(width: int, height: int, raw_pixels: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw_pixels, 9))
        + chunk(b"IEND", b"")
    )


class _RestartLearning(RuntimeError):
    pass


class _NoUnfinishedLearning(RuntimeError):
    pass


class _TaskCompletedAwaitResume(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.already_notified = True


class _DailyLimitReached(RuntimeError):
    def __init__(self, message: str, *, already_notified: bool) -> None:
        super().__init__(message)
        self.already_notified = already_notified


class _AuthVerificationFailed(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.already_notified = True


class Orchestrator:
    _MAX_RESTART_ATTEMPTS = 3

    def __init__(
        self,
        *,
        config: Config,
        client: HxaccClient,
        store: RunStore,
        session_id: str,
        logger: logging.Logger,
        notifier: WeChatNotifier,
        id_card: str,
        name: str,
        replay: bool,
        auto_next_course: bool,
        oss_uploader: OssAccountUploader,
    ) -> None:
        self.config = config
        self.client = client
        self.store = store
        self.session_id = session_id
        self.logger = logger
        self.notifier = notifier
        self.id_card = id_card
        self.name = name
        self.replay = replay
        self.auto_next_course = auto_next_course
        self.oss_uploader = oss_uploader
        self.qr_state: QRPageState | None = None
        self.qr_server_handle: QRServerHandle | None = None
        self.tasks: list[dict[str, Any]] = []

    def run(self) -> None:
        self._set_state("starting")
        self.logger.info("开始正常路径集成测试")

        tasks = self._select_tasks()
        self.tasks = tasks
        total = len(tasks)
        self.logger.info("接口返回 %s 个任务，将逐个处理", total)

        completed_any = False
        for index, task in enumerate(tasks, 1):
            watched = self._run_task_with_restarts(
                task,
                index=index,
                total=total,
            )
            if watched:
                completed_any = True

        if completed_any:
            self.logger.info("所有任务处理完成")
        else:
            self.logger.info("没有未完成的视频需要观看")

    def _run_task_with_restarts(
        self,
        task: dict[str, Any],
        *,
        index: int,
        total: int,
    ) -> bool:
        task_info = task.get("taskInfo") or {}
        title = task_info.get("title") or "<未命名任务>"

        for attempt in range(1, self._MAX_RESTART_ATTEMPTS + 1):
            try:
                self.logger.info(
                    "[%s/%s] 开始处理任务：%s",
                    index,
                    total,
                    title,
                )
                self._run_task(task)
                self.logger.info(
                    "[%s/%s] 任务处理完成：%s",
                    index,
                    total,
                    title,
                )
                return True
            except _NoUnfinishedLearning as exc:
                self.logger.info(
                    "[%s/%s] 任务“%s”已完结或无未完成小节，跳过：%s",
                    index,
                    total,
                    title,
                    exc,
                )
                return False
            except _RestartLearning as exc:
                if attempt >= self._MAX_RESTART_ATTEMPTS:
                    raise RuntimeError(
                        f"任务“{title}”连续 {attempt} 次遇到“{exc}”，自动重跑已达上限"
                    ) from exc
                self.logger.warning(
                    "任务“%s”第 %s 次运行遇到“%s”，按正常链路重新开始",
                    title,
                    attempt,
                    exc,
                )
                self._set_state("restarting")
                time.sleep(1)

    def _run_task(self, task: dict[str, Any]) -> None:
        task_info = task.get("taskInfo") or {}
        task_id = task_info.get("id")
        if not task_id:
            raise RuntimeError("任务数据缺少 taskInfo.id")
        title = task_info.get("title") or "<未命名任务>"
        self.logger.info("处理任务：%s（ID：%s）", title, task_id)

        detail = self._get_task_detail(task_id)
        skipped_course_ids: set[str] = set()

        while True:
            if self._run_exam_if_required(str(task_id), detail):
                detail = self._get_task_detail(task_id)
            # Exam pass and task completion are independent. The certificate
            # endpoint remains the authority for synced, including after exams.
            if self._is_task_completed(task_id):
                self._on_task_completed(task, detail)
                break
            course_id = self._next_unfinished_course_id(
                detail,
                exclude=skipped_course_ids,
            )
            forced_course_list_id = ""
            forced_watch = False
            if not course_id:
                forced_course_list_id, course_id = self._cert_watch_target(task_id)
                forced_watch = bool(course_id)
                if not forced_watch:
                    forced_course_list_id, course_id = self._default_course_target(detail)
                    forced_watch = bool(course_id)
                if not forced_watch:
                    self.logger.warning(
                        "任务未同步，但无法从 getTaskCert 或任务配置定位待观看课程：%s",
                        task_id,
                    )
                    raise RuntimeError("任务尚未同步且无法定位课程，保留未完成状态")

            try:
                if forced_watch:
                    course_list_id = forced_course_list_id
                    course_title = course_id
                    doc_id = ""
                    self.logger.info(
                        "任务未同步，继续观看课程：%s",
                        course_id,
                    )
                else:
                    course_list_id, course_ids = self._find_course_group(
                        detail,
                        course_id,
                    )
                    course_list = self._get_course_list(
                        task_id,
                        course_list_id,
                        course_ids,
                    )
                    course_info, doc_id = self._select_course_and_doc(
                        course_list,
                        course_id=course_id,
                    )
                    self.logger.info(
                        "选中课程：%s",
                        course_info.get("title"),
                    )
                    course_title = course_info.get("title") or course_id
                self._watch_doc(
                    task_id=task_id,
                    course_id=course_id,
                    course_list_id=course_list_id,
                    doc_id=doc_id or None,
                )
                detail = self._get_task_detail(task_id)
            except _NoUnfinishedLearning as exc:
                if forced_watch:
                    raise RuntimeError("任务尚未同步，服务端没有可继续学习的小节") from exc
                skipped_course_ids.add(course_id)
                self.logger.info("课程 %s 跳过：%s", course_id, exc)
                detail = self._get_task_detail(task_id)

        self.logger.info("任务已全部看完：%s", title)
        self._set_state("completed")

    def _run_exam_if_required(self, task_id: str, detail: dict[str, Any]) -> bool:
        mytask = (detail.get("data") or {}).get("mytaskInfo") or {}
        task_info = (detail.get("data") or {}).get("taskInfo") or {}
        if not task_info.get("examId") or mytask.get("examStatus") not in (0, "0"):
            return False
        self._set_state("exam")
        try:
            exam_ids = resolve_exam_ids(detail, task_id, self.config.exam_task_map_json)
            for exam_id in exam_ids:
                checkpoint = ExamStateStore(
                    self.config.runs_dir, self.id_card, task_id, exam_id, self.oss_uploader,
                )
                runner = ExamRunner(
                    config=self.config, api=self.client.post_exam_json,
                    state_store=checkpoint, answerer=LlmAnswerer(self.config, logger=self.logger),
                    can_start=lambda: ((self._get_task_detail(task_id).get("data") or {})
                                       .get("mytaskInfo") or {}).get("learnedStatus") in (2, "2"),
                    notify=lambda message: self.notifier.send_markdown(
                        f"学员：{self.name}\n任务：{task_id}\n{message}"
                    ),
                )
                runner.ensure_passed()
                self._record(
                    event_type="exam_passed", method="STATE", host=self.config.exam_base_url,
                    path=f"exam/{exam_id}", status_code=None,
                    request_payload={"taskId": task_id, "examId": exam_id},
                    response_summary={key: runner.state.get(key) for key in
                                      ("mygradeId", "phase", "score", "passedScore")},
                )
        except ExamDeferred as exc:
            self.logger.info("考试暂不具备开考条件：%s", exc)
            self._set_state("learning")
            return True
        except ApiError as exc:
            if exc.status_code == 401:
                self._signal_relogin()
            raise
        except ExamBlocked:
            self._mark_account_stopped("exam_blocked")
            raise
        self._set_state("learning")
        return True

    def _get_task_detail(self, task_id: str) -> dict[str, Any]:
        return self._post_json(
            "/api/mytask/getMytaskDetail",
            {
                "taskId": task_id,
                "includeTaskCourse": True,
                "appId": self.config.app_id,
            },
            event_type="getMytaskDetail",
        )

    def _get_course_list(
        self,
        task_id: str,
        course_list_id: str,
        course_ids: list[str],
        *,
        page_size: int = 30,
    ) -> dict[str, Any]:
        return self._post_json(
            "/api/mytask/getTaskCourseList",
            {
                "taskId": task_id,
                "courseListId": course_list_id,
                "courseIds": course_ids,
                "page": 1,
                "pageSize": page_size,
                "appId": self.config.app_id,
            },
            event_type="getTaskCourseList",
        )

    def _watch_doc(
        self,
        *,
        task_id: str,
        course_id: str,
        course_list_id: str,
        doc_id: str | None = None,
    ) -> None:
        post_learn = self._post_json(
            "/api/mytask/postLearnCourse",
            {
                "taskId": task_id,
                "courseId": course_id,
                "courseListId": course_list_id,
                "platform": "minapp",
                "appId": self.config.app_id,
            },
            event_type="postLearnCourse",
        )
        mycourse_id = self._extract_mycourse_id(post_learn)

        course_detail = self._post_json(
            "/api/mycourse/getMycourseDetail",
            {
                "mycourseId": mycourse_id,
                "docId": doc_id,
                "platform": "minapp",
                "appId": self.config.app_id,
            },
            event_type="getMycourseDetail",
        )
        doc_id = self._resolve_current_doc_id(course_detail)
        self.logger.info("当前小节：%s", doc_id)
        random_value = self._find_random(course_detail, doc_id)
        target_seconds = self._find_target_seconds(course_detail, doc_id)
        resume_seconds = (
            0
            if getattr(self, "replay", False)
            else self._find_learned_seconds(course_detail, doc_id)
        )

        point_state = self._gd_call(
            "gdGetPointState",
            mycourse_id=mycourse_id,
            study_token="",
            point_code=None,
        )
        point_state_data = self._coerce_data(point_state)
        study_token = point_state_data.get("studyToken") or ""
        point_code = point_state_data.get("pointCode") or ""
        qrcode_url = point_state_data.get("qrcodeUrl") or ""

        self._gd_call(
            "gdResumeCourse",
            mycourse_id=mycourse_id,
            study_token=study_token,
            point_code=point_code,
        )

        m3u8_url = choose_m3u8_url(
            course_detail["data"]["docInfo"],
            quality=self.config.video_quality,
        )
        segments = self._load_segments(m3u8_url)
        if not segments:
            raise RuntimeError("m3u8 中没有可播放的视频分片")

        self._set_state("learning")
        self._play_and_heartbeat(
            mycourse_id=mycourse_id,
            doc_id=doc_id,
            study_token=study_token,
            random_value=random_value,
            segments=segments,
            point_code=point_code,
            qrcode_url=qrcode_url,
            target_seconds=target_seconds,
            resume_seconds=resume_seconds,
        )

    def _set_state(self, state: str) -> None:
        self.store.update_session_state(self.session_id, state, utc_now())

    def _record(
        self,
        *,
        event_type: str,
        method: str,
        host: str,
        path: str,
        status_code: int | None,
        request_payload: dict[str, Any] | None,
        response_summary: dict[str, Any] | None,
    ) -> None:
        self.store.record_event(
            session_id=self.session_id,
            occurred_at=utc_now(),
            event_type=event_type,
            method=method,
            host=host,
            path=path,
            status_code=status_code,
            request_payload=request_payload,
            response_summary=response_summary,
        )

    def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        event_type: str,
    ) -> dict[str, Any]:
        self.logger.info(
            "POST %s 请求参数: %s",
            path,
            json.dumps(self._redact_payload(payload), ensure_ascii=False),
        )
        try:
            data = self.client.post_learn_json(path, payload)
        except ApiError as exc:
            if exc.status_code == 401:
                self._signal_relogin()
            raise
        summary = self._summarize(data)
        self._record(
            event_type=event_type,
            method="POST",
            host=self.config.learn_base_url,
            path=path,
            status_code=200,
            request_payload=payload,
            response_summary=summary,
        )
        self.logger.info(
            "POST %s 响应摘要: %s",
            path,
            json.dumps(summary, ensure_ascii=False),
        )
        return data

    def _gd_call(
        self,
        method: str,
        *,
        mycourse_id: str,
        study_token: str,
        point_code: str | None,
    ) -> dict[str, Any]:
        payload = {
            "method": method,
            "mycourseId": mycourse_id,
            "studyToken": study_token,
            "pointCode": point_code,
            "appId": self.config.app_id,
        }
        return self._post_json(
            "/api/area/guangdongPoint",
            payload,
            event_type=method,
        )

    def _select_tasks(self) -> list[dict[str, Any]]:
        data = self._post_json(
            "/api/mytask/getTaskList",
            {"appId": self.config.app_id},
            event_type="getTaskList",
        )
        task_list = data.get("data", {}).get("list", [])
        if not task_list:
            raise RuntimeError("接口未返回任何任务")
        return task_list

    def _next_unfinished_course_id(
        self,
        detail: dict[str, Any],
        *,
        exclude: set[str] | None = None,
    ) -> str:
        learned = (
            detail.get("data", {})
            .get("mytaskInfo", {})
            .get("learned")
            or {}
        )
        excluded = exclude or set()
        for course_id, state in learned.items():
            if course_id in excluded or not isinstance(state, dict):
                continue
            raw_progress = state.get("lp")
            if raw_progress is None:
                continue
            try:
                progress = int(raw_progress)
            except (TypeError, ValueError):
                continue
            if progress < 100:
                return course_id
        return ""

    def _on_task_completed(
        self,
        task: dict[str, Any],
        detail: dict[str, Any],
    ) -> None:
        if self.auto_next_course:
            return

        task_info = task.get("taskInfo") or {}
        task_id = task_info.get("id")
        task_title = task_info.get("title") or "<未命名任务>"
        completed_courses = self._task_completed_courses(task_id, detail)
        next_target = self._next_task_target(task_id)

        ok = self.notifier.send_task_completed(
            app_id=self.config.app_id,
            token_prefix=self.config.token_prefix,
            device_id=self.config.device_id,
            session_id=self.session_id,
            task_title=task_title,
            completed_courses=completed_courses,
            next_target=next_target,
            id_card=self.id_card,
            name=self.name,
        )
        if not ok:
            self.logger.warning("任务完成的企业微信推送失败")

        if next_target:
            self._mark_account_stopped("task_completed")
            raise _TaskCompletedAwaitResume(
                f"任务“{task_title}”已完成，等待人工恢复继续下一门"
            )

    def _task_completed_courses(
        self,
        task_id: str,
        detail: dict[str, Any],
    ) -> list[str]:
        learned = (
            detail.get("data", {})
            .get("mytaskInfo", {})
            .get("learned")
            or {}
        )
        completed_ids: list[str] = []
        for course_id, state in learned.items():
            if not isinstance(state, dict):
                continue
            try:
                progress = int(state.get("lp", 0))
            except (TypeError, ValueError):
                continue
            if progress >= 100:
                completed_ids.append(str(course_id))
        if not completed_ids:
            return []

        titles = self._course_titles(task_id, detail, completed_ids)
        return [titles.get(course_id, course_id) for course_id in completed_ids]

    def _course_titles(
        self,
        task_id: str,
        detail: dict[str, Any],
        course_ids: list[str],
    ) -> dict[str, str]:
        if not course_ids:
            return {}

        task_info = detail.get("data", {}).get("taskInfo") or {}
        by_config: dict[str, list[str]] = {}
        for course_config in task_info.get("courseConfig") or []:
            if not isinstance(course_config, dict):
                continue
            course_list_id = course_config.get("id") or course_config.get("_id")
            if not course_list_id:
                continue
            config_course_ids = {
                str(course_id)
                for course_id in course_config.get("courseIds") or []
            }
            for group in course_config.get("courseGroup") or []:
                if isinstance(group, dict):
                    config_course_ids.update(
                        str(course_id) for course_id in group.get("courseIds") or []
                    )
            matched = [cid for cid in course_ids if cid in config_course_ids]
            if matched:
                by_config[str(course_list_id)] = matched

        titles: dict[str, str] = {}
        for course_list_id, ids in by_config.items():
            response = self._get_course_list(
                task_id,
                course_list_id,
                ids,
                page_size=max(30, len(ids)),
            )
            items = response.get("data", {}).get("list") or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                course_info = item.get("courseInfo") or {}
                if not isinstance(course_info, dict):
                    continue
                course_id = course_info.get("id") or course_info.get("_id")
                title = course_info.get("title")
                if course_id and title:
                    titles[str(course_id)] = str(title)
        return titles

    def _next_task_target(self, current_task_id: str) -> str | None:
        seen_current = False
        for task in self.tasks:
            task_info = task.get("taskInfo") or {}
            task_id = task_info.get("id")
            if not task_id:
                continue
            if not seen_current:
                if str(task_id) == str(current_task_id):
                    seen_current = True
                continue

            detail = self._get_task_detail(str(task_id))
            mytask_info = detail.get("data", {}).get("mytaskInfo") or {}
            if mytask_info.get("synced") in (1, "1", True):
                continue
            if ((detail.get("data", {}).get("taskInfo") or {}).get("examId")
                    and mytask_info.get("examStatus") in (0, "0")):
                return f"{task_info.get('title') or '<未命名任务>'}：检查正式考试状态"

            course_id = self._next_unfinished_course_id(detail)
            if not course_id:
                _, course_id = self._cert_watch_target(str(task_id))
            if not course_id:
                _, course_id = self._default_course_target(detail)
            if course_id:
                titles = self._course_titles(str(task_id), detail, [course_id])
                course_title = titles.get(course_id, course_id)
                return f"{task_info.get('title') or '<未命名任务>'}：{course_title}"
        return None

    def _find_course_group(
        self,
        detail: dict[str, Any],
        course_id: str,
    ) -> tuple[str, list[str]]:
        task_info = detail.get("data", {}).get("taskInfo") or {}
        course_configs = task_info.get("courseConfig") or []
        for course_config in course_configs:
            course_list_id = course_config.get("id") or course_config.get("_id")
            if not course_list_id:
                continue
            flat_course_ids = course_config.get("courseIds") or []
            if course_id in flat_course_ids:
                return course_list_id, [course_id]
            for group in course_config.get("courseGroup") or []:
                course_ids = group.get("courseIds") or []
                if course_id in course_ids:
                    return course_list_id, [course_id]
        raise _NoUnfinishedLearning(f"未找到课程 {course_id} 所属分组")

    def _find_target_seconds(
        self,
        course_detail: dict[str, Any],
        doc_id: str,
    ) -> int:
        learned = (
            course_detail.get("data", {})
            .get("mycourseInfo", {})
            .get("learned")
            or {}
        )
        doc_state = learned.get(doc_id) or {}
        total_time = doc_state.get("totalTime")
        if total_time is not None:
            try:
                total_time_int = int(total_time)
            except (TypeError, ValueError):
                total_time_int = 0
            if total_time_int > 0:
                return total_time_int

        doc_info = course_detail.get("data", {}).get("docInfo") or {}
        duration = doc_info.get("duration")
        if duration is not None:
            try:
                duration_int = int(duration)
            except (TypeError, ValueError):
                duration_int = 0
            if duration_int > 0:
                return duration_int

        raise RuntimeError("找不到该小节的总学习时长")

    def _find_learned_seconds(
        self,
        course_detail: dict[str, Any],
        doc_id: str,
    ) -> int:
        learned = (
            course_detail.get("data", {})
            .get("mycourseInfo", {})
            .get("learned")
            or {}
        )
        doc_state = learned.get(doc_id) or {}
        for key in ("learnedTime", "lastTime"):
            value = doc_state.get(key)
            if value is None:
                continue
            try:
                value_int = int(value)
            except (TypeError, ValueError):
                value_int = 0
            if value_int > 0:
                return value_int
        return 0

    def _resolve_current_doc_id(
        self,
        course_detail: dict[str, Any],
    ) -> str:
        data = Orchestrator._coerce_data(course_detail)
        doc_info = data.get("docInfo") or {}
        doc_id = doc_info.get("id") or doc_info.get("_id")
        if doc_id:
            return str(doc_id)

        mycourse_info = data.get("mycourseInfo") or {}
        last_doc_id = mycourse_info.get("lastDocId")
        if last_doc_id:
            return str(last_doc_id)

        raise RuntimeError("getMycourseDetail 未返回当前小节 docInfo.id")

    def _select_course_config(
        self,
        detail: dict[str, Any],
    ) -> tuple[str, str, list[str]]:
        task_info = detail.get("data", {}).get("taskInfo") or {}
        course_configs = task_info.get("courseConfig") or []
        if not course_configs:
            raise _NoUnfinishedLearning("courseConfig 为空")

        for course_config in course_configs:
            course_group_list = course_config.get("courseGroup") or []
            if not course_group_list:
                continue
            group = course_group_list[0]
            course_ids = group.get("courseIds") or []
            if not course_ids:
                continue
            course_list_id = course_config.get("id") or course_config.get("_id")
            if not course_list_id:
                continue
            return course_list_id, course_ids[0], course_ids

        first = course_configs[0]
        course_list_id = first.get("id") or first.get("_id")
        course_ids = first.get("courseIds") or []
        if not course_list_id or not course_ids:
            raise _NoUnfinishedLearning("无法从 courseConfig 中确定课程")
        return course_list_id, course_ids[0], course_ids

    def _select_course_and_doc(
        self,
        course_list: dict[str, Any],
        *,
        course_id: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        items = course_list.get("data", {}).get("list") or []
        if not items:
            raise _NoUnfinishedLearning("课程小节列表为空")

        for item in items:
            course_info = item.get("courseInfo") or {}
            mycourse_info = item.get("mycourseInfo") or {}
            if course_id:
                item_course_id = (
                    course_info.get("id")
                    or course_info.get("_id")
                    or mycourse_info.get("courseId")
                )
                if item_course_id != course_id:
                    continue
            replay = getattr(self, "replay", False)
            if not replay and Orchestrator._course_is_finished(mycourse_info):
                continue
            body = course_info.get("body") or []
            learned = mycourse_info.get("learned") or {}
            for part in body:
                for child in part.get("child") or []:
                    doc_id = child.get("docId")
                    if not doc_id:
                        continue
                    if replay or (learned.get(doc_id) or {}).get("status") != "finish":
                        return course_info, doc_id

        raise _NoUnfinishedLearning("课程小节已全部完成")

    @staticmethod
    def _course_is_finished(mycourse_info: dict[str, Any]) -> bool:
        if mycourse_info.get("learnedStatus") == 2:
            return True
        if mycourse_info.get("learnedProgress") == 100:
            return True
        return False

    def _extract_mycourse_id(self, post_learn: dict[str, Any]) -> str:
        mycourse_info = post_learn.get("data", {}).get("mycourseInfo") or {}
        mycourse_id = mycourse_info.get("_id") or mycourse_info.get("id")
        if not mycourse_id:
            raise RuntimeError("postLearnCourse 未返回 mycourseInfo.id")
        return mycourse_id

    def _find_random(
        self,
        course_detail: dict[str, Any],
        doc_id: str,
    ) -> str:
        data = Orchestrator._coerce_data(course_detail)
        doc_info = data.get("docInfo") or {}
        if "random" in doc_info:
            return str(doc_info["random"])

        mycourse_info = data.get("mycourseInfo") or {}
        learned = mycourse_info.get("learned") or {}
        doc_state = learned.get(doc_id) or {}
        if "random" in doc_state:
            return str(doc_state["random"])

        for candidate in (
            data.get("courseInfo") or {},
            mycourse_info,
            data,
        ):
            if isinstance(candidate, dict) and "random" in candidate:
                return str(candidate["random"])
        raise RuntimeError("courseDetail 中未找到 random")

    def _load_segments(self, m3u8_url: str) -> list:
        self.logger.info("加载 m3u8：%s", m3u8_url)
        content = self._get_video_bytes_with_notify(m3u8_url).decode(
            "utf-8",
            "replace",
        )
        segments = parse_m3u8(content, m3u8_url)
        self.logger.info("m3u8 加载完成，共 %s 个视频分片", len(segments))
        self._record(
            event_type="video_playlist",
            method="GET",
            host=self.config.video_base_url,
            path=m3u8_url,
            status_code=200,
            request_payload=None,
            response_summary={"size_bytes": len(content)},
        )
        return segments

    def _play_and_heartbeat(
        self,
        *,
        mycourse_id: str,
        doc_id: str,
        study_token: str,
        random_value: str,
        segments: list,
        point_code: str,
        qrcode_url: str | None = None,
        target_seconds: int,
        resume_seconds: int = 0,
    ) -> None:
        interval = self.config.heartbeat_interval_seconds
        start_monotonic = time.monotonic()
        segment_index = 0
        next_segment_at = start_monotonic
        next_heartbeat_at = start_monotonic + interval
        update_number = 61

        self.logger.info(
            "开始播放与心跳循环，目标时长：%s 秒",
            target_seconds,
        )

        while True:
            now = time.monotonic()
            wait_until = min(next_segment_at, next_heartbeat_at)
            if wait_until > now:
                time.sleep(min(wait_until - now, 1.0))
                continue

            if next_segment_at <= time.monotonic() and segment_index < len(segments):
                segment = segments[segment_index]
                self._download_segment(segment.url)
                segment_index += 1
                next_segment_at += segment.duration
                continue

            if next_heartbeat_at <= time.monotonic():
                elapsed_seconds = max(0, int(time.monotonic() - start_monotonic))
                reported_seconds = min(resume_seconds + elapsed_seconds, target_seconds)
                final_heartbeat = resume_seconds + elapsed_seconds >= target_seconds
                payload = {
                    "mycourseId": mycourse_id,
                    "docId": doc_id,
                    "updateNumber": update_number,
                    "lastTime": reported_seconds,
                    "random": random_value,
                    "type": "normal",
                    "docPlayTime": reported_seconds,
                    "docPlayEnd": final_heartbeat,
                    "studyToken": study_token,
                    "appId": self.config.app_id,
                }
                response = self._post_json(
                    "/api/mycourse/postUpdateTimeGuangdong",
                    payload,
                    event_type="postUpdateTimeGuangdong",
                )
                data = self._coerce_data(response)

                message = self._response_status_message(response)
                if self._is_daily_limit_message(message):
                    notified = self._send_daily_limit_notification(message)
                    self._mark_daily_limit()
                    raise _DailyLimitReached(
                        message,
                        already_notified=notified,
                    )

                if self._is_update_interval_too_short(message):
                    wait_seconds = int(data.get("waitSeconds") or 60)
                    update_number = 61 + wait_seconds
                    self.logger.warning(
                        "更新间隔太短，等待 %s 秒后继续心跳",
                        wait_seconds,
                    )
                    time.sleep(wait_seconds)
                    continue

                if self._heartbeat_needs_restart(response):
                    self.logger.warning("心跳返回业务提示：%s", message)
                    raise _RestartLearning(message)

                if data.get("needPoint") is True or data.get("code") == 2:
                    self._handle_qr_verification(
                        mycourse_id=mycourse_id,
                        study_token=study_token,
                        trigger_data=data,
                        fallback_point_code=point_code,
                        fallback_qrcode_url=qrcode_url,
                    )
                    next_heartbeat_at = time.monotonic() + interval
                    continue

                if data.get("code") == -20:
                    wait_seconds = int(data.get("waitSeconds") or 1)
                    update_number = 61 + wait_seconds
                    self.logger.warning(
                        "心跳间隔过短，等待 %s 秒后重试",
                        wait_seconds,
                    )
                    time.sleep(wait_seconds)
                    continue

                if final_heartbeat:
                    self.logger.info("小节视频已完成：%s", doc_id)
                    return

                update_number = 61
                next_heartbeat_at = time.monotonic() + interval

    def _download_segment(self, url: str) -> None:
        self.logger.info("下载视频分片：%s", url)
        content = self._get_video_bytes_with_notify(url)
        self._record(
            event_type="video_segment",
            method="GET",
            host=self.config.video_base_url,
            path=url,
            status_code=200,
            request_payload=None,
            response_summary={"size_bytes": len(content)},
        )

    def _get_video_bytes_with_notify(self, url: str) -> bytes:
        try:
            return self.client.get_video_bytes(
                url,
                retries=self.config.video_max_retries,
            )
        except ApiError as exc:
            if exc.status_code == 401:
                self._signal_relogin()
            ok = self.notifier.send_video_download_failed(
                app_id=self.config.app_id,
                token_prefix=self.config.token_prefix,
                device_id=self.config.device_id,
                session_id=self.session_id,
                url=url,
                attempts=self.config.video_max_retries,
                exc=exc,
                id_card=self.id_card,
                name=self.name,
            )
            if not ok:
                self.logger.warning(
                    "视频下载失败的企业微信预警发送失败，已写入本地待发队列"
                )
            raise VideoDownloadError(
                str(exc),
                already_notified=ok,
                status_code=getattr(exc, "status_code", None),
                response_text=getattr(exc, "response_text", None),
            ) from exc

    def _signal_relogin(self) -> None:
        # 401 时删除本地账户文件并同时写 stop，避免 watch 用旧 token 反复重启。
        config = getattr(self, "config", None)
        account_dir = getattr(config, "account_dir", None)
        if account_dir is not None:
            account_path = account_dir / f"{self.id_card}.account"
            try:
                account_path.unlink(missing_ok=True)
            except OSError as exc:
                self.logger.warning(
                    "删除本地账户文件失败 %s：%s",
                    account_path,
                    exc,
                )
            else:
                self.logger.info("已删除本地账户文件：%s", account_path)

        for suffix, content in (
            ("relogin", json.dumps({"attempts": 0}, ensure_ascii=False)),
            ("stop", ""),
        ):
            key = f"hxacc/account/{self.id_card}/{suffix}"
            result = self.oss_uploader.upload_text(key, content)
            if not result.success:
                self.logger.warning(
                    "写入 %s 标记失败 %s：%s",
                    suffix,
                    key,
                    result.error,
                )
        self.logger.info(
            "视频返回 401，已删除本地账户文件并写入重新登录信号，30 秒后继续：%s",
            self.id_card,
        )
        time.sleep(30)

    def _handle_qr_verification(
        self,
        *,
        mycourse_id: str,
        study_token: str,
        trigger_data: dict[str, Any],
        fallback_point_code: str | None = None,
        fallback_qrcode_url: str | None = None,
    ) -> None:
        point_code = trigger_data.get("pointCode") or fallback_point_code
        qrcode_url = trigger_data.get("qrcodeUrl") or fallback_qrcode_url
        if not point_code or not qrcode_url:
            raise RuntimeError("心跳返回的二维码字段不完整")

        self._upload_qr_artifacts(
            point_code=point_code,
            qrcode_url=qrcode_url,
            trigger_data=trigger_data,
        )

        self._set_state("waiting_for_scan")
        self.logger.info("检测到二维码认证触发，暂停课程")
        self._gd_call(
            "gdPauseCourse",
            mycourse_id=mycourse_id,
            study_token=study_token,
            point_code=point_code,
        )

        if self.qr_state is None:
            self.qr_state = QRPageState(qrcode_url=qrcode_url)
            ready_event = threading.Event()
            self.qr_server_handle = start_qr_server(
                self.qr_state,
                self.config.qr_page_port,
                ready_event,
            )
            ready_event.wait(timeout=5)
            self.logger.info("已启动二维码页面服务")
        else:
            self.qr_state.qrcode_url = qrcode_url
            self.qr_state.verify_status = "PENDING"
            self.qr_state.message = "等待扫码认证"
            self.logger.info("复用二维码页面服务并更新二维码")

        qr_state = self.qr_state
#        暂不打开二维码
#         page_url = f"http://127.0.0.1:{self.config.qr_page_port}/qr"
#         self.logger.info("打开二维码页面：%s", page_url)
#         webbrowser.open(page_url)

        self._set_state("polling_verification")
        self._poll_until_verified(
            mycourse_id=mycourse_id,
            study_token=study_token,
            point_code=point_code,
            qr_state=qr_state,
        )
        self.logger.info("认证成功，恢复学习")

        self._gd_call(
            "gdResumeCourse",
            mycourse_id=mycourse_id,
            study_token=study_token,
            point_code=point_code,
        )
        self._gd_call(
            "gdGetPointState",
            mycourse_id=mycourse_id,
            study_token=study_token,
            point_code=point_code,
        )
        self._set_state("learning")

    def _upload_qr_artifacts(
        self,
        *,
        point_code: str,
        qrcode_url: str,
        trigger_data: dict[str, Any],
    ) -> None:
        created_at = datetime.now(CN_TZ)
        expires_at = self._qr_expires_at(trigger_data, created_at)
        self.logger.info(
            "二维码过期时间：%s",
            expires_at.astimezone(CN_TZ).isoformat(),
        )

        png_bytes = self._qr_png_bytes(qrcode_url)
        sha256 = hashlib.sha256(png_bytes).hexdigest()

        prefix = f"hxacc/account/{self.id_card}"
        png_result = self.oss_uploader.upload_bytes(
            f"{prefix}/{point_code}.png",
            png_bytes,
        )
        if not png_result.success:
            self._notify_qr_upload_failed(png_result.key, png_result.error)

        json_payload = json.dumps(
            {
                "task_id": point_code,
                "created_at": created_at.isoformat(),
                "expires_at": expires_at.astimezone(CN_TZ).isoformat(),
                "sha256": sha256,
            },
            ensure_ascii=False,
        )
        json_result = self.oss_uploader.upload_text(
            f"{prefix}/{point_code}.json",
            json_payload,
        )
        if not json_result.success:
            self._notify_qr_upload_failed(json_result.key, json_result.error)

        self.logger.info(
            "已上传二维码截图和元数据：%s",
            f"{prefix}/{point_code}",
        )

    @staticmethod
    def _qr_png_bytes(qrcode_url: str) -> bytes:
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=12,
            border=2,
        )
        qr.add_data(qrcode_url)
        qr.make(fit=True)
        modules = qr.modules
        module_count = len(modules)
        if module_count == 0:
            raise RuntimeError("二维码生成失败：模块矩阵为空")
        scale = 12
        width = len(modules[0]) * scale
        height = module_count * scale
        raw = bytearray()
        for row in modules:
            row_bytes = bytearray()
            for dark in row:
                pixel = b"\x00" if dark else b"\xff"
                row_bytes.extend(pixel * scale)
            for _ in range(scale):
                raw.append(0)
                raw.extend(row_bytes)
        return _encode_png(width, height, bytes(raw))

    def _qr_expires_at(
        self,
        trigger_data: dict[str, Any],
        created_at: datetime,
    ) -> datetime:
        check_end_time = trigger_data.get("checkEndTime")
        if isinstance(check_end_time, (int, float)) and check_end_time > 0:
            return datetime.fromtimestamp(check_end_time / 1000, tz=timezone.utc)

        validity_ms = trigger_data.get("validityMs")
        if isinstance(validity_ms, (int, float)) and validity_ms > 0:
            return created_at + timedelta(milliseconds=validity_ms)

        validity = trigger_data.get("validity")
        if isinstance(validity, (int, float)) and validity > 0:
            return created_at + timedelta(seconds=validity)

        return created_at + timedelta(seconds=1800)

    def _notify_qr_upload_failed(
        self,
        key: str,
        error: str | None,
    ) -> None:
        ok = self.notifier.send_exception(
            app_id=self.config.app_id,
            token_prefix=self.config.token_prefix,
            device_id=self.config.device_id,
            session_id=self.session_id,
            exc=RuntimeError(f"二维码文件上传失败 {key}: {error}"),
            id_card=self.id_card,
            name=self.name,
        )
        if not ok:
            self.logger.warning("二维码上传失败的企业微信推送失败，已写入待发队列")

    def shutdown_qr_server(self) -> None:
        if self.qr_server_handle is None:
            return
        self.qr_server_handle.server.should_exit = True
        self.qr_server_handle.thread.join(timeout=5)
        self.qr_server_handle = None
        self.qr_state = None

    def _poll_until_verified(
        self,
        *,
        mycourse_id: str,
        study_token: str,
        point_code: str,
        qr_state: QRPageState,
    ) -> None:
        poll_seconds = self.config.verify_poll_interval_seconds
        deadline = time.monotonic() + (30 * 60)
        while True:
            result = self._gd_call(
                "gdQueryVerificationResults",
                mycourse_id=mycourse_id,
                study_token=study_token,
                point_code=point_code,
            )
            data = self._coerce_data(result)
            verify_result = data.get("verifyResult")
            if verify_result in ("1", 1, True):
                qr_state.verify_status = "VERIFIED"
                qr_state.message = "认证成功，可以继续恢复学习。"
                return

            if time.monotonic() >= deadline:
                qr_state.verify_status = "PENDING"
                qr_state.message = "认证未通过，停止学习。"
                self._notify_verification_failed(
                    point_code=point_code,
                    waited_seconds=30 * 60,
                )
                self._mark_account_stopped()
                raise _AuthVerificationFailed(
                    "认证未通过，等待 30 分钟超时，停止学习"
                )

            qr_state.verify_status = "PENDING"
            qr_state.message = "等待扫码认证"
            self.logger.info("认证仍未完成，%s 秒后重试", poll_seconds)
            time.sleep(poll_seconds)

    def _get_task_cert_synced(self, task_id: str) -> int:
        item = self._get_task_cert_item(task_id)
        if item is None:
            return 0
        extra = item.get("extra") or {}
        if not isinstance(extra, dict):
            extra = {}
        synced = extra.get("synced")
        return 1 if synced in (1, "1", True) else 0

    def _get_task_cert_item(self, task_id: str) -> dict[str, Any] | None:
        response = self._post_json(
            "/api/mycert/getTaskCert",
            {
                "filter": {
                    "realname": self.name,
                    "idcard": self.id_card,
                }
            },
            event_type="getTaskCert",
        )
        data = response.get("data")
        items: list[dict[str, Any]] = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            raw_list = data.get("list") or data.get("items") or []
            if isinstance(raw_list, list):
                items = raw_list

        for item in items:
            if not isinstance(item, dict):
                continue
            mytask = item.get("mytask")
            if not isinstance(mytask, dict):
                source = item
            else:
                source = mytask
            item_task_id = (
                source.get("taskId")
                or source.get("id")
                or source.get("_id")
                or item.get("taskId")
                or item.get("id")
                or item.get("_id")
            )
            if str(item_task_id) != str(task_id):
                continue
            return source if mytask is not None else item
        return None

    def _cert_watch_target(self, task_id: str) -> tuple[str, str]:
        item = self._get_task_cert_item(task_id)
        if item is None:
            return "", ""
        course_list = item.get("courseList") or []
        if not isinstance(course_list, list):
            return "", ""
        for entry in course_list:
            if not isinstance(entry, dict):
                continue
            course_list_id = entry.get("id") or entry.get("_id")
            course_ids = entry.get("courseIds") or []
            if course_list_id and course_ids:
                return str(course_list_id), str(course_ids[0])
        learned = item.get("learned") or {}
        if isinstance(learned, dict) and learned:
            return "", str(next(iter(learned)))
        return "", ""

    def _is_task_completed(self, task_id: str) -> bool:
        return self._get_task_cert_synced(task_id) == 1

    @staticmethod
    def _data_learned_status(data: dict[str, Any]) -> int | None:
        if isinstance(data.get("learnedStatus"), (int, float)):
            return int(data["learnedStatus"])
        mycourse_info = data.get("mycourseInfo") or {}
        if isinstance(mycourse_info, dict):
            value = mycourse_info.get("learnedStatus")
            if isinstance(value, (int, float)):
                return int(value)
        return None

    @classmethod
    def _is_learned_complete(cls, data: dict[str, Any]) -> bool:
        return cls._data_learned_status(data) == 2

    def _mark_account_stopped(self, content: str = "") -> None:
        key = f"hxacc/account/{self.id_card}/stop"
        result = self.oss_uploader.upload_text(key, content)
        if not result.success:
            self.logger.warning(
                "写入停止标记失败 %s：%s",
                key,
                result.error,
            )

    def _mark_daily_limit(self) -> None:
        key = f"hxacc/account/{self.id_card}/daily_limit"
        result = self.oss_uploader.upload_text(
            key,
            datetime.now(CN_TZ).strftime("%Y-%m-%d"),
        )
        if not result.success:
            self.logger.warning(
                "写入每日上限标记失败 %s：%s",
                key,
                result.error,
            )

    def _default_course_target(
        self,
        detail: dict[str, Any],
    ) -> tuple[str, str]:
        mytask_info = detail.get("data", {}).get("mytaskInfo") or {}
        course_list = mytask_info.get("courseList") or []
        if isinstance(course_list, list):
            for entry in course_list:
                if not isinstance(entry, dict):
                    continue
                course_list_id = entry.get("id") or entry.get("_id")
                course_ids = entry.get("courseIds") or []
                if course_list_id and course_ids:
                    return str(course_list_id), str(course_ids[0])

        task_info = detail.get("data", {}).get("taskInfo") or {}
        course_configs = task_info.get("courseConfig") or []
        learned = mytask_info.get("learned") or {}

        def is_finished(course_id: str) -> bool:
            state = learned.get(course_id)
            if not isinstance(state, dict):
                return False
            try:
                return int(state.get("lp", 0)) >= 100
            except (TypeError, ValueError):
                return False

        for course_config in course_configs:
            if not isinstance(course_config, dict):
                continue
            course_list_id = course_config.get("id") or course_config.get("_id")
            if not course_list_id:
                continue
            flat_course_ids = course_config.get("courseIds") or []
            unfinished = [
                str(course_id)
                for course_id in flat_course_ids
                if not is_finished(str(course_id))
            ]
            if unfinished:
                return str(course_list_id), unfinished[0]
            for group in course_config.get("courseGroup") or []:
                if not isinstance(group, dict):
                    continue
                group_course_ids = group.get("courseIds") or []
                unfinished = [
                    str(course_id)
                    for course_id in group_course_ids
                    if not is_finished(str(course_id))
                ]
                if unfinished:
                    return str(course_list_id), unfinished[0]
        return "", ""

    def _notify_verification_failed(
        self,
        *,
        point_code: str,
        waited_seconds: int,
    ) -> None:
        content = "\n".join(
            [
                "🚫 认证未通过，已停止学习",
                "",
                f"时间：{datetime.now(CN_TZ):%Y-%m-%d %H:%M:%S}",
                f"idCard：{self.id_card}",
                f"姓名：{self.name}",
                f"pointCode：{point_code}",
                f"已等待：{waited_seconds // 60} 分钟",
                "",
                "请解决认证问题后恢复学习：",
                f".venv/bin/python -m integration_harness resume --id-card {self.id_card}",
            ]
        )
        ok = self.notifier.send_markdown(content)
        if not ok:
            self.logger.warning("认证未通过的企业微信推送失败，已写入本地待发队列")

    @staticmethod
    def _response_status_message(data: dict[str, Any]) -> str:
        status = data.get("status")
        if isinstance(status, dict):
            message = status.get("message")
            if isinstance(message, str):
                return message
        return ""

    @staticmethod
    def _coerce_data(response: dict[str, Any]) -> dict[str, Any]:
        data = response.get("data")
        if data is None or data == {}:
            return {}
        if not isinstance(data, dict):
            logger.warning("响应 data 不是对象，已按空数据继续：%r", data)
            return {}
        return data

    @staticmethod
    def _heartbeat_needs_restart(response: dict[str, Any]) -> bool:
        message = Orchestrator._response_status_message(response)
        return bool(message and message != "SUCCESS")

    @staticmethod
    def _is_daily_limit_message(message: str) -> bool:
        return (
            "学习时长已经超过" in message
            and "不继续累计时长" in message
        )

    @staticmethod
    def _is_update_interval_too_short(message: str) -> bool:
        return "更新间隔太短" in message or "请稍后再试" in message

    def _send_daily_limit_notification(self, message: str) -> bool:
        ok = self.notifier.send_daily_limit(
            app_id=self.config.app_id,
            token_prefix=self.config.token_prefix,
            device_id=self.config.device_id,
            session_id=self.session_id,
            message=message,
            id_card=self.id_card,
            name=self.name,
        )
        if not ok:
            self.logger.warning(
                "学习时长上限的企业微信推送失败，已写入本地待发队列"
            )
        else:
            self.logger.info("已推送学习时长上限通知")
        return ok

    @staticmethod
    def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
        redacted = dict(payload)
        study_token = redacted.get("studyToken")
        if isinstance(study_token, str) and study_token:
            redacted["studyToken"] = study_token[:8] + "…"
        return redacted

    @staticmethod
    def _summarize(data: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        status = data.get("status") or {}
        if isinstance(status, dict):
            summary["status_code"] = status.get("code")
            summary["status_message"] = status.get("message")

        nested_data = data.get("data") or {}
        if isinstance(nested_data, dict):
            for key in (
                "needPoint",
                "verifyStatus",
                "verifyResult",
                "learnStatus",
                "studyDuration",
                "pointCode",
                "qrcodeUrl",
                "code",
                "message",
                "action",
            ):
                if key in nested_data:
                    value = nested_data[key]
                    if key == "qrcodeUrl":
                        value = str(value)[:80]
                    summary[key] = value
        return summary

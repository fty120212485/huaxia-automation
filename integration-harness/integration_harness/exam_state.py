"""Durable exam checkpoints shared through the account's existing OSS lease."""
from __future__ import annotations

import hashlib
import json
import logging

import oss2
import os
import time
from pathlib import Path


class ExamStateStore:
    def __init__(self, root: Path, account_id: str, task_id: str, exam_id: str, uploader):
        self.identity = {"taskId": task_id, "examId": exam_id, "examType": "normal"}
        digest = hashlib.sha256(f"{account_id}/{task_id}/{exam_id}".encode()).hexdigest()
        self.path = root / "exam_states" / f"{digest}.json"
        self.key = f"hxacc/account/{account_id}/exam/{digest}.json"
        self.uploader = uploader

    def load(self) -> dict:
        candidates = []
        if self.path.exists():
            candidates.append(json.loads(self.path.read_text(encoding="utf-8")))
        # Do not interpret an unavailable remote checkpoint as a new exam.
        if self.uploader.object_exists(self.key):
            try:
                remote = self.uploader.get_object_text(self.key)
            except oss2.exceptions.NotFound as exc:
                # HEAD and GET can disagree. Only an absent object is recoverable;
                # bucket/configuration failures must still stop reconciliation.
                if type(exc) not in (oss2.exceptions.NotFound, oss2.exceptions.NoSuchKey):
                    raise
                logging.getLogger(__name__).warning(
                    "OSS 考试恢复记录读取返回 404；本地记录=%s。继续查询服务端考试状态，不据此重新开考。",
                    bool(candidates),
                )
            else:
                candidates.append(json.loads(remote))
        for state in candidates:
            if not isinstance(state, dict) or any(state.get(k) != v for k, v in self.identity.items()):
                raise RuntimeError("考试恢复记录身份不匹配")
        return max(candidates, key=lambda s: (s.get("revision", 0), s.get("updatedAtNs", 0))) if candidates else dict(self.identity)

    def save(self, state: dict) -> None:
        state.update(self.identity)
        state["revision"] = int(state.get("revision", 0)) + 1
        state["updatedAtNs"] = time.time_ns()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(state, ensure_ascii=False)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)
        result = self.uploader.upload_text(self.key, content)
        if not result.success:
            raise RuntimeError("考试恢复记录上传失败，已保留本地记录，暂停后续操作")

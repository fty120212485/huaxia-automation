from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .oss_runtime import ReadCache, call, file_size, seconds

try:
    import oss2
except Exception:  # pragma: no cover - exercised when dependency is missing
    oss2 = None


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OssDeleteResult:
    prefix: str
    deleted_count: int
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None


class OssClient:
    def __init__(
        self,
        *,
        access_key_id: str,
        access_key_secret: str,
        bucket_name: str,
        endpoint: str,
    ) -> None:
        if oss2 is None:
            raise RuntimeError("oss2 is not installed")
        auth = oss2.Auth(access_key_id, access_key_secret)
        self.bucket = oss2.Bucket(
            auth,
            endpoint,
            bucket_name,
            proxies={"http": None, "https": None},
        )

    def delete_prefix(self, prefix: str) -> OssDeleteResult:
        if not prefix.endswith("/"):
            prefix += "/"
        deleted = 0
        try:
            result = call("LIST", prefix, lambda: self.bucket.list_objects(prefix=prefix))
            for obj in result.object_list:
                if obj.key.endswith("/"):
                    continue
                call("DELETE", obj.key, lambda: self.bucket.delete_object(obj.key))
                deleted += 1
            return OssDeleteResult(prefix=prefix, deleted_count=deleted)
        except Exception as exc:
            return OssDeleteResult(
                prefix=prefix,
                deleted_count=deleted,
                error=f"{type(exc).__name__}: {exc}",
            )

    def object_exists(self, key: str) -> bool:
        try:
            return bool(call("EXISTS", key, lambda: self.bucket.object_exists(key)))
        except Exception:
            return False

    def list_prefixes(self, prefix: str) -> list[str]:
        if not hasattr(self, "_prefix_cache"):
            self._prefix_cache = ReadCache()
        return list(self._prefix_cache.get(
            prefix, seconds("OSS_DIRECTORY_CACHE_SECONDS", 60),
            lambda: tuple(self._list_prefixes(prefix)),
        ))

    def _list_prefixes(self, prefix: str) -> list[str]:
        try:
            prefixes: list[str] = []
            marker = ""
            while True:
                result = call("LIST", prefix, lambda: self.bucket.list_objects(
                    prefix=prefix, delimiter="/", marker=marker, max_keys=1000
                ))
                prefixes.extend(
                    item if isinstance(item, str) else item.prefix
                    for item in result.prefix_list
                )
                if not result.is_truncated:
                    return prefixes
                next_marker = result.next_marker
                if not next_marker or next_marker == marker:
                    raise RuntimeError("OSS 目录分页未返回有效的下一页标记")
                marker = next_marker
        except Exception:
            logger.warning("OSS 列举目录失败，下轮重试")
            raise

    def get_optional_text(self, key: str) -> str | None:
        """Read once; only a missing object is treated as absent."""
        try:
            return call("GET", key, lambda: self.bucket.get_object(key).read()).decode("utf-8")
        except oss2.exceptions.NoSuchKey:
            return None

    def get_object_text(self, key: str) -> str:
        try:
            return call("GET", key, lambda: self.bucket.get_object(key).read()).decode("utf-8")
        except Exception:
            return ""

    def delete_object(self, key: str) -> bool:
        try:
            call("DELETE", key, lambda: self.bucket.delete_object(key))
            return True
        except Exception:
            return False

    def upload_text(self, key: str, content: str) -> None:
        logger.info("OSS 上传开始：%s", key)
        try:
            call("PUT", key, lambda: self.bucket.put_object(key, content.encode("utf-8")),
                 uploaded=len(content.encode("utf-8")))
        except Exception:
            logger.exception("OSS 上传失败：%s", key)
            raise
        logger.info("OSS 上传成功：%s", key)

    def upload_file(self, file_path: Path, key: str) -> None:
        logger.info("OSS 上传文件开始：%s -> %s", file_path, key)
        try:
            call("PUT", key, lambda: self.bucket.put_object_from_file(key, str(file_path)),
                 uploaded=file_size(file_path))
        except Exception:
            logger.exception("OSS 上传文件失败：%s", key)
            raise
        logger.info("OSS 上传文件成功：%s", key)


def account_oss_prefix(account_dir: str, id_card: str) -> str:
    base = Path(account_dir)
    return str(base / id_card)

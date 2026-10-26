from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .oss_runtime import ReadCache, call, file_size, seconds

import oss2
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


@dataclass(frozen=True)
class OssUploadResult:
    key: str
    success: bool
    error: str | None = None


class OssAccountUploader:
    def __init__(
        self,
        *,
        access_key_id: str,
        access_key_secret: str,
        bucket_name: str,
        endpoint: str,
        connect_timeout: int = 10,
    ) -> None:
        auth = oss2.Auth(access_key_id, access_key_secret)
        self.bucket = oss2.Bucket(
            auth,
            endpoint,
            bucket_name,
            connect_timeout=connect_timeout,
            proxies={"http": None, "https": None},
        )
        self.bucket.session.session.verify = False

    def upload_file(self, file_path: Path, key: str) -> OssUploadResult:
        try:
            call("PUT", key, lambda: self.bucket.put_object_from_file(key, str(file_path)),
                 uploaded=file_size(file_path))
            return OssUploadResult(key=key, success=True)
        except oss2.exceptions.OssError as exc:
            return OssUploadResult(key=key, success=False, error=str(exc))
        except Exception as exc:
            return OssUploadResult(
                key=key,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )

    def upload_text(self, key: str, content: str = "") -> OssUploadResult:
        try:
            call("PUT", key, lambda: self.bucket.put_object(key, content.encode("utf-8")),
                 uploaded=len(content.encode("utf-8")))
            return OssUploadResult(key=key, success=True)
        except oss2.exceptions.OssError as exc:
            return OssUploadResult(key=key, success=False, error=str(exc))
        except Exception as exc:
            return OssUploadResult(
                key=key,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )

    def upload_bytes(self, key: str, content: bytes) -> OssUploadResult:
        try:
            call("PUT", key, lambda: self.bucket.put_object(key, content), uploaded=len(content))
            return OssUploadResult(key=key, success=True)
        except oss2.exceptions.OssError as exc:
            return OssUploadResult(key=key, success=False, error=str(exc))
        except Exception as exc:
            return OssUploadResult(
                key=key,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )

    def list_prefixes(self, prefix: str) -> list[str]:
        if not hasattr(self, "_prefix_cache"):
            self._prefix_cache = ReadCache()
        return list(self._prefix_cache.get(
            prefix, seconds("OSS_DIRECTORY_CACHE_SECONDS", 60),
            lambda: tuple(self._list_prefixes(prefix)),
        ))

    def _list_prefixes(self, prefix: str) -> list[str]:
        prefixes: list[str] = []
        marker = ""
        seen: set[str] = set()
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
            marker = result.next_marker
            if not marker or marker in seen:
                raise RuntimeError("OSS 目录分页未返回有效的下一页标记")
            seen.add(marker)

    def object_exists(self, key: str) -> bool:
        return call("EXISTS", key, lambda: self.bucket.object_exists(key))

    def get_object_text(self, key: str) -> str:
        return call("GET", key, lambda: self.bucket.get_object(key).read()).decode("utf-8")

    def get_optional_text(self, key: str) -> str | None:
        """Read once; propagate failures other than a missing object."""
        try:
            return self.get_object_text(key)
        except oss2.exceptions.NoSuchKey:
            return None

    def delete_object(self, key: str) -> OssUploadResult:
        try:
            call("DELETE", key, lambda: self.bucket.delete_object(key))
            return OssUploadResult(key=key, success=True)
        except Exception as exc:
            return OssUploadResult(
                key=key,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )

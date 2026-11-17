"""Filesystem object storage shared by all local workers. No network fallback."""
from __future__ import annotations
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile


@dataclass(frozen=True)
class DeleteResult:
    prefix: str
    deleted_count: int
    error: str | None = None

    @property
    def success(self):
        return self.error is None


class FileStore:
    def __init__(self, root):
        configured = Path(root).expanduser()
        if configured.is_symlink():
            raise ValueError('共享存储根目录不能是符号链接')
        self.root = configured.resolve()
        # Only migration/initialization may create the shared root. A typo or
        # missing mount must not silently create a worker-specific store.
        marker = self.root / '.hxacc-store.json'
        if not marker.is_file() or json.loads(marker.read_text()).get('format') != 1:
            raise RuntimeError('共享目录未初始化，请先迁移或初始化 Local 存储')
        self._held_locks = {}

    def path(self, key):
        if '\\' in key or key.startswith('/') or '..' in PurePosixPath(key).parts:
            raise ValueError('非法存储路径')
        if key == 'hxacc' or key == 'hxacc/':
            return self.root
        if not key.startswith('hxacc/'):
            raise ValueError('存储 key 必须以 hxacc/ 开头')
        current = self.root
        for part in PurePosixPath(key[len('hxacc/'):]).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError('共享存储对象路径不能包含符号链接')
        target = (self.root / key[len('hxacc/'):]).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError('存储路径越界')
        return target

    def object_exists(self, key):
        return self.path(key).is_file()

    def list_prefixes(self, prefix):
        directory = self.path(prefix)
        if not directory.exists():
            return []
        return [prefix.rstrip('/') + '/' + p.name + '/' for p in sorted(directory.iterdir())
                if p.is_dir() and not p.is_symlink() and not p.name.startswith('.')]

    def get_object_text(self, key):
        return self.path(key).read_text(encoding='utf-8')

    def get_optional_text(self, key):
        try:
            return self.get_object_text(key)
        except FileNotFoundError:
            return None

    def upload_bytes(self, key, content):
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.hx-write-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def upload_text(self, key, content=''):
        self.upload_bytes(key, content.encode('utf-8'))

    def upload_file(self, file_path, key):
        # Never replace a file with itself, particularly the shared credentials.
        if Path(file_path).resolve() != self.path(key):
            self.upload_bytes(key, Path(file_path).read_bytes())

    def delete_object(self, key):
        self.path(key).unlink(missing_ok=True)
        return True

    def delete_prefix(self, prefix):
        directory = self.path(prefix.rstrip('/') + '/')
        if directory == self.root:
            raise ValueError('不允许删除整个共享存储根目录')
        count = 0
        try:
            if directory.exists():
                for path in directory.rglob('*'):
                    if path.is_symlink():
                        raise ValueError('共享目录包含符号链接')
                for path in sorted(directory.rglob('*'), key=lambda p: len(p.parts), reverse=True):
                    if path.is_file():
                        path.unlink(); count += 1
                    elif path.is_dir():
                        path.rmdir()
                directory.rmdir()
            return DeleteResult(prefix, count)
        except Exception as exc:
            return DeleteResult(prefix, count, type(exc).__name__)

    def acquire_account_lock(self, account):
        if account in self._held_locks:
            return True
        directory = self.root / '.locks'
        directory.mkdir(exist_ok=True)
        name = hashlib.sha256(account.encode()).hexdigest()
        fd = os.open(directory / (name + '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._held_locks[account] = fd
        return True

    def release_account_lock(self, account):
        fd = self._held_locks.pop(account, None)
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def close(self):
        for account in list(self._held_locks):
            self.release_account_lock(account)

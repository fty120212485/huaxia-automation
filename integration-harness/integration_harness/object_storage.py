from pathlib import Path
from .local_store import FileStore
from .oss_upload import OssAccountUploader, OssUploadResult


class LocalAccountUploader(FileStore):
    def upload_bytes(self, key, content):
        try:
            FileStore.upload_bytes(self, key, content)
            return OssUploadResult(key, True)
        except Exception as exc:
            return OssUploadResult(key, False, type(exc).__name__)

    def upload_text(self, key, content=''):
        return self.upload_bytes(key, content.encode('utf-8'))

    def upload_file(self, file_path, key):
        try:
            if Path(file_path).resolve() == self.path(key):
                return OssUploadResult(key, True)
            return self.upload_bytes(key, Path(file_path).read_bytes())
        except Exception as exc:
            return OssUploadResult(key, False, type(exc).__name__)

    def delete_object(self, key):
        process = key.endswith('.process')
        account = Path(key).stem
        if process and not self.acquire_account_lock(account):
            return OssUploadResult(key, False, '账号仍被另一个进程持有')
        try:
            FileStore.delete_object(self, key)
            return OssUploadResult(key, True)
        except Exception as exc:
            return OssUploadResult(key, False, type(exc).__name__)
        finally:
            if process:
                self.release_account_lock(account)


def make_storage(config, *, oss_factory=None):
    if getattr(config, 'storage_mode', 'OSS') == 'Local':
        return LocalAccountUploader(config.shared_root)
    factory = oss_factory or OssAccountUploader
    return factory(access_key_id=config.oss_access_key_id,
                   access_key_secret=config.oss_access_key_secret,
                   bucket_name=config.oss_bucket, endpoint=config.oss_endpoint)

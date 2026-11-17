from .local_store import FileStore
from .oss import OssClient


def enabled(config):
    return getattr(config, 'storage_mode', 'OSS') == 'Local' or bool(
        config.oss_bucket and config.oss_access_key_id and config.oss_access_key_secret)


def make_storage(config):
    if getattr(config, 'storage_mode', 'OSS') == 'Local':
        return FileStore(config.shared_root)
    return OssClient(access_key_id=config.oss_access_key_id,
                     access_key_secret=config.oss_access_key_secret,
                     bucket_name=config.oss_bucket, endpoint=config.oss_endpoint)

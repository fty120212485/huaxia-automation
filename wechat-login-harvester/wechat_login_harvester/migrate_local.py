"""Explicit OSS pre-copy. Does not change configuration or remove OSS data."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

from .config import load_config
from .local_store import FileStore
from .web_login import private_json


def main():
    parser = argparse.ArgumentParser(description='同步 OSS 资料到 Local；切换前应暂停登录端和学习端写入')
    parser.add_argument('--root', type=Path, default=Path('/Users/fty/共享/hxacc'))
    args = parser.parse_args()
    config = load_config()
    import oss2
    bucket = oss2.Bucket(oss2.Auth(config.oss_access_key_id, config.oss_access_key_secret),
                        config.oss_endpoint, config.oss_bucket, proxies={'http': None, 'https': None})
    args.root.mkdir(parents=True, exist_ok=True)
    marker = args.root / '.hxacc-store.json'
    if not marker.exists():
        private_json(marker, {'format': 1, 'id': uuid.uuid4().hex})
    target = FileStore(args.root)
    manifest_path = args.root/'.migration-login-learning.json'
    previous = json.loads(manifest_path.read_text()).get('objects', {}) if manifest_path.exists() else {}
    entries = [x for x in oss2.ObjectIterator(bucket, prefix='hxacc/account/') if not x.key.endswith('/')]
    finished = {x.key.split('/')[2] for x in entries if x.key.endswith('/finished')}
    selected = [x for x in entries if not x.key.endswith(('.DS_Store', '.process')) and (
        x.key.split('/')[2] not in finished or x.key.endswith('.account') or
        x.key.rsplit('/', 1)[-1] in {'account.json', 'finished', 'stop', 'daily_limit', 'relogin', 'auto_next'} or
        '/exam/' in x.key)]
    summary = {'copied': 0, 'same': 0, 'preserved_local': 0, 'failed': 0, 'credentials': 0}
    manifest = {}
    lock = threading.Lock()

    def copy(item):
        try:
            response = bucket.get_object(item.key, headers={'If-Match': item.etag})
            try:
                content = response.read()
            finally:
                response.close()
            if len(content) != item.size:
                raise ValueError('source size changed')
            digest = hashlib.sha256(content).hexdigest()
            path = target.path(item.key)
            result = 'copied'
            if path.exists():
                existing = path.read_bytes()
                result = 'same' if existing == content else 'preserved_local'
                old = previous.get(item.key, {})
                if result == 'preserved_local' and hashlib.sha256(existing).hexdigest() == old.get('sha256'):
                    target.upload_bytes(item.key, content)
                    result = 'copied'
                # Never overwrite work another participant has published locally.
            else:
                target.upload_bytes(item.key, content)
            actual = path.read_bytes()
            if result != 'preserved_local' and hashlib.sha256(actual).hexdigest() != digest:
                raise ValueError('destination verification failed')
            if item.key.endswith('.account'):
                payload = json.loads(actual)
                ident = item.key.split('/')[2]
                if not all(payload.get(k) for k in ('appId', 'token', 'deviceId', 'name')):
                    raise ValueError('incomplete credentials')
                if str(payload.get('idCard', ident)).upper() != ident.upper():
                    raise ValueError('identity mismatch')
                credential = args.root/'credentials'/f'{ident}.account'
                if not credential.exists():
                    private_json(credential, payload)
                elif hashlib.sha256(credential.read_bytes()).hexdigest() == previous.get(item.key, {}).get('sha256'):
                    private_json(credential, payload)
                with lock:
                    summary['credentials'] += 1
            with lock:
                summary[result] += 1
                manifest[item.key] = {'source_etag': item.etag, 'sha256': hashlib.sha256(actual).hexdigest(),
                                      'size': len(actual), 'result': result}
        except Exception as exc:
            with lock:
                summary['failed'] += 1
                manifest[item.key] = {'error': type(exc).__name__}

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(copy, selected))
    if bucket.object_exists('hxacc/workers') and not target.object_exists('hxacc/workers'):
        target.upload_bytes('hxacc/workers', bucket.get_object('hxacc/workers').read())
    # Remove only stale markers created by this migration, preserving a backup.
    current_keys = {item.key for item in selected}
    for key, old in previous.items():
        if key in current_keys or key.rsplit('/', 1)[-1] not in {'stop', 'finished', 'daily_limit', 'relogin', 'auto_next'}:
            continue
        path = target.path(key)
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == old.get('sha256'):
            backup = args.root/'.migration-removed'/str(int(time.time()))/path.relative_to(args.root)
            backup.parent.mkdir(parents=True, exist_ok=True)
            path.replace(backup)
    private_json(manifest_path, {
        'summary': summary, 'objects': manifest, 'completed_at': time.time(),
        'skipped_completed_history': len(entries)-len(selected)})
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if summary['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())

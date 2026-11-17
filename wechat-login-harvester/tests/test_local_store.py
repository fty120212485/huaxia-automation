from pathlib import Path
from types import SimpleNamespace
import json
import multiprocessing

import pytest

from wechat_login_harvester.local_store import FileStore
from wechat_login_harvester.storage import make_storage


def root(tmp_path):
    path = tmp_path/'shared'
    path.mkdir()
    (path/'.hxacc-store.json').write_text('{"format":1}')
    return path


def test_local_uses_existing_shared_root_without_oss(tmp_path, monkeypatch):
    shared = root(tmp_path)
    cfg = SimpleNamespace(storage_mode='Local', shared_root=shared)
    monkeypatch.setattr('wechat_login_harvester.storage.OssClient', lambda **kw: pytest.fail('OSS constructed'))
    store = make_storage(cfg)
    store.upload_text('hxacc/account/id/account.json', '{"name":"测试"}')
    assert (shared/'account/id/account.json').is_file()
    assert not (shared/'hxacc').exists()
    assert store.list_prefixes('hxacc/account/') == ['hxacc/account/id/']
    assert store.get_optional_text('hxacc/account/id/missing') is None
    assert store.object_exists('hxacc/account/id/account.json')
    assert (shared/'account/id/account.json').stat().st_mode & 0o777 == 0o600


def test_unavailable_root_does_not_create_worker_private_storage(tmp_path):
    missing = tmp_path/'missing'
    with pytest.raises(RuntimeError):
        FileStore(missing)
    assert not missing.exists()


@pytest.mark.parametrize('key', ['../escape', '/tmp/escape', 'hxacc/../../escape', 'hxacc/a\\b', 'other/account'])
def test_path_traversal_is_rejected(tmp_path, key):
    store = FileStore(root(tmp_path))
    with pytest.raises(ValueError):
        store.upload_text(key, 'secret')


def test_symlink_outside_root_is_rejected(tmp_path):
    shared = root(tmp_path)
    external = tmp_path/'outside';external.mkdir()
    (shared/'alias').symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError):
        FileStore(shared).upload_text('hxacc/alias/secret', 'token')
    assert not (external/'secret').exists()


def test_two_instances_see_atomic_replacement_and_status_deletion(tmp_path):
    shared = root(tmp_path)
    one, two = FileStore(shared), FileStore(shared)
    one.upload_text('hxacc/account/id/stop', 'stop')
    assert two.get_object_text('hxacc/account/id/stop') == 'stop'
    one.upload_text('hxacc/account/id/stop', 'updated')
    assert two.get_object_text('hxacc/account/id/stop') == 'updated'
    assert one.delete_object('hxacc/account/id/stop')
    assert two.get_optional_text('hxacc/account/id/stop') is None
    one.upload_bytes('hxacc/account/id/qr.png', b'png')
    assert two.delete_prefix('hxacc/account/id/').success
    assert one.list_prefixes('hxacc/account/') == []


def test_process_lock_excludes_another_instance_and_releases(tmp_path):
    shared = root(tmp_path)
    one, two = FileStore(shared), FileStore(shared)
    assert one.acquire_account_lock('id')
    assert not two.acquire_account_lock('id')
    one.release_account_lock('id')
    assert two.acquire_account_lock('id')
    two.close()
    assert one.acquire_account_lock('id')
    one.close()


def test_config_local_overrides_account_dir(tmp_path, monkeypatch):
    from wechat_login_harvester.config import load_config
    monkeypatch.chdir(tmp_path)
    (tmp_path/'user').write_text('[{"name":"测试","id_card":"id"}]')
    monkeypatch.setenv('STORAGE_MODE','Local')
    monkeypatch.setenv('SHARED_ROOT',str(tmp_path/'shared'))
    monkeypatch.setenv('ACCOUNT_DIR',str(tmp_path/'worker-private'))
    cfg=load_config()
    assert cfg.account_dir == tmp_path/'shared'/'credentials'
    assert cfg.storage_mode == 'Local'


def test_symlink_inside_root_cannot_alias_another_account(tmp_path):
    shared=root(tmp_path)
    (shared/'account'/'other').mkdir(parents=True)
    (shared/'account'/'alias').symlink_to(shared/'account'/'other',target_is_directory=True)
    with pytest.raises(ValueError):
        FileStore(shared).upload_text('hxacc/account/alias/finished','')
    assert not (shared/'account'/'other'/'finished').exists()


def test_source_oss_factory_is_preserved(monkeypatch):
    expected=object()
    cfg=SimpleNamespace(storage_mode='OSS',oss_access_key_id='k',oss_access_key_secret='s',oss_bucket='b',oss_endpoint='e')
    calls=[]
    def factory(**kwargs):calls.append(kwargs);return expected
    monkeypatch.setattr('wechat_login_harvester.storage.OssClient',factory)
    assert make_storage(cfg) is expected
    assert calls[0]['endpoint']=='e'

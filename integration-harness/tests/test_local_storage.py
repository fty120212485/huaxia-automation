import json
from types import SimpleNamespace
import pytest

from integration_harness.local_store import FileStore
from integration_harness.object_storage import make_storage, LocalAccountUploader


def root(tmp_path):
    path=tmp_path/'shared';path.mkdir();(path/'.hxacc-store.json').write_text('{"format":1}')
    return path


def test_local_factory_does_not_construct_oss(tmp_path):
    cfg=SimpleNamespace(storage_mode='Local', shared_root=root(tmp_path))
    adapter=make_storage(cfg,oss_factory=lambda **kw: pytest.fail('OSS constructed'))
    assert adapter.upload_text('hxacc/account/id/account.json','{}').success
    assert adapter.get_object_text('hxacc/account/id/account.json')=='{}'
    assert adapter.upload_bytes('hxacc/account/id/point.png',b'png').success
    assert adapter.delete_object('hxacc/account/id/point.png').success
    assert adapter.get_optional_text('hxacc/account/id/absent') is None


def test_other_watcher_cannot_delete_or_claim_live_account(tmp_path):
    shared=root(tmp_path);a,b=LocalAccountUploader(shared),LocalAccountUploader(shared)
    assert a.acquire_account_lock('id')
    assert a.upload_text('hxacc/account/id/id.process','{}').success
    assert not b.acquire_account_lock('id')
    assert not b.delete_object('hxacc/account/id/id.process').success
    assert a.delete_object('hxacc/account/id/id.process').success
    assert b.acquire_account_lock('id')
    b.close();a.close()


def test_local_config_uses_one_credentials_directory(tmp_path,monkeypatch):
    from integration_harness.config import load_config
    monkeypatch.chdir(tmp_path)
    cfg=load_config({'HXACC_TOKEN':'t','HXACC_DEVICE_ID':'d','STORAGE_MODE':'Local',
                     'SHARED_ROOT':str(tmp_path/'shared'),'ACCOUNT_DIR':str(tmp_path/'private')})
    assert cfg.storage_mode=='Local'
    assert cfg.account_dir==tmp_path/'shared'/'credentials'


def test_local_upload_error_is_not_reported_as_success(tmp_path):
    a=LocalAccountUploader(root(tmp_path))
    assert not a.upload_text('hxacc/../../escape','secret').success
    assert not a.upload_file(tmp_path/'missing','hxacc/account/id/id.account').success


def test_oss_mode_preserves_factory_contract():
    cfg=SimpleNamespace(storage_mode='OSS',oss_access_key_id='k',oss_access_key_secret='s',oss_bucket='b',oss_endpoint='e')
    calls=[]
    expected=object()
    def factory(**kw):calls.append(kw);return expected
    assert make_storage(cfg,oss_factory=factory) is expected
    assert calls[0]['bucket_name']=='b'


def test_discovery_ignores_auth_only_and_unrelated_directories(tmp_path):
    from integration_harness.main import _discover_storage_accounts
    shared=root(tmp_path)
    adapter=LocalAccountUploader(shared)
    for ident in ["auth-only", "未命名文件夹", "'429001198409298228"]:
        adapter.upload_text(f'hxacc/account/{ident}/account.json','{}')
    adapter.upload_text('hxacc/account/canonical/canonical.account','{}')
    adapter.upload_text('hxacc/account/Z00000000/account.json','{}')
    credentials=shared/'credentials';credentials.mkdir()
    (credentials/'Z00000000.account').write_text('{}')
    assert _discover_storage_accounts(adapter,credentials,'Local')=={'canonical','Z00000000'}
    assert 'auth-only' in _discover_storage_accounts(adapter,credentials,'OSS')
    # A later login creates real learning credentials and is discoverable next scan.
    adapter.upload_text('hxacc/account/auth-only/auth-only.account','{}')
    assert 'auth-only' in _discover_storage_accounts(adapter,credentials,'Local')

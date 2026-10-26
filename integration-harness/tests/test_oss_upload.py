from io import BytesIO
from types import SimpleNamespace

import oss2
import pytest

from integration_harness.oss_upload import OssAccountUploader


def test_upload_file_success(tmp_path, monkeypatch):
    class FakeBucket:
        def __init__(self):
            self.key = None
            self.file_path = None

        def put_object_from_file(self, key, file_path):
            self.key = key
            self.file_path = file_path

    uploader = OssAccountUploader(
        access_key_id="key",
        access_key_secret="secret",
        bucket_name="bucket",
        endpoint="https://example.invalid",
    )
    fake_bucket = FakeBucket()
    monkeypatch.setattr(uploader, "bucket", fake_bucket)

    source = tmp_path / "id.account"
    source.write_text("{}", encoding="utf-8")
    result = uploader.upload_file(source, "hxacc/account/id")

    assert result.success is True
    assert fake_bucket.key == "hxacc/account/id"
    assert fake_bucket.file_path == str(source)


def test_upload_file_returns_failure(monkeypatch):
    class FakeBucket:
        def put_object_from_file(self, key, file_path):
            raise RuntimeError("boom")

    uploader = OssAccountUploader(
        access_key_id="key",
        access_key_secret="secret",
        bucket_name="bucket",
        endpoint="https://example.invalid",
    )
    monkeypatch.setattr(uploader, "bucket", FakeBucket())

    result = uploader.upload_file(__file__, "hxacc/account/id")

    assert result.success is False
    assert "boom" in result.error


def test_upload_text_success(monkeypatch):
    class FakeBucket:
        def put_object(self, key, content):
            self.key = key
            self.content = content

    uploader = OssAccountUploader(
        access_key_id="key",
        access_key_secret="secret",
        bucket_name="bucket",
        endpoint="https://example.invalid",
    )
    fake_bucket = FakeBucket()
    monkeypatch.setattr(uploader, "bucket", fake_bucket)

    result = uploader.upload_text("hxacc/account/id/20260917000000.success")

    assert result.success is True
    assert fake_bucket.key == "hxacc/account/id/20260917000000.success"
    assert fake_bucket.content == b""


def test_upload_bytes_success(monkeypatch):
    class FakeBucket:
        def put_object(self, key, content):
            self.key = key
            self.content = content

    uploader = OssAccountUploader(
        access_key_id="key",
        access_key_secret="secret",
        bucket_name="bucket",
        endpoint="https://example.invalid",
    )
    fake_bucket = FakeBucket()
    monkeypatch.setattr(uploader, "bucket", fake_bucket)

    result = uploader.upload_bytes("hxacc/account/id/pointCode.png", b"png")

    assert result.success is True
    assert fake_bucket.content == b"png"


def test_list_prefixes_handles_string_items(monkeypatch):
    class FakeBucket:
        def list_objects(self, prefix, delimiter, marker, max_keys):
            assert max_keys == 1000
            return SimpleNamespace(prefix_list=["hxacc/account/id/"], is_truncated=False)

    uploader = OssAccountUploader(
        access_key_id="key",
        access_key_secret="secret",
        bucket_name="bucket",
        endpoint="https://example.invalid",
    )
    monkeypatch.setattr(uploader, "bucket", FakeBucket())

    assert uploader.list_prefixes("hxacc/account/") == ["hxacc/account/id/"]


def test_list_prefixes_reads_next_page():
    calls = []
    class FakeBucket:
        def list_objects(self, **kwargs):
            calls.append(kwargs)
            if not kwargs['marker']:
                return SimpleNamespace(prefix_list=['hxacc/account/first/'], is_truncated=True, next_marker='next')
            return SimpleNamespace(prefix_list=[SimpleNamespace(prefix='hxacc/account/last/')], is_truncated=False)
    uploader = OssAccountUploader.__new__(OssAccountUploader)
    uploader.bucket = FakeBucket()
    assert uploader.list_prefixes('hxacc/account/') == ['hxacc/account/first/', 'hxacc/account/last/']
    assert [c['marker'] for c in calls] == ['', 'next']
    assert all(c['max_keys'] == 1000 for c in calls)


@pytest.mark.parametrize('failure', ['network', 'empty_marker', 'cycle'])
def test_list_prefixes_rejects_incomplete_listing(failure):
    calls = []
    class FakeBucket:
        def list_objects(self, **kwargs):
            calls.append(kwargs['marker'])
            if len(calls) > 1 and failure == 'network':
                raise TimeoutError('timeout')
            marker = '' if failure == 'empty_marker' else 'next'
            return SimpleNamespace(prefix_list=['hxacc/account/first/'], is_truncated=True, next_marker=marker)
    uploader = OssAccountUploader.__new__(OssAccountUploader)
    uploader.bucket = FakeBucket()
    with pytest.raises((RuntimeError, TimeoutError)):
        uploader.list_prefixes('hxacc/account/')
    assert len(calls) <= 2


@pytest.mark.parametrize('content', [b'', b'2026-10-26'])
def test_optional_text_reads_once(content):
    calls = []
    class Reader:
        def get_object(self, key):
            calls.append(key)
            return BytesIO(content)
    uploader = OssAccountUploader.__new__(OssAccountUploader)
    uploader.bucket = Reader()
    assert uploader.get_optional_text('marker') == content.decode()
    assert calls == ['marker']


@pytest.mark.parametrize('error,missing', [
    (oss2.exceptions.NoSuchKey(404, {}, b'', {}), True),
    (oss2.exceptions.NoSuchBucket(404, {}, b'', {}), False),
    (oss2.exceptions.AccessDenied(403, {}, b'', {}), False),
    (TimeoutError('timeout'), False),
])
def test_optional_text_only_treats_missing_object_as_absent(error, missing):
    class Reader:
        def get_object(self, key):
            raise error
    uploader = OssAccountUploader.__new__(OssAccountUploader)
    uploader.bucket = Reader()
    if missing:
        assert uploader.get_optional_text('marker') is None
    else:
        with pytest.raises(type(error)):
            uploader.get_optional_text('marker')


def test_directory_cache_returns_copies_and_retries_failed_refresh(monkeypatch):
    from integration_harness.oss_runtime import ReadCache
    now = [0.0]
    calls = []
    class Reader:
        fail = False
        def list_objects(self, **kwargs):
            calls.append(kwargs)
            if self.fail:
                raise TimeoutError('timeout')
            return SimpleNamespace(prefix_list=['hxacc/account/id/'], is_truncated=False)
    oss = OssAccountUploader.__new__(OssAccountUploader)
    oss.bucket = Reader()
    oss._prefix_cache = ReadCache(clock=lambda: now[0])
    monkeypatch.setenv('OSS_DIRECTORY_CACHE_SECONDS', '60')
    oss.list_prefixes('hxacc/account/').clear()
    assert oss.list_prefixes('hxacc/account/') == ['hxacc/account/id/']
    assert len(calls) == 1
    now[0] = 60
    oss.bucket.fail = True
    with pytest.raises(TimeoutError):
        oss.list_prefixes('hxacc/account/')
    oss.bucket.fail = False
    assert oss.list_prefixes('hxacc/account/') == ['hxacc/account/id/']
    assert len(calls) == 3

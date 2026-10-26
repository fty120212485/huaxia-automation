import logging
from io import BytesIO
from types import SimpleNamespace

import pytest
import oss2

from wechat_login_harvester.oss import OssClient


def test_list_prefixes_reads_all_pages():
    calls = []

    class PaginatedBucket:
        def list_objects(self, **kwargs):
            calls.append(kwargs)
            if kwargs["marker"] == "":
                return SimpleNamespace(
                    prefix_list=["hxacc/account/first/"],
                    is_truncated=True,
                    next_marker="page-2",
                )
            assert kwargs["marker"] == "page-2"
            return SimpleNamespace(
                prefix_list=[SimpleNamespace(prefix="hxacc/account/last/")],
                is_truncated=False,
            )

    oss = OssClient.__new__(OssClient)
    oss.bucket = PaginatedBucket()
    assert oss.list_prefixes("hxacc/account/") == [
        "hxacc/account/first/", "hxacc/account/last/"
    ]
    assert calls == [
        {"prefix": "hxacc/account/", "delimiter": "/", "marker": "", "max_keys": 1000},
        {"prefix": "hxacc/account/", "delimiter": "/", "marker": "page-2", "max_keys": 1000},
    ]


@pytest.mark.parametrize("fail_with_exception", [True, False])
def test_list_prefixes_reports_failed_or_stuck_pagination(caplog, fail_with_exception):
    class BrokenBucket:
        def list_objects(self, **kwargs):
            if fail_with_exception:
                raise RuntimeError("network failure")
            return SimpleNamespace(prefix_list=[], is_truncated=True, next_marker="")

    oss = OssClient.__new__(OssClient)
    oss.bucket = BrokenBucket()
    with pytest.raises(RuntimeError):
        oss.list_prefixes("hxacc/account/")
    assert "OSS 列举目录失败" in caplog.text


class FakeBucket:
    def __init__(self) -> None:
        self.calls = []
        self.file_calls = []

    def put_object(self, key, content):
        self.calls.append((key, content))

    def put_object_from_file(self, key, file_path):
        self.file_calls.append((key, file_path))


def test_upload_text_logs_start_and_success(caplog, monkeypatch):
    oss = OssClient(
        access_key_id="a",
        access_key_secret="b",
        bucket_name="bucket",
        endpoint="https://example.com",
    )
    fake_bucket = FakeBucket()
    monkeypatch.setattr(oss, "bucket", fake_bucket)

    with caplog.at_level(logging.INFO, logger="wechat_login_harvester.oss"):
        oss.upload_text("hxacc/account/1/account.json", "{}")

    assert "OSS 上传开始：hxacc/account/1/account.json" in caplog.text
    assert "OSS 上传成功：hxacc/account/1/account.json" in caplog.text


def test_upload_file_logs_start_and_success(caplog, monkeypatch, tmp_path):
    oss = OssClient(
        access_key_id="a",
        access_key_secret="b",
        bucket_name="bucket",
        endpoint="https://example.com",
    )
    fake_bucket = FakeBucket()
    monkeypatch.setattr(oss, "bucket", fake_bucket)
    source = tmp_path / "id.account"
    source.write_text("{}", encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="wechat_login_harvester.oss"):
        oss.upload_file(source, "hxacc/account/id/id.account")

    assert "OSS 上传文件开始" in caplog.text
    assert "OSS 上传文件成功：hxacc/account/id/id.account" in caplog.text
    assert fake_bucket.file_calls == [("hxacc/account/id/id.account", str(source))]


@pytest.mark.parametrize("content", [b"", b'{"attempts":0}'])
def test_optional_text_reads_once_and_preserves_empty_objects(content):
    calls = []
    class Reader:
        def get_object(self, key):
            calls.append(key)
            return BytesIO(content)
    oss = OssClient.__new__(OssClient)
    oss.bucket = Reader()
    assert oss.get_optional_text("marker") == content.decode()
    assert calls == ["marker"]


@pytest.mark.parametrize("error,missing", [
    (oss2.exceptions.NoSuchKey(404, {}, b"", {}), True),
    (oss2.exceptions.NoSuchBucket(404, {}, b"", {}), False),
    (oss2.exceptions.AccessDenied(403, {}, b"", {}), False),
    (TimeoutError("timeout"), False),
])
def test_optional_text_only_treats_missing_object_as_absent(error, missing):
    class Reader:
        def get_object(self, key):
            raise error
    oss = OssClient.__new__(OssClient)
    oss.bucket = Reader()
    if missing:
        assert oss.get_optional_text("marker") is None
    else:
        with pytest.raises(type(error)):
            oss.get_optional_text("marker")


def test_directory_cache_returns_copies_and_retries_failed_refresh(monkeypatch):
    from wechat_login_harvester.oss_runtime import ReadCache
    now = [0.0]
    calls = []
    class Reader:
        fail = False
        def list_objects(self, **kwargs):
            calls.append(kwargs)
            if self.fail:
                raise TimeoutError('timeout')
            return SimpleNamespace(prefix_list=['hxacc/account/id/'], is_truncated=False)
    oss = OssClient.__new__(OssClient)
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

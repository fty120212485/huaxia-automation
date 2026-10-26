import os

import pytest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from integration_harness.accounts import Account
from integration_harness.config import load_config
from integration_harness.main import (
    _account_slot,
    _account_file_paths,
    _append_compensation,
    _compensation_sources,
    _daily_limit_key,
    _ensure_account_file,
    _is_in_run_window,
    _oss_worker_ids,
    _prepare_accounts,
    _remove_compensation,
    _resume_account,
    _single_account_command,
    _watch_accounts,
    build_parser,
)


def test_parser_accepts_account_dir():
    parser = build_parser()
    args = parser.parse_args(["run", "--account-dir", "/tmp/account"])
    assert args.account_dir == "/tmp/account"
    assert args.compensate is False


def test_parser_accepts_compensate_and_workers():
    parser = build_parser()
    args = parser.parse_args(["run", "--compensate", "--max-workers", "4"])
    assert args.compensate is True
    assert args.max_workers == 4


def test_parser_accepts_watch_and_single_account_file():
    parser = build_parser()
    args = parser.parse_args(
        ["watch", "--account-dir", "/tmp/account"]
    )
    assert args.command == "watch"
    assert args.account_dir == "/tmp/account"

    run_args = parser.parse_args(
        ["run", "--account-file", "/tmp/account/id.account"]
    )
    assert run_args.command == "run"
    assert run_args.account_file == "/tmp/account/id.account"


def test_parser_accepts_resume_and_id_card():
    parser = build_parser()
    args = parser.parse_args(["resume", "--id-card", "id-1"])
    assert args.command == "resume"
    assert args.id_card == "id-1"


def test_parser_accepts_workers_command():
    parser = build_parser()
    args = parser.parse_args(["workers", "--list"])
    assert args.command == "workers"
    assert args.list is True

    args = parser.parse_args(
        ["workers", "--add", "device-03", "--remove", "device-02"]
    )
    assert args.add == ["device-03"]
    assert args.remove == ["device-02"]

    args = parser.parse_args(["workers", "--set", "device-01", "device-02"])
    assert args.set == ["device-01", "device-02"]


def test_oss_worker_ids_parses_dedupes_and_rejects_bad_input():
    class FakeUploader:
        def __init__(self, content=None):
            self._content = content

        def get_object_text(self, key):
            if self._content is None:
                raise RuntimeError("NoSuchKey")
            return self._content

    assert _oss_worker_ids(
        FakeUploader('["device-02", "device-01", "device-02"]')
    ) == ["device-01", "device-02"]
    assert _oss_worker_ids(FakeUploader("not json")) is None
    assert _oss_worker_ids(FakeUploader("{}")) is None
    assert _oss_worker_ids(FakeUploader("[]")) is None
    assert _oss_worker_ids(FakeUploader()) is None


def test_ensure_account_file_pulls_from_oss_when_local_missing(tmp_path):
    class FakeUploader:
        def __init__(self, content=None, error=False):
            self._content = content
            self._error = error

        def get_object_text(self, key):
            if self._error:
                raise RuntimeError("NoSuchKey")
            return self._content

    path = _ensure_account_file(
        FakeUploader('{"token": "t"}'), tmp_path, "id-1"
    )
    assert path == tmp_path / "id-1.account"
    assert path.read_text() == '{"token": "t"}'

    (tmp_path / "id-2.account").write_text("local", encoding="utf-8")
    path = _ensure_account_file(
        FakeUploader('{"token": "oss"}'), tmp_path, "id-2"
    )
    assert path.read_text() == "local"

    assert _ensure_account_file(FakeUploader(error=True), tmp_path, "id-3") is None
    assert _ensure_account_file(FakeUploader("   "), tmp_path, "id-4") is None


def test_load_config_populates_dotenv_worker_id(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKER_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("WORKER_ID=device-02\n", encoding="utf-8")

    load_config({"HXACC_TOKEN": "t", "HXACC_DEVICE_ID": "d"})

    assert os.getenv("WORKER_ID") == "device-02"


def test_is_in_run_window_respects_hours():
    assert _is_in_run_window(datetime(2026, 9, 16, 5, 0), 5, 22) is True
    assert _is_in_run_window(datetime(2026, 9, 16, 21, 59), 5, 22) is True
    assert _is_in_run_window(datetime(2026, 9, 16, 22, 0), 5, 22) is False
    assert _is_in_run_window(datetime(2026, 9, 16, 4, 59), 5, 22) is False


def _account(tmp_path: Path, name: str) -> Account:
    source = tmp_path / f"{name}.account"
    return Account(
        id_card=name,
        name=name,
        replay=False,
        auto_next_course=None,
        app_id="app-1",
        token="token-1",
        device_id="device-1",
        source_path=source,
    )


def test_compensation_queue_deduplicates_and_removes(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    account = _account(tmp_path, "id-1")

    _append_compensation(account, "first")
    _append_compensation(account, "second")
    assert _compensation_sources() == [str(account.source_path)]

    _remove_compensation(account)
    assert _compensation_sources() == []


def test_prepare_accounts_builds_parallel_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("ACCOUNT_DIR", str(tmp_path))
    accounts = [_account(tmp_path, "id-1"), _account(tmp_path, "id-2")]

    prepared, exit_code = _prepare_accounts(accounts)

    assert exit_code == 0
    assert [task.account.id_card for task in prepared] == ["id-1", "id-2"]
    for task in prepared:
        assert task.config.token == "token-1"


def test_account_file_paths_filters_account_files(tmp_path):
    (tmp_path / "a.account").write_text("{}", encoding="utf-8")
    (tmp_path / "b.txt").write_text("{}", encoding="utf-8")
    subdir = tmp_path / "sub"
    subdir.mkdir()
    (subdir / "c.account").write_text("{}", encoding="utf-8")

    paths = _account_file_paths(tmp_path)

    assert [Path(path).name for path in sorted(paths)] == ["a.account"]


def test_single_account_command(tmp_path):
    account = tmp_path / "id.account"
    command = _single_account_command(account)

    assert command[-2:] == ["--account-file", str(account)]


def test_resume_account_deletes_stop_and_daily_limit_markers_and_notifies(monkeypatch):
    class FakeUploader:
        def __init__(self, **kwargs):
            self.deleted = []

        def object_exists(self, key):
            return True

        def delete_object(self, key):
            self.deleted.append(key)
            return SimpleNamespace(key=key, success=True, error=None)

    class FakeNotifier:
        def __init__(self, *args, **kwargs):
            self.sent = []

        def send_markdown(self, content):
            self.sent.append(content)
            return True

        def close(self):
            pass

    uploader = FakeUploader()
    monkeypatch.setattr(
        "integration_harness.main.load_config",
        lambda overrides=None: SimpleNamespace(
            wecom_webhook_url="https://example.com/hook",
            oss_bucket="bucket",
            oss_access_key_id="key",
            oss_access_key_secret="secret",
            oss_endpoint="https://example.com",
        ),
    )
    monkeypatch.setattr(
        "integration_harness.main.OssAccountUploader",
        lambda **kwargs: uploader,
    )
    monkeypatch.setattr(
        "integration_harness.main.WeChatNotifier",
        FakeNotifier,
    )

    result = _resume_account("id-1")

    assert result == 0
    assert uploader.deleted == [
        "hxacc/account/id-1/stop",
        _daily_limit_key("id-1"),
    ]


def test_account_slot_is_stable_and_in_range():
    assert _account_slot("id-1", 16) == _account_slot("id-1", 16)
    assert 0 <= _account_slot("id-1", 16) < 16


class _FakePopen:
    instances = []

    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.terminated = False
        self.killed = False
        self.waited = False
        _FakePopen.instances.append(self)

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waited = True
        return 0


def test_watch_accounts_starts_and_stops_child(tmp_path, monkeypatch):
    account = tmp_path / "id.account"
    account.write_text(
        '{"appId":"app","token":"token","deviceId":"device","name":"张三","tokenExpiresAt":4102444800}',
        encoding="utf-8",
    )

    class FakeUploader:
        def list_prefixes(self, prefix):
            return ["hxacc/account/id/"]

        def object_exists(self, key):
            return False

        def upload_text(self, key, content=""):
            return SimpleNamespace(key=key, success=True, error=None)

        def delete_object(self, key):
            return SimpleNamespace(key=key, success=True, error=None)

    class FakeNotifier:
        def __init__(self, *args, **kwargs):
            self.sent = []

        def send_markdown(self, content):
            self.sent.append(content)
            return True

        def close(self):
            pass

    monkeypatch.setattr(
        "integration_harness.main._project_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "integration_harness.main.load_config",
        lambda overrides=None: SimpleNamespace(
            wecom_webhook_url="https://example.com/hook",
            runs_dir=tmp_path / "runs",
            oss_bucket="bucket",
            oss_access_key_id="key",
            oss_access_key_secret="secret",
            oss_endpoint="https://example.com",
        ),
    )
    monkeypatch.setattr(
        "integration_harness.main.OssAccountUploader",
        lambda **kwargs: FakeUploader(),
    )
    monkeypatch.setattr(
        "integration_harness.main.WeChatNotifier",
        FakeNotifier,
    )
    monkeypatch.setattr(
        "integration_harness.main._single_account_command",
        lambda path: ["fake-run", str(path)],
    )
    monkeypatch.setattr(
        "integration_harness.main.subprocess.Popen",
        _FakePopen,
    )

    def interrupt_sleep(_):
        raise KeyboardInterrupt

    monkeypatch.setattr("integration_harness.main.time.sleep", interrupt_sleep)
    _FakePopen.instances.clear()

    result = _watch_accounts(tmp_path)

    assert result == 0
    assert len(_FakePopen.instances) == 1
    assert _FakePopen.instances[0].terminated is True
    assert _FakePopen.instances[0].waited is True


def test_watch_accounts_restarts_child_when_account_file_changes(
    tmp_path, monkeypatch
):
    account = tmp_path / "id.account"
    account.write_text(
        '{"appId":"app","token":"token","deviceId":"device","name":"张三","tokenExpiresAt":4102444800}',
        encoding="utf-8",
    )

    class FakeUploader:
        def list_prefixes(self, prefix):
            return ["hxacc/account/id/"]

        def object_exists(self, key):
            return False

        def upload_text(self, key, content=""):
            return SimpleNamespace(key=key, success=True, error=None)

        def delete_object(self, key):
            return SimpleNamespace(key=key, success=True, error=None)

    class FakeNotifier:
        def __init__(self, *args, **kwargs):
            self.sent = []

        def send_markdown(self, content):
            self.sent.append(content)
            return True

        def close(self):
            pass

    monkeypatch.setattr(
        "integration_harness.main._project_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "integration_harness.main.load_config",
        lambda overrides=None: SimpleNamespace(
            wecom_webhook_url="https://example.com/hook",
            runs_dir=tmp_path / "runs",
            oss_bucket="bucket",
            oss_access_key_id="key",
            oss_access_key_secret="secret",
            oss_endpoint="https://example.com",
        ),
    )
    monkeypatch.setattr(
        "integration_harness.main.OssAccountUploader",
        lambda **kwargs: FakeUploader(),
    )
    monkeypatch.setattr(
        "integration_harness.main.WeChatNotifier",
        FakeNotifier,
    )
    monkeypatch.setattr(
        "integration_harness.main._single_account_command",
        lambda path: ["fake-run", str(path)],
    )
    monkeypatch.setattr(
        "integration_harness.main.subprocess.Popen",
        _FakePopen,
    )

    calls = 0

    def change_then_interrupt(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            account.write_text(
                '{"appId":"app","token":"new-token","deviceId":"device","name":"张三","tokenExpiresAt":4102444800}',
                encoding="utf-8",
            )
            return
        raise KeyboardInterrupt

    monkeypatch.setattr("integration_harness.main.time.sleep", change_then_interrupt)
    _FakePopen.instances.clear()

    result = _watch_accounts(tmp_path)

    assert result == 0
    assert len(_FakePopen.instances) == 2
    assert _FakePopen.instances[0].terminated is True
    assert _FakePopen.instances[1].terminated is True


@pytest.fixture
def lease_watch(tmp_path, monkeypatch):
    from integration_harness import main as main_module

    account = tmp_path / "id.account"
    account.write_text('{"tokenExpiresAt":4102444800}', encoding="utf-8")
    clock = [0.0]

    class FakeUploader:
        process_exists = False
        process_checks = 0
        process_reads = 0
        process_writes = 0
        stopped = False

        def list_prefixes(self, prefix):
            return ["hxacc/account/id/"]

        def object_exists(self, key):
            if key.endswith(".process"):
                self.process_checks += 1
                return self.process_exists
            return self.stopped and key.endswith("/stop")

        def get_object_text(self, key):
            if key.endswith(".process"):
                self.process_reads += 1
                return '{"worker_id":"other","expires_at":"2100-01-01T00:00:00+00:00"}'
            return ""

        def upload_text(self, key, content=""):
            assert key.endswith(".process")
            self.process_exists = True
            self.process_writes += 1
            return SimpleNamespace(success=True, error=None)

        def delete_object(self, key):
            if key.endswith(".process"):
                self.process_exists = False
            return SimpleNamespace(success=True, error=None)

    uploader = FakeUploader()
    monkeypatch.setattr(main_module, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(main_module, "load_config", lambda overrides=None: SimpleNamespace(
        wecom_webhook_url="https://example.com/hook", runs_dir=tmp_path / "runs",
        oss_bucket="bucket", oss_access_key_id="key", oss_access_key_secret="secret",
        oss_endpoint="https://example.com",
    ))
    monkeypatch.setattr(main_module, "OssAccountUploader", lambda **kwargs: uploader)
    monkeypatch.setattr(main_module, "WeChatNotifier", lambda *args, **kwargs: SimpleNamespace(
        send_markdown=lambda content: True, close=lambda: None,
    ))
    monkeypatch.setattr(main_module, "_oss_worker_ids", lambda _: ["test-worker"])
    monkeypatch.setenv("WORKER_ID", "test-worker")
    monkeypatch.setenv("PROCESS_LEASE_SECONDS", "120")
    monkeypatch.setattr(main_module, "_single_account_command", lambda path: ["fake-run"])
    monkeypatch.setattr(main_module.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(main_module.time, "monotonic", lambda: clock[0])
    _FakePopen.instances.clear()
    return main_module, uploader, clock, account


def test_watch_local_process_skips_remote_lease_reads_but_renews(lease_watch, monkeypatch):
    main_module, uploader, clock, account = lease_watch
    rounds = 0

    def advance(_):
        nonlocal rounds
        rounds += 1
        if rounds == 3:
            raise KeyboardInterrupt
        clock[0] += 61

    monkeypatch.setattr(main_module.time, "sleep", advance)
    assert main_module._watch_accounts(account.parent) == 0
    assert len(_FakePopen.instances) == 1
    assert uploader.process_checks == 1  # Only the initial acquisition.
    assert uploader.process_reads == 0
    assert uploader.process_writes == 3  # Initial lease and two renewals.


def test_watch_still_checks_remote_lease_before_starting(lease_watch, monkeypatch):
    main_module, uploader, clock, account = lease_watch
    uploader.process_exists = True
    rounds = 0

    def advance(_):
        nonlocal rounds
        rounds += 1
        if rounds == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(main_module.time, "sleep", advance)
    assert main_module._watch_accounts(account.parent) == 0
    assert _FakePopen.instances == []
    assert uploader.process_checks == 3
    assert uploader.process_reads == 3
    assert uploader.process_writes == 0


@pytest.mark.parametrize("reason", ["stop", "expired", "exit"])
def test_watch_local_process_still_stops_or_restarts(lease_watch, monkeypatch, reason):
    main_module, uploader, clock, account = lease_watch
    rounds = 0

    def change_then_interrupt(_):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            if reason == "stop":
                uploader.stopped = True
            elif reason == "expired":
                monkeypatch.setattr(main_module, "_token_expired", lambda _: True)
            else:
                _FakePopen.instances[0].poll = lambda: 0
            return
        if rounds == 2 and reason == "exit":
            return
        raise KeyboardInterrupt

    monkeypatch.setattr(main_module.time, "sleep", change_then_interrupt)
    assert main_module._watch_accounts(account.parent) == 0
    assert len(_FakePopen.instances) == (2 if reason == "exit" else 1)
    assert uploader.process_reads == 0
    assert _FakePopen.instances[-1].terminated

import json
from types import SimpleNamespace

from wechat_login_harvester.__main__ import (
    RELOGIN_COOLDOWN_SECONDS,
    RELOGIN_MAX_ATTEMPTS,
    _poll_relogin_signals,
    _relogin_signal_key,
)
from wechat_login_harvester.config import UserCredential


class _FakeOss:
    def __init__(self, signals: dict[str, str], prefixes: list[str]) -> None:
        self.signals = dict(signals)
        self.prefixes = prefixes
        self.deleted: list[str] = []
        self.uploads: list[tuple[str, str]] = []

    def list_prefixes(self, prefix):
        return self.prefixes

    def object_exists(self, key):
        return key in self.signals

    def get_object_text(self, key):
        return self.signals.get(key, "")

    def get_optional_text(self, key):
        return self.signals.get(key)

    def delete_object(self, key):
        self.deleted.append(key)
        self.signals.pop(key, None)

    def upload_text(self, key, content):
        self.uploads.append((key, content))
        self.signals[key] = content


class _FakeNotifier:
    def __init__(self) -> None:
        self.failures: list[tuple[str, dict | None]] = []

    def send_failure(self, title, details=None):
        self.failures.append((title, details))
        return True


def _config(id_card: str) -> SimpleNamespace:
    return SimpleNamespace(
        users=(UserCredential(name="张三", id_card=id_card),)
    )


def test_relogin_signal_key():
    assert _relogin_signal_key("id-1") == "hxacc/account/id-1/relogin"


def test_poll_relogin_signals_deletes_on_success(monkeypatch):
    oss = _FakeOss(
        {"hxacc/account/id-1/relogin": '{"attempts": 0}'},
        ["hxacc/account/id-1/"],
    )
    notifier = _FakeNotifier()
    monkeypatch.setattr(
        "wechat_login_harvester.__main__._relogin_user",
        lambda config, user, notifier: True,
    )
    monkeypatch.setattr(
        "wechat_login_harvester.__main__.time.time",
        lambda: 1000.0,
    )

    _poll_relogin_signals(_config("id-1"), oss, notifier)

    assert "hxacc/account/id-1/relogin" in oss.deleted
    assert "hxacc/account/id-1/stop" in oss.deleted


def test_poll_relogin_signals_respects_cooldown(monkeypatch):
    oss = _FakeOss(
        {
            "hxacc/account/id-1/relogin": json.dumps(
                {"attempts": 1, "last_attempt_at": 1000.0}
            )
        },
        ["hxacc/account/id-1/"],
    )
    notifier = _FakeNotifier()
    called: list[bool] = []
    monkeypatch.setattr(
        "wechat_login_harvester.__main__._relogin_user",
        lambda config, user, notifier: called.append(True),
    )
    monkeypatch.setattr(
        "wechat_login_harvester.__main__.time.time",
        lambda: 1001.0,
    )

    _poll_relogin_signals(_config("id-1"), oss, notifier)

    assert called == []


def test_poll_relogin_signals_retries_then_alerts(monkeypatch):
    oss = _FakeOss(
        {"hxacc/account/id-1/relogin": '{"attempts": 0}'},
        ["hxacc/account/id-1/"],
    )
    notifier = _FakeNotifier()
    monkeypatch.setattr(
        "wechat_login_harvester.__main__._relogin_user",
        lambda config, user, notifier: False,
    )
    clock = {"t": 1000.0}
    monkeypatch.setattr(
        "wechat_login_harvester.__main__.time.time",
        lambda: clock["t"],
    )

    _poll_relogin_signals(_config("id-1"), oss, notifier)
    assert json.loads(oss.signals["hxacc/account/id-1/relogin"])["attempts"] == 1

    clock["t"] += RELOGIN_COOLDOWN_SECONDS + 1
    _poll_relogin_signals(_config("id-1"), oss, notifier)
    assert json.loads(oss.signals["hxacc/account/id-1/relogin"])["attempts"] == 2

    clock["t"] += RELOGIN_COOLDOWN_SECONDS + 1
    _poll_relogin_signals(_config("id-1"), oss, notifier)
    assert "hxacc/account/id-1/relogin" in oss.deleted
    assert notifier.failures
    assert "请马上修复" in notifier.failures[0][0]


def test_poll_relogin_signals_skips_unknown_user(monkeypatch):
    oss = _FakeOss(
        {"hxacc/account/id-1/relogin": '{"attempts": 0}'},
        ["hxacc/account/id-1/"],
    )
    notifier = _FakeNotifier()
    monkeypatch.setattr(
        "wechat_login_harvester.__main__._relogin_user",
        lambda config, user, notifier: True,
    )

    _poll_relogin_signals(
        SimpleNamespace(users=(UserCredential(name="李四", id_card="id-2"),)),
        oss,
        notifier,
    )

    assert "hxacc/account/id-1/relogin" in oss.deleted


def test_poll_relogin_signals_skips_empty_signal(monkeypatch):
    oss = _FakeOss(
        {"hxacc/account/id-1/relogin": ""},
        ["hxacc/account/id-1/"],
    )
    notifier = _FakeNotifier()
    called: list[bool] = []
    monkeypatch.setattr(
        "wechat_login_harvester.__main__._relogin_user",
        lambda config, user, notifier: called.append(True),
    )

    _poll_relogin_signals(_config("id-1"), oss, notifier)

    assert called == []
    assert "hxacc/account/id-1/relogin" not in oss.deleted


def test_relogin_reads_signal_without_existence_probe(monkeypatch):
    oss = _FakeOss(
        {"hxacc/account/id-1/relogin": '{"attempts": 0}'},
        ["hxacc/account/id-1/"],
    )
    def unexpected_probe(key):
        raise AssertionError("Should read signal directly")
    monkeypatch.setattr(oss, "object_exists", unexpected_probe)
    monkeypatch.setattr(
        "wechat_login_harvester.__main__._relogin_user", lambda *args: True
    )
    _poll_relogin_signals(_config("id-1"), oss, _FakeNotifier())
    assert "hxacc/account/id-1/relogin" in oss.deleted


def test_relogin_read_failure_is_reported_without_consuming_signal(monkeypatch, caplog):
    oss = _FakeOss(
        {"hxacc/account/id-1/relogin": '{"attempts": 0}'},
        ["hxacc/account/id-1/"],
    )
    def fail_read(key):
        raise TimeoutError("network timeout")
    monkeypatch.setattr(oss, "get_optional_text", fail_read)
    called = []
    monkeypatch.setattr(
        "wechat_login_harvester.__main__._relogin_user", lambda *args: called.append(True)
    )
    _poll_relogin_signals(_config("id-1"), oss, _FakeNotifier())
    assert called == []
    assert oss.deleted == []
    assert oss.uploads == []
    assert "TimeoutError" in caplog.text


def test_failed_directory_listing_does_not_process_signals(monkeypatch, caplog):
    oss = _FakeOss({'hxacc/account/id-1/relogin': '{"attempts":0}'}, [])
    def fail_listing(prefix):
        raise TimeoutError('timeout')
    monkeypatch.setattr(oss, 'list_prefixes', fail_listing)
    _poll_relogin_signals(_config('id-1'), oss, _FakeNotifier())
    assert oss.deleted == []
    assert oss.uploads == []
    assert '读取账号目录失败' in caplog.text


def test_watch_polls_login_signals_independently_of_user_file_checks(tmp_path, monkeypatch):
    import pytest
    from wechat_login_harvester import __main__ as main
    config = SimpleNamespace(user_file=tmp_path / 'user', watch_interval_seconds=5,
                             wecom_bot_id='', wecom_bot_secret='')
    now = [0]
    polls = []
    monkeypatch.setenv('RELOGIN_POLL_INTERVAL_SECONDS', '15')
    monkeypatch.setattr(main, '_make_oss_client', lambda config: None)
    monkeypatch.setattr(main, '_user_file_hash', lambda config: 'unchanged')
    monkeypatch.setattr(main, 'load_config', lambda: config)
    monkeypatch.setattr(main, '_poll_relogin_signals', lambda *args: polls.append(now[0]))
    monkeypatch.setattr(main.time, 'monotonic', lambda: now[0])
    def advance(_):
        now[0] += 5
        if now[0] >= 40:
            raise KeyboardInterrupt
    monkeypatch.setattr(main.time, 'sleep', advance)
    with pytest.raises(KeyboardInterrupt):
        main._command_watch(config, None)
    assert polls == [5, 20, 35]

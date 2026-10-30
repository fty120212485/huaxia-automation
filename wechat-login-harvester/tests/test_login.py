from pathlib import Path
from types import SimpleNamespace

import wechat_login_harvester.login as login_module
from wechat_login_harvester.login import LoginRunner
from wechat_login_harvester.ui import TextObservation


def test_logout_breaks_after_modal_is_dismissed(tmp_path):
    runner = LoginRunner.__new__(LoginRunner)
    runner._window = object()
    runner._counter = 0
    runner.screenshot_dir = tmp_path

    clicked = []
    runner._ensure_bottom_nav = lambda window: None
    runner._click_exact_text = lambda window, text: clicked.append(text)
    runner._wait_for_text = lambda window, text, *, timeout_seconds: None
    runner._dismiss_unbound_modal = lambda window: True
    runner._looks_logged_in = lambda texts: False
    runner._texts = lambda window: []
    runner._wait_for_any_text = lambda window, texts, *, timeout_seconds: None

    runner._logout(object())

    assert clicked.count("退出账号") == 1
    assert clicked.count("确定退出") == 1


def test_text_matches_requires_expected_text_inside_actual_text():
    assert LoginRunner._text_matches("欢迎登录华夏会计网", "欢迎登录华夏会计网") is True
    assert LoginRunner._text_matches("请输入用户名", "请输入用户名") is True
    assert LoginRunner._text_matches("欢迎登录华夏会计网", "华夏会计网") is False


def test_dismiss_unbound_modal_waits_for_login_page(tmp_path, monkeypatch):
    runner = LoginRunner.__new__(LoginRunner)
    runner._window = type(
        "Window",
        (),
        {"bounds": type("Bounds", (), {"x": 0, "y": 0})()},
    )()
    runner._counter = 0
    runner.screenshot_dir = tmp_path

    clicked = []
    responses = [
        [TextObservation("退出后需要重新绑定华夏会计账号", 0, 0, 100, 30)],
        [
            TextObservation("账号未绑定", 0, 0, 100, 30),
            TextObservation("确定", 200, 300, 40, 30),
        ],
        [TextObservation("欢迎登录华夏会计网", 0, 0, 200, 40)],
    ]
    response_iter = iter(responses)

    monkeypatch.setattr(login_module, "recognize_text", lambda path: next(response_iter))
    monkeypatch.setattr(login_module, "click_screen_point", lambda x, y: clicked.append((x, y)))
    monkeypatch.setattr(login_module.time, "sleep", lambda _: None)
    runner._snapshot = lambda window, name: Path("unused.png")

    result = runner._dismiss_unbound_modal(object())

    assert result is True
    assert len(clicked) == 1


def test_login_ui_ready_requires_all_bottom_markers():
    assert LoginRunner._logged_in_ui_ready(["首页", "课程", "考试", "我的"]) is True
    assert LoginRunner._logged_in_ui_ready(["首页", "课程", "我的"]) is False
    assert LoginRunner._looks_logged_in(["首页", "课程"]) is True
    assert LoginRunner._looks_logged_in(["首页"]) is False


def test_capture_window_retries_transient_screencapture_failure(tmp_path, monkeypatch):
    import subprocess

    import wechat_login_harvester.ui as ui_module

    calls = []

    def fake_run(cmd, *, check, capture_output, text):
        calls.append(cmd)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, cmd, stderr="locked screen\n")
        (tmp_path / "out.png").write_bytes(b"png")
        return 0

    monkeypatch.setattr(ui_module.subprocess, "run", fake_run)
    monkeypatch.setattr(ui_module.time, "sleep", lambda _: None)
    window = ui_module.MiniProgramWindow(
        window_id=1,
        owner_name="微信",
        name="华夏会计网校",
        bounds=ui_module.WindowBounds(527, 73, 414, 780),
    )

    result = ui_module.capture_window(window, tmp_path / "out.png")

    assert result == tmp_path / "out.png"
    assert len(calls) == 2


def test_capture_window_surfaces_stderr_after_retries(tmp_path, monkeypatch):
    import subprocess

    import wechat_login_harvester.ui as ui_module

    def fake_run(cmd, *, check, capture_output, text):
        raise subprocess.CalledProcessError(1, cmd, stderr="no screen permission\n")

    monkeypatch.setattr(ui_module.subprocess, "run", fake_run)
    monkeypatch.setattr(ui_module.time, "sleep", lambda _: None)
    window = ui_module.MiniProgramWindow(
        window_id=1,
        owner_name="微信",
        name="华夏会计网校",
        bounds=ui_module.WindowBounds(527, 73, 414, 780),
    )

    import pytest

    with pytest.raises(RuntimeError, match="no screen permission"):
        ui_module.capture_window(window, tmp_path / "out.png")


def test_account_file_updated_requires_new_mtime(tmp_path):
    runner = LoginRunner.__new__(LoginRunner)
    runner.config = SimpleNamespace(account_dir=tmp_path)
    runner.user = SimpleNamespace(id_card="id-1")
    runner._require_account_update = True
    runner._account_mtime_before = 0.0

    assert runner._account_file_updated() is False

    account = tmp_path / "id-1.account"
    account.write_text("{}", encoding="utf-8")

    assert runner._account_file_updated() is True


def test_profile_ready_requires_personal_center_info():
    runner = LoginRunner.__new__(LoginRunner)
    runner.user = SimpleNamespace(name="梁敏仪", id_card="440682197801283620")

    assert runner._profile_ready(
        ["梁敏仪", "440682197801283620", "我的订单"]
    ) is True
    assert runner._profile_ready(["个人中心", "退出账号"]) is False


def test_wait_for_profile_ready_clicks_my_and_waits(tmp_path, monkeypatch):
    runner = LoginRunner.__new__(LoginRunner)
    runner.user = SimpleNamespace(name="梁敏仪", id_card="440682197801283620")
    runner._window = type(
        "Window",
        (),
        {"bounds": type("Bounds", (), {"x": 0, "y": 0})()},
    )()
    runner.screenshot_dir = tmp_path
    runner._counter = 0

    clicked = []
    texts = [
        ["个人中心"],
        ["梁敏仪", "440682197801283620", "我的订单"],
    ]
    text_iter = iter(texts)

    runner._ensure_bottom_nav = lambda window: None
    runner._click_exact_text = lambda window, text: clicked.append(text)
    runner._texts = lambda window: next(text_iter)
    monkeypatch.setattr(login_module.time, "sleep", lambda _: None)

    runner._wait_for_profile_ready(object(), timeout_seconds=5)

    assert clicked == ["我的"]

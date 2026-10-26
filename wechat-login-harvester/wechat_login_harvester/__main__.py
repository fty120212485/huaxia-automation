from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .bot import UserStore, start_bot_thread
from .config import load_config
from .login import LoginRunner
from .oss import OssClient
from .reconcile import reconcile
from .wechat import WeChatNotifier
from .oss_runtime import STATS, seconds


logger = logging.getLogger(__name__)


DEFAULT_WECOM_WEBHOOK_URL = (
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
    "?key=de2d8b32-ca95-4117-b349-816feb0347d2"
)

RELOGIN_MAX_ATTEMPTS = 2
# ponytail: hardcoded retry cooldown; make configurable if operators need it.
RELOGIN_COOLDOWN_SECONDS = 300


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="WeChat mini-program login UI automation and token capture"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="Run UI login flow")
    sub.add_parser("capture", help="Start capture proxy")
    sub.add_parser("harvest", help="Start capture proxy and run login")
    sub.add_parser("reconcile", help="Reconcile user file with accounts/OSS")
    sub.add_parser("watch", help="Watch user file and reconcile changes")
    sub.add_parser("bot", help="Run WeCom intelligent-bot long connection only")
    return parser


def _session_id() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def _outbox_path() -> Path:
    return Path.cwd() / "runs" / ".wecom_outbox.jsonl"


def _make_notifier(config) -> WeChatNotifier | None:
    webhook_url = config.wecom_webhook_url or DEFAULT_WECOM_WEBHOOK_URL
    return WeChatNotifier(webhook_url, outbox_path=_outbox_path())


def _notify_failure(
    notifier: WeChatNotifier | None,
    title: str,
    *,
    details: dict[str, str] | None = None,
) -> None:
    message = title
    if details:
        message += "：" + "；".join(f"{key}={value}" for key, value in details.items())
    print(message, file=sys.stderr)
    if notifier is not None and not notifier.send_failure(title, details=details):
        print("企业微信推送失败，已写入本地待发队列", file=sys.stderr)


def _user_details(user) -> dict[str, str]:
    return {"姓名": user.name, "身份证": user.id_card}


def _active_network_service() -> str:
    route = subprocess.check_output(
        ["route", "get", "default"],
        text=True,
    )
    interface = ""
    for line in route.splitlines():
        if line.strip().startswith("interface:"):
            interface = line.split(":", 1)[1].strip()
            break
    if not interface:
        return "Wi-Fi"

    ports = subprocess.check_output(
        ["networksetup", "-listallhardwareports"],
        text=True,
    )
    current_port = ""
    for line in ports.splitlines():
        if line.startswith("Hardware Port:"):
            current_port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and line.split(":", 1)[1].strip() == interface:
            return current_port
    return "Wi-Fi"


def _set_system_proxy(port: int, service: str) -> None:
    host = "127.0.0.1"
    subprocess.run(
        ["networksetup", "-setwebproxy", service, host, str(port)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["networksetup", "-setsecurewebproxy", service, host, str(port)],
        check=True,
        capture_output=True,
    )


def _clear_system_proxy(service: str) -> None:
    subprocess.run(
        ["networksetup", "-setwebproxystate", service, "off"],
        check=False,
        capture_output=True,
    )
    subprocess.run(
        ["networksetup", "-setsecurewebproxystate", service, "off"],
        check=False,
        capture_output=True,
    )


def _capture_script_path() -> Path:
    return Path(__file__).with_name("capture.py")


def _start_capture(config, run_dir: Path) -> subprocess.Popen:
    run_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = run_dir / "mitmdump.out.log"
    stderr_path = run_dir / "mitmdump.err.log"
    mitmdump = Path(sys.executable).with_name("mitmdump")
    command = [
        str(mitmdump),
        "--listen-port",
        str(config.capture_port),
        "-s",
        str(_capture_script_path()),
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = (
        str(Path.cwd())
        + os.pathsep
        + env.get("PYTHONPATH", "")
    )
    proc = subprocess.Popen(
        command,
        cwd=Path.cwd(),
        stdout=stdout_path.open("w", encoding="utf-8"),
        stderr=stderr_path.open("w", encoding="utf-8"),
        env=env,
    )
    time.sleep(1)
    if proc.poll() is not None:
        raise RuntimeError(
            f"抓包代理启动失败，详见 {stderr_path} / {stdout_path}"
        )
    return proc


def _stop_capture(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)


def _command_login(config, notifier: WeChatNotifier | None) -> int:
    active_users = [user for user in config.users if not user.skip]
    for index, user in enumerate(active_users, 1):
        run_dir = Path.cwd() / "runs" / f"{_session_id()}-{index:02d}"
        try:
            runner = LoginRunner(
                config,
                user,
                screenshot_dir=run_dir / "screenshots",
            )
            path = runner.run()
            print(f"User {index} login screenshot: {path}")
        except Exception as exc:
            _notify_failure(
                notifier,
                "微信登录失败",
                details={
                    **_user_details(user),
                    "异常": f"{type(exc).__name__}: {exc}",
                },
            )
            return 1
    return 0


def _command_capture(config, notifier: WeChatNotifier | None) -> int:
    config.token_output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path.cwd() / "runs" / _session_id()
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        proc = _start_capture(config, run_dir)
    except Exception as exc:
        _notify_failure(
            notifier,
            "抓包代理启动失败",
            details={"异常": f"{type(exc).__name__}: {exc}"},
        )
        return 1
    try:
        service = _active_network_service()
        _set_system_proxy(config.capture_port, service)
    except Exception as exc:
        _stop_capture(proc)
        _notify_failure(
            notifier,
            "设置系统代理失败",
            details={"异常": f"{type(exc).__name__}: {exc}"},
        )
        return 1
    print(f"Capture proxy started on 127.0.0.1:{config.capture_port}")
    try:
        return_code = proc.wait()
        if return_code != 0:
            _notify_failure(
                notifier,
                "抓包代理异常退出",
                details={"returncode": str(return_code)},
            )
            return 1
    except KeyboardInterrupt:
        _stop_capture(proc)
    finally:
        _clear_system_proxy(service)
    return 0


def _upload_account_artifacts(
    config,
    user,
    account_path: Path,
) -> bool:
    if not account_path.exists():
        return False
    oss = OssClient(
        access_key_id=config.oss_access_key_id,
        access_key_secret=config.oss_access_key_secret,
        bucket_name=config.oss_bucket,
        endpoint=config.oss_endpoint,
    )
    prefix = f"hxacc/account/{user.id_card}"
    account_key = f"{prefix}/{account_path.name}"
    manifest_key = f"{prefix}/account.json"
    oss.upload_file(account_path, account_key)
    oss.upload_text(
        manifest_key,
        json.dumps(
            {"username": user.name, "id_card": user.id_card},
            ensure_ascii=False,
        ),
    )
    return True


def _harvest_users(config, users, notifier: WeChatNotifier | None) -> int:
    config.token_output_dir.mkdir(parents=True, exist_ok=True)
    capture_run_dir = Path.cwd() / "runs" / _session_id()
    try:
        proc = _start_capture(config, capture_run_dir)
    except Exception as exc:
        _notify_failure(
            notifier,
            "抓包代理启动失败",
            details={"异常": f"{type(exc).__name__}: {exc}"},
        )
        return 1
    try:
        service = _active_network_service()
        _set_system_proxy(config.capture_port, service)
    except Exception as exc:
        _stop_capture(proc)
        _notify_failure(
            notifier,
            "设置系统代理失败",
            details={"异常": f"{type(exc).__name__}: {exc}"},
        )
        return 1
    try:
        for index, user in enumerate(users, 1):
            account_path = config.account_dir / f"{user.id_card}.account"
            before_mtime = (
                account_path.stat().st_mtime if account_path.exists() else 0.0
            )
            captured = False
            last_error = ""
            for attempt in range(1, 4):
                try:
                    run_dir = (
                        Path.cwd()
                        / "runs"
                        / f"{_session_id()}-{index:02d}-attempt{attempt}"
                    )
                    runner = LoginRunner(
                        config,
                        user,
                        screenshot_dir=run_dir / "screenshots",
                        require_account_update=True,
                    )
                    runner.run()
                    if account_path.exists():
                        try:
                            _upload_account_artifacts(config, user, account_path)
                        except Exception as exc:
                            _notify_failure(
                                notifier,
                                "OSS 上传失败",
                                details={
                                    **_user_details(user),
                                    "异常": f"{type(exc).__name__}: {exc}",
                                },
                            )
                    time.sleep(2)
                    after_mtime = (
                        account_path.stat().st_mtime
                        if account_path.exists()
                        else 0.0
                    )
                    if after_mtime > before_mtime:
                        captured = True
                        break
                    last_error = "未捕获到新 token"
                    print(
                        f"未捕获到 {user.name} 的新 token，重试 {attempt}/3",
                        file=sys.stderr,
                    )
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    print(
                        f"登录 {user.name} 失败，重试 {attempt}/3：{last_error}",
                        file=sys.stderr,
                    )
            if not captured:
                _notify_failure(
                    notifier,
                    "微信 token 采集失败",
                    details={
                        **_user_details(user),
                        "原因": last_error,
                    },
                )
    finally:
        _clear_system_proxy(service)
        _stop_capture(proc)
    print(f"Token output: {config.token_output_dir}")
    return 0


def _command_harvest(config, notifier: WeChatNotifier | None) -> int:
    active_users = [user for user in config.users if not user.skip]
    if not active_users:
        print("没有需要采集的用户", file=sys.stderr)
        return 0
    return _harvest_users(config, active_users, notifier)


def _user_file_hash(config) -> str:
    return hashlib.sha256(config.user_file.read_bytes()).hexdigest()


def _print_reconcile_result(result, notifier: WeChatNotifier | None = None) -> None:
    print(f"跳过用户：{[(u.name, u.id_card) for u in result.skipped]}")
    if result.deleted_local:
        print(f"删除本地账户：{result.deleted_local}")
    for oss_result in result.oss_results:
        if oss_result.success:
            print(
                f"OSS 已删除 {oss_result.prefix}"
                f"（{oss_result.deleted_count} 个对象）"
            )
        else:
            print(
                f"OSS 删除 {oss_result.prefix} 失败：{oss_result.error}",
                file=sys.stderr,
            )
            _notify_failure(
                notifier,
                "OSS 账号目录删除失败",
                details={
                    "prefix": oss_result.prefix,
                    "异常": oss_result.error or "未知错误",
                },
            )
    print(f"待采集：{[(u.name, u.id_card) for u in result.to_harvest]}")


def _command_reconcile(config, notifier: WeChatNotifier | None) -> int:
    _print_reconcile_result(reconcile(config), notifier)
    return 0


def _relogin_signal_key(id_card: str) -> str:
    return f"hxacc/account/{id_card}/relogin"


def _stop_key(id_card: str) -> str:
    return f"hxacc/account/{id_card}/stop"


def _list_account_ids(oss) -> list[str]:
    ids: list[str] = []
    for prefix in oss.list_prefixes("hxacc/account/"):
        stripped = prefix.rstrip("/")
        if not stripped.startswith("hxacc/account/"):
            continue
        id_card = stripped.rsplit("/", 1)[-1]
        if id_card:
            ids.append(id_card)
    return ids


def _relogin_user(config, user, notifier) -> bool:
    account_path = config.account_dir / f"{user.id_card}.account"
    before = account_path.stat().st_mtime if account_path.exists() else 0.0
    try:
        _harvest_users(config, [user], notifier)
    except Exception as exc:
        _notify_failure(
            notifier,
            "重新登录执行失败",
            details={**_user_details(user), "异常": f"{type(exc).__name__}: {exc}"},
        )
    after = account_path.stat().st_mtime if account_path.exists() else 0.0
    return after > before


def _poll_relogin_signals(config, oss, notifier) -> None:
    if oss is None:
        return
    try:
        account_ids = _list_account_ids(oss)
    except Exception as exc:
        logger.warning("读取账号目录失败（%s），本轮不处理登录信号", type(exc).__name__)
        return
    for id_card in account_ids:
        key = _relogin_signal_key(id_card)
        try:
            raw = oss.get_optional_text(key)
        except Exception as exc:
            logger.warning(
                "读取重新登录标记失败：%s（%s），下轮重试", key, type(exc).__name__
            )
            continue
        if raw is None or not raw.strip():
            continue
        attempts = 0
        last_attempt_at = 0.0
        try:
            data = json.loads(raw)
            attempts = int(data.get("attempts", 0))
            last_attempt_at = float(data.get("last_attempt_at", 0) or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        user = next((u for u in config.users if u.id_card == id_card), None)
        if user is None or user.skip:
            oss.delete_object(key)
            continue
        if time.time() - last_attempt_at < RELOGIN_COOLDOWN_SECONDS:
            continue

        if _relogin_user(config, user, notifier):
            oss.delete_object(key)
            oss.delete_object(_stop_key(id_card))
            continue

        attempts += 1
        if attempts > RELOGIN_MAX_ATTEMPTS:
            _notify_failure(
                notifier,
                "重新登录超过 2 次仍失败，请马上修复",
                details={**_user_details(user), "已尝试": str(attempts)},
            )
            oss.delete_object(key)
        else:
            oss.upload_text(
                key,
                json.dumps(
                    {"attempts": attempts, "last_attempt_at": time.time()},
                    ensure_ascii=False,
                ),
            )


def _command_watch(config, notifier: WeChatNotifier | None) -> int:
    stop_event = threading.Event()
    bot_thread = None
    oss = _make_oss_client(config)
    if config.wecom_bot_id and config.wecom_bot_secret:
        bot_thread = start_bot_thread(
            bot_id=config.wecom_bot_id,
            secret=config.wecom_bot_secret,
            store=UserStore(config.user_file),
            stop_event=stop_event,
            oss=oss,
            account_dir=config.account_dir,
        )
        print(
            f"已启动企业微信机器人长连接（监听 {config.user_file} 的 user 变更）"
        )
    else:
        print(
            "未配置 WECOM_BOT_ID/WECOM_BOT_SECRET，跳过企业微信机器人长连接",
            file=sys.stderr,
        )

    last_hash = _user_file_hash(config)
    last_relogin_poll: float | None = None
    relogin_interval = seconds("RELOGIN_POLL_INTERVAL_SECONDS", 15)
    print(f"开始监控 {config.user_file}，间隔 {config.watch_interval_seconds}s")
    print(
        f"OSS 登录信号检查间隔 {relogin_interval}s，"
        f"目录缓存 {seconds('OSS_DIRECTORY_CACHE_SECONDS', 60)}s"
    )
    try:
        while True:
            time.sleep(config.watch_interval_seconds)
            try:
                new_config = load_config()
            except Exception as exc:
                print(f"配置读取失败：{exc}", file=sys.stderr)
                _notify_failure(
                    notifier,
                    "用户文件配置读取失败",
                    details={"异常": f"{type(exc).__name__}: {exc}"},
                )
                continue
            now = time.monotonic()
            if last_relogin_poll is None or now - last_relogin_poll >= relogin_interval:
                _poll_relogin_signals(new_config, oss, notifier)
                last_relogin_poll = time.monotonic()
            STATS.flush()
            new_hash = _user_file_hash(new_config)
            if new_hash == last_hash:
                continue
            last_hash = new_hash
            result = reconcile(new_config)
            _print_reconcile_result(result, notifier)
            if result.to_harvest:
                _harvest_users(new_config, result.to_harvest, notifier)
            config = new_config
    finally:
        STATS.flush(force=True)
        stop_event.set()
        if bot_thread is not None:
            bot_thread.join(timeout=3)


def _command_bot(config, notifier: WeChatNotifier | None) -> int:
    if not config.wecom_bot_id or not config.wecom_bot_secret:
        _notify_failure(
            notifier,
            "企业微信机器人配置缺失",
            details={"提示": "请在 .env 中配置 WECOM_BOT_ID 和 WECOM_BOT_SECRET"},
        )
        return 1
    stop_event = threading.Event()
    thread = start_bot_thread(
        bot_id=config.wecom_bot_id,
        secret=config.wecom_bot_secret,
        store=UserStore(config.user_file),
        stop_event=stop_event,
        oss=_make_oss_client(config),
    )
    print(
        f"企业微信机器人长连接已启动，监听 {config.user_file} 的 user 变更"
    )
    try:
        while thread.is_alive():
            thread.join(timeout=1)
    except KeyboardInterrupt:
        stop_event.set()
        thread.join(timeout=3)
    return 0


def _make_oss_client(config):
    if not (config.oss_bucket and config.oss_access_key_id and config.oss_access_key_secret):
        return None
    return OssClient(
        access_key_id=config.oss_access_key_id,
        access_key_secret=config.oss_access_key_secret,
        bucket_name=config.oss_bucket,
        endpoint=config.oss_endpoint,
    )


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # oss2 会把每个 HTTP 错误（含预期的 404 NoSuchKey）以 INFO 打到 oss2.api，
    # 这里只保留 WARNING 及以上，避免刷屏；真正的错误仍会通过异常/我们自己的日志暴露。
    logging.getLogger("oss2.api").setLevel(logging.WARNING)
    try:
        config = load_config()
    except Exception as exc:
        notifier = WeChatNotifier(
            os.getenv("WECOM_WEBHOOK_URL", DEFAULT_WECOM_WEBHOOK_URL),
            outbox_path=_outbox_path(),
        )
        _notify_failure(
            notifier,
            "采集配置加载失败",
            details={"异常": f"{type(exc).__name__}: {exc}"},
        )
        notifier.close()
        return 1

    notifier = _make_notifier(config)
    try:
        if args.command == "login":
            return _command_login(config, notifier)
        if args.command == "capture":
            return _command_capture(config, notifier)
        if args.command == "harvest":
            return _command_harvest(config, notifier)
        if args.command == "reconcile":
            return _command_reconcile(config, notifier)
        if args.command == "watch":
            return _command_watch(config, notifier)
        if args.command == "bot":
            return _command_bot(config, notifier)
    except Exception as exc:
        _notify_failure(
            notifier,
            "采集程序执行失败",
            details={"异常": f"{type(exc).__name__}: {exc}"},
        )
        return 1
    finally:
        if notifier is not None:
            notifier.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

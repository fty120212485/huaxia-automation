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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .accounts import Account, AccountLoadError, load_account_file, load_accounts
from .client import ApiError, HxaccClient
from .config import Config, load_config
from .oss_upload import OssAccountUploader
from .orchestrator import Orchestrator, _TaskCompletedAwaitResume
from .storage import RunStore
from .wechat import WeChatNotifier


DEFAULT_WECOM_WEBHOOK_URL = (
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
    "?key=de2d8b32-ca95-4117-b349-816feb0347d2"
)

COMPENSATION_LOCK = threading.Lock()
_AWAIT_RESUME_EXIT_CODE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Real-environment normal-path integration harness")
    parser.add_argument(
        "command",
        choices=["run", "watch", "resume", "workers"],
        help="Command to execute",
    )
    parser.add_argument(
        "--id-card",
        default=None,
        help="Account idCard, required by the resume command",
    )
    parser.add_argument(
        "--account-dir",
        default=os.getenv(
            "ACCOUNT_DIR",
            "../account",
        ),
        help="Directory containing .account JSON files",
    )
    parser.add_argument(
        "--account-file",
        default=None,
        help="Run a single .account file",
    )
    parser.add_argument(
        "--compensate",
        action="store_true",
        help="Only retry accounts recorded in the compensation queue",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=int(os.getenv("MAX_PARALLEL_ACCOUNTS", "3")),
        help="Maximum number of accounts to run in parallel",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List worker IDs (workers command)",
    )
    parser.add_argument(
        "--add",
        action="append",
        default=[],
        metavar="WORKER_ID",
        help="Add worker IDs to the shared list (workers command)",
    )
    parser.add_argument(
        "--remove",
        action="append",
        default=[],
        metavar="WORKER_ID",
        help="Remove worker IDs from the shared list (workers command)",
    )
    parser.add_argument(
        "--set",
        nargs="+",
        default=None,
        metavar="WORKER_ID",
        help="Replace the shared worker list (workers command)",
    )
    return parser


def configure_logging(
    log_path: Path,
    logger_name: str = "integration_harness",
) -> logging.Logger:
    logger = logging.getLogger(logger_name)
    logger.handlers.clear()
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def _make_session_id(index: int) -> str:
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{timestamp}-{index:02d}"


@dataclass
class _PreparedAccount:
    account: Account
    index: int
    session_id: str
    config: Config
    notifier: WeChatNotifier
    uploader: OssAccountUploader


def _compensation_queue_path() -> Path:
    path = Path(os.getenv("RUNS_DIR", "runs"))
    if not path.is_absolute():
        path = Path.cwd() / path
    return path / ".compensation_queue.jsonl"


def _append_compensation(account: Account, error: str) -> None:
    path = _compensation_queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with COMPENSATION_LOCK:
        with path.open("a", encoding="utf-8") as file:
            file.write(
                json.dumps(
                    {
                        "source_path": str(account.source_path),
                        "error": error,
                        "failed_at": datetime.now(timezone.utc).isoformat(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def _compensation_sources() -> list[str]:
    path = _compensation_queue_path()
    if not path.exists():
        return []
    with COMPENSATION_LOCK:
        lines = path.read_text(encoding="utf-8").splitlines()
    sources: list[str] = []
    seen: set[str] = set()
    for line in lines:
        try:
            payload = json.loads(line)
            source = payload.get("source_path")
        except json.JSONDecodeError:
            continue
        if isinstance(source, str) and source and source not in seen:
            seen.add(source)
            sources.append(source)
    return sources


def _remove_compensation(account: Account) -> None:
    source = str(account.source_path)
    path = _compensation_queue_path()
    if not path.exists():
        return
    with COMPENSATION_LOCK:
        lines = path.read_text(encoding="utf-8").splitlines()
        remaining: list[str] = []
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                remaining.append(line)
                continue
            if payload.get("source_path") != source:
                remaining.append(line)
        path.write_text(
            "\n".join(remaining) + ("\n" if remaining else ""),
            encoding="utf-8",
        )


def _is_in_run_window(now: datetime, start_hour: int, end_hour: int) -> bool:
    return start_hour <= now.hour < end_hour


def _run_one(
    config: Config,
    session_id: str,
    notifier: WeChatNotifier,
    id_card: str,
    name: str,
    replay: bool,
    auto_next_course: bool,
    oss_uploader: OssAccountUploader,
) -> int:
    run_dir = config.runs_dir / session_id
    run_dir.mkdir(parents=True, exist_ok=True)
    logger = configure_logging(
        run_dir / "run.log",
        logger_name=f"integration_harness.account.{id_card}",
    )

    store = RunStore(run_dir / "events.sqlite3")
    client = HxaccClient(config)
    store.create_session(
        session_id,
        config.token_prefix,
        datetime.now(timezone.utc).isoformat(),
    )

    orchestrator = Orchestrator(
        config=config,
        client=client,
        store=store,
        session_id=session_id,
        logger=logger,
        notifier=notifier,
        id_card=id_card,
        name=name,
        replay=replay,
        auto_next_course=auto_next_course,
        oss_uploader=oss_uploader,
    )

    try:
        orchestrator.run()
    except _TaskCompletedAwaitResume as exc:
        logger.info("任务已完成，等待恢复：%s", exc)
        store.update_session_state(
            session_id,
            "awaiting_resume",
            datetime.now(timezone.utc).isoformat(),
        )
        return _AWAIT_RESUME_EXIT_CODE
    except Exception as exc:
        logger.exception("运行失败：%s", exc)
        if not getattr(exc, "already_notified", False):
            ok = notifier.send_exception(
                app_id=config.app_id,
                token_prefix=config.token_prefix,
                device_id=config.device_id,
                session_id=session_id,
                exc=exc,
                id_card=id_card,
                name=name,
            )
            if not ok:
                logger.warning(
                    "运行异常的企业微信推送失败，已写入本地待发队列"
                )
        store.update_session_state(
            session_id,
            "failed",
            datetime.now(timezone.utc).isoformat(),
        )
        return 1
    finally:
        orchestrator.shutdown_qr_server()
        client.close()
        store.close()

    logger.info("运行完成，产物目录：%s", run_dir)
    print(f"Run directory: {run_dir}")
    return 0


def _run_prepared(task: _PreparedAccount) -> int:
    account = task.account
    config = task.config
    notifier = task.notifier
    uploader = task.uploader
    session_id = task.session_id

    try:
        oss_dir = f"hxacc/account/{account.id_card}"
        oss_key = f"{oss_dir}/{account.source_path.name}"
        upload_result = uploader.upload_file(account.source_path, oss_key)
        if not upload_result.success:
            print(
                f"OSS 上传失败 {oss_key}: {upload_result.error}",
                file=sys.stderr,
            )
            notifier.send_exception(
                app_id=config.app_id,
                token_prefix=config.token_prefix,
                device_id=config.device_id,
                session_id=session_id,
                exc=RuntimeError(f"OSS 上传失败 {oss_key}: {upload_result.error}"),
                id_card=account.id_card,
                name=account.name,
            )
            _append_compensation(
                account,
                f"OSS 上传失败 {oss_key}: {upload_result.error}",
            )
            return 1
        print(f"OSS 上传成功：{oss_key}")

        now = datetime.now()
        if not _is_in_run_window(
            now,
            config.run_start_hour,
            config.run_end_hour,
        ):
            notifier.send_time_window_blocked(
                app_id=config.app_id,
                token_prefix=config.token_prefix,
                device_id=config.device_id,
                start_hour=config.run_start_hour,
                end_hour=config.run_end_hour,
                id_card=account.id_card,
                name=account.name,
            )
            print(
                f"当前时间 {now:%H:%M:%S} 不在允许运行窗口内，跳过 appId={config.app_id}",
                file=sys.stderr,
            )
            return 0

        result = _run_one(
            config,
            session_id,
            notifier,
            account.id_card,
            account.name,
            account.replay,
            (
                account.auto_next_course
                if account.auto_next_course is not None
                else config.auto_next_course
            ),
            uploader,
        )
        if result == _AWAIT_RESUME_EXIT_CODE:
            print(f"{account.id_card} 任务已完成，等待恢复")
            return 0
        if result != 0:
            _append_compensation(
                account,
                f"课程学习执行失败，退出码：{result}",
            )
            return result

        success_key = (
            f"{oss_dir}/{datetime.now().strftime('%Y%m%d%H%M%S')}.success"
        )
        success_result = uploader.upload_text(success_key)
        if not success_result.success:
            print(
                f"OSS success 标记上传失败 {success_key}: {success_result.error}",
                file=sys.stderr,
            )
            notifier.send_exception(
                app_id=config.app_id,
                token_prefix=config.token_prefix,
                device_id=config.device_id,
                session_id=session_id,
                exc=RuntimeError(
                    f"OSS success 标记上传失败 {success_key}: {success_result.error}"
                ),
                id_card=account.id_card,
                name=account.name,
            )
            _append_compensation(
                account,
                f"OSS success 标记上传失败 {success_key}: {success_result.error}",
            )
            return 1

        print(f"OSS success 标记上传成功：{success_key}")

        finished_key = f"{oss_dir}/finished"
        finished_result = uploader.upload_text(finished_key, "")
        if not finished_result.success:
            print(
                f"OSS finished 标记上传失败 {finished_key}: {finished_result.error}",
                file=sys.stderr,
            )
            notifier.send_exception(
                app_id=config.app_id,
                token_prefix=config.token_prefix,
                device_id=config.device_id,
                session_id=session_id,
                exc=RuntimeError(
                    f"OSS finished 标记上传失败 {finished_key}: {finished_result.error}"
                ),
                id_card=account.id_card,
                name=account.name,
            )
            _append_compensation(
                account,
                f"OSS finished 标记上传失败 {finished_key}: {finished_result.error}",
            )
            return 1

        print(f"OSS finished 标记上传成功：{finished_key}")
        _remove_compensation(account)
        return 0
    except Exception as exc:
        print(f"{account.id_card} 执行失败：{exc}", file=sys.stderr)
        try:
            notifier.send_exception(
                app_id=config.app_id,
                token_prefix=config.token_prefix,
                device_id=config.device_id,
                session_id=session_id,
                exc=exc,
                id_card=account.id_card,
                name=account.name,
            )
        except Exception:
            pass
        _append_compensation(
            account,
            f"{type(exc).__name__}: {exc}",
        )
        return 1
    finally:
        notifier.close()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _account_file_paths(account_dir: Path) -> set[str]:
    account_dir = account_dir.expanduser()
    if not account_dir.exists():
        return set()
    return {
        str(path.resolve())
        for path in account_dir.glob("*.account")
        if path.is_file()
    }


def _single_account_command(account_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "integration_harness",
        "run",
        "--account-file",
        str(account_path),
    ]


def _token_expired(account_path: Path) -> bool:
    try:
        payload = json.loads(account_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return True
    expires_at = payload.get("tokenExpiresAt")
    if not isinstance(expires_at, (int, float)):
        return True
    return datetime.now().timestamp() >= float(expires_at)


def _ensure_account_file(
    uploader: OssAccountUploader,
    account_dir: Path,
    id_card: str,
) -> Path | None:
    account_path = account_dir / f"{id_card}.account"
    if account_path.exists():
        return account_path
    try:
        content = uploader.get_object_text(
            f"hxacc/account/{id_card}/{id_card}.account"
        )
    except Exception:
        content = ""
    if not content.strip():
        return None
    account_path.parent.mkdir(parents=True, exist_ok=True)
    account_path.write_text(content, encoding="utf-8")
    return account_path


def _account_slot(id_card: str, slot_count: int) -> int:
    digest = hashlib.sha256(id_card.encode("utf-8")).hexdigest()
    return int(digest, 16) % slot_count


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dotenv_bool(path: Path, key: str, default: bool = False) -> bool:
    if not path.exists():
        return default
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        raw_key, value = line.split("=", 1)
        if raw_key.strip() == key:
            return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _daily_limit_key(id_card: str) -> str:
    return f"hxacc/account/{id_card}/daily_limit"


WORKERS_KEY = "hxacc/workers"


def _oss_worker_ids(uploader: OssAccountUploader) -> list[str] | None:
    try:
        raw = uploader.get_object_text(WORKERS_KEY).strip()
    except Exception:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    ids = sorted({str(item).strip() for item in data if str(item).strip()})
    return ids or None


def _stop_watched_process(
    proc: subprocess.Popen,
    stdout_file,
    stderr_file,
) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    stdout_file.close()
    stderr_file.close()


def _watch_accounts(account_dir: Path) -> int:
    account_dir = account_dir.expanduser()
    config = load_config(
        {
            "HXACC_TOKEN": "watch-mode",
            "HXACC_DEVICE_ID": "watch-mode",
        }
    )
    interval = float(os.getenv("ACCOUNT_WATCH_INTERVAL_SECONDS", "2"))
    slot_count = int(os.getenv("ACCOUNT_SLOT_COUNT", "16"))
    worker_id = os.getenv("WORKER_ID", "device-01")
    active_worker_ids: list[str] = []
    my_worker_index = -1
    notifier = WeChatNotifier(
        config.wecom_webhook_url,
        outbox_path=config.runs_dir / ".wecom_outbox.jsonl",
    )
    uploader = OssAccountUploader(
        access_key_id=config.oss_access_key_id,
        access_key_secret=config.oss_access_key_secret,
        bucket_name=config.oss_bucket,
        endpoint=config.oss_endpoint,
    )
    log_dir = _project_root() / "runs" / "watch"
    log_dir.mkdir(parents=True, exist_ok=True)
    watched: dict[str, tuple[subprocess.Popen, object, object]] = {}
    account_signatures: dict[str, str] = {}
    finished_notified: set[str] = set()
    stop_notified: set[str] = set()
    expired_notified: set[str] = set()
    process_ttl_seconds = int(os.getenv("PROCESS_LEASE_SECONDS", "120"))
    last_renew: dict[str, float] = {}
    global_env_path = _project_root() / ".env"
    last_global_auto_next = getattr(config, "auto_next_course", False)
    print(f"开始监控 OSS hxacc/account/，本地账户目录 {account_dir}，扫描间隔 {interval}s")

    def process_key(id_card: str) -> str:
        return f"hxacc/account/{id_card}/{id_card}.process"

    def stop_key(id_card: str) -> str:
        return f"hxacc/account/{id_card}/stop"

    def daily_limit_date(id_card: str) -> str | None:
        key = _daily_limit_key(id_card)
        if not uploader.object_exists(key):
            return None
        try:
            return uploader.get_object_text(key).strip()
        except Exception:
            return ""

    def stop_reason(id_card: str) -> str | None:
        key = stop_key(id_card)
        if not uploader.object_exists(key):
            return None
        try:
            return uploader.get_object_text(key).strip()
        except Exception:
            return ""

    def notify_process_file_failure(action: str, key: str, error: str) -> None:
        notifier.send_markdown(
            "\n".join(
                [
                    f"⚠️ 进程标记文件{action}失败",
                    f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
                    f"key：{key}",
                    f"原因：{error}",
                ]
            )
        )

    def write_process_file(id_card: str) -> bool:
        key = process_key(id_card)
        now = datetime.now(timezone.utc)
        payload = {
            "worker_id": worker_id,
            "started_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=process_ttl_seconds)).isoformat(),
        }
        result = uploader.upload_text(
            key,
            json.dumps(payload, ensure_ascii=False),
        )
        if not result.success:
            print(f"写入进程标记失败 {key}: {result.error}", file=sys.stderr)
            notify_process_file_failure("写入", key, result.error or "未知错误")
            return False
        last_renew[id_card] = time.monotonic()
        return True

    def create_process_file(id_card: str) -> bool:
        ok = write_process_file(id_card)
        if ok:
            print(f"已创建进程标记：{process_key(id_card)}")
        return ok

    def delete_process_file(id_card: str) -> bool:
        key = process_key(id_card)
        result = uploader.delete_object(key)
        if not result.success:
            print(f"删除进程标记失败 {key}: {result.error}", file=sys.stderr)
            notify_process_file_failure("删除", key, result.error or "未知错误")
            return False
        print(f"已删除进程标记：{key}")
        return True

    def process_is_active(id_card: str) -> bool:
        key = process_key(id_card)
        try:
            content = uploader.get_object_text(key)
            payload = json.loads(content)
            expires_at = payload.get("expires_at")
            expires_timestamp = datetime.fromisoformat(expires_at).timestamp()
        except Exception:
            return False
        return datetime.now(timezone.utc).timestamp() < expires_timestamp

    def renew_process_file(id_card: str) -> None:
        if id_card not in watched:
            return
        last = last_renew.get(id_card, 0.0)
        if time.monotonic() - last < (process_ttl_seconds / 2):
            return
        write_process_file(id_card)

    def stop_all() -> None:
        for path, (proc, stdout_file, stderr_file) in list(watched.items()):
            delete_process_file(path)
            last_renew.pop(path, None)
            _stop_watched_process(proc, stdout_file, stderr_file)
            print(f"已停止：{path}")
        watched.clear()

    def stop_one(id_card: str) -> None:
        if id_card not in watched:
            return
        proc, stdout_file, stderr_file = watched.pop(id_card)
        delete_process_file(id_card)
        last_renew.pop(id_card, None)
        _stop_watched_process(proc, stdout_file, stderr_file)
        print(f"已停止：{id_card}")

    def start_one(id_card: str, account_path: Path) -> None:
        stdout_path = log_dir / f"{id_card}.out.log"
        stderr_path = log_dir / f"{id_card}.err.log"
        stdout_file = stdout_path.open("a", encoding="utf-8")
        stderr_file = stderr_path.open("a", encoding="utf-8")
        proc = subprocess.Popen(
            _single_account_command(account_path),
            cwd=_project_root(),
            stdout=stdout_file,
            stderr=stderr_file,
        )
        watched[id_card] = (proc, stdout_file, stderr_file)
        print(f"已启动：{id_card}")

    try:
        consecutive_oss_failures = 0
        while True:
            try:
                # 每轮从 OSS 重读节点名单，实现扩缩容热加载；读不到沿用上一轮结果。
                remote_ids = _oss_worker_ids(uploader)
                if remote_ids is not None:
                    active_worker_ids = remote_ids
                elif not active_worker_ids:
                    ids = [
                        item.strip()
                        for item in os.getenv("WORKER_IDS", worker_id).split(",")
                        if item.strip()
                    ]
                    if worker_id not in ids:
                        ids.append(worker_id)
                    active_worker_ids = sorted(set(ids))
                my_worker_index = (
                    active_worker_ids.index(worker_id)
                    if worker_id in active_worker_ids
                    else -1
                )

                current_global_auto_next = _dotenv_bool(
                    global_env_path,
                    "AUTO_NEXT_COURSE",
                    False,
                )
                if current_global_auto_next != last_global_auto_next:
                    print("全局自动下一门配置变更，重启所有运行中的学习进程")
                    stop_all()
                    last_global_auto_next = current_global_auto_next

                prefixes = uploader.list_prefixes("hxacc/account/")
                current_ids = {
                    prefix.rstrip("/").rsplit("/", 1)[-1]
                    for prefix in prefixes
                    if prefix.rstrip("/").startswith("hxacc/account/")
                }

                for id_card in sorted(current_ids):
                    slot = _account_slot(id_card, slot_count)
                    if slot % len(active_worker_ids) != my_worker_index:
                        continue

                    prefix = f"hxacc/account/{id_card}/"
                    limit_date = daily_limit_date(id_card)
                    today = datetime.now().astimezone().strftime("%Y-%m-%d")
                    if limit_date is not None:
                        if limit_date == today:
                            if id_card in watched:
                                stop_one(id_card)
                            elif uploader.object_exists(process_key(id_card)):
                                delete_process_file(id_card)
                            continue
                        if not uploader.delete_object(_daily_limit_key(id_card)).success:
                            print(
                                f"删除过期每日上限标记失败：{_daily_limit_key(id_card)}",
                                file=sys.stderr,
                            )
                        if uploader.object_exists(stop_key(id_card)):
                            uploader.delete_object(stop_key(id_card))

                    if uploader.object_exists(f"{prefix}finished"):
                        if id_card in watched:
                            stop_one(id_card)
                        elif uploader.object_exists(process_key(id_card)):
                            delete_process_file(id_card)
                        if id_card not in finished_notified:
                            notifier.send_markdown(
                                "\n".join(
                                    [
                                        "✅ 课程学习已完成",
                                        f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
                                        f"idCard：{id_card}",
                                    ]
                                )
                            )
                            finished_notified.add(id_card)
                        continue

                    if uploader.object_exists(stop_key(id_card)):
                        if id_card in watched:
                            stop_one(id_card)
                        elif uploader.object_exists(process_key(id_card)):
                            delete_process_file(id_card)
                        if (
                            stop_reason(id_card) != "task_completed"
                            and id_card not in stop_notified
                        ):
                            notifier.send_markdown(
                                "\n".join(
                                    [
                                        "⏹️ 已停止学习（收到停止指令）",
                                        f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
                                        f"idCard：{id_card}",
                                    ]
                                )
                            )
                            stop_notified.add(id_card)
                        continue

                    account_path = _ensure_account_file(
                        uploader, account_dir, id_card
                    )
                    if account_path is None:
                        print(
                            f"本地账户文件不存在且 OSS 无副本："
                            f"{account_dir / f'{id_card}.account'}",
                            file=sys.stderr,
                        )
                        continue

                    signature = _file_sha256(account_path)
                    if (
                        id_card in watched
                        and account_signatures.get(id_card) != signature
                    ):
                        print(f"账户配置变更，重启学习：{id_card}")
                        stop_one(id_card)
                    account_signatures[id_card] = signature

                    # 本机已管理的进程由 watched 跟踪，并在本轮末尾续期和回收。
                    # 仅准备启动/接管账号时读取远端租约，避免每轮重复读取自己的标记。
                    if id_card not in watched and uploader.object_exists(process_key(id_card)):
                        if process_is_active(id_card):
                            print(f"跳过账户 {id_card}：{id_card}.process 租约有效")
                            continue
                        print(f"清理过期进程标记：{process_key(id_card)}")
                        if not delete_process_file(id_card):
                            continue

                    expired = _token_expired(account_path)
                    if expired:
                        stop_one(id_card)
                        if id_card not in expired_notified:
                            notifier.send_markdown(
                                "\n".join(
                                    [
                                        "⚠️ Token 已过期，请重新获取 token",
                                        f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
                                        f"idCard：{id_card}",
                                        f"账户文件：{account_path}",
                                    ]
                                )
                            )
                            expired_notified.add(id_card)
                        continue

                    if id_card not in watched:
                        if create_process_file(id_card):
                            start_one(id_card, account_path)

                # 热加载交接：不再归本机管理的账号停掉并清租约，让新 owner 接管；
                # 本机被移出名单时（my_worker_index < 0）清空所有子进程转 standby。
                for id_card in list(watched):
                    owned = (
                        my_worker_index >= 0
                        and _account_slot(id_card, slot_count) % len(active_worker_ids)
                        == my_worker_index
                    )
                    if not owned:
                        stop_one(id_card)

                for id_card in list(watched):
                    if id_card not in current_ids:
                        stop_one(id_card)

                for id_card in list(watched):
                    renew_process_file(id_card)

                for id_card, (proc, stdout_file, stderr_file) in list(watched.items()):
                    if proc.poll() is None:
                        continue
                    delete_process_file(id_card)
                    last_renew.pop(id_card, None)
                    stdout_file.close()
                    stderr_file.close()
                    watched.pop(id_card, None)
                    print(f"已结束：{id_card}")
            except Exception as exc:
                consecutive_oss_failures += 1
                print(
                    f"OSS 扫描失败，等待后重试：{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                if consecutive_oss_failures >= 3:
                    notifier.send_markdown(
                        "\n".join(
                            [
                                "⚠️ OSS 扫描连续失败",
                                f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
                                f"连续失败次数：{consecutive_oss_failures}",
                                f"最后异常：{type(exc).__name__}: {exc}",
                            ]
                        )
                    )
                    consecutive_oss_failures = 0
            else:
                consecutive_oss_failures = 0

            time.sleep(interval)
    except KeyboardInterrupt:
        stop_all()
        return 0
    finally:
        notifier.close()


def _load_compensation_accounts() -> list[Account]:
    accounts: list[Account] = []
    for source in _compensation_sources():
        try:
            accounts.append(load_account_file(Path(source)))
        except Exception as exc:
            print(
                f"补偿账户读取失败，保留在队列中：{source}：{exc}",
                file=sys.stderr,
            )
    return accounts


def _prepare_accounts(accounts: list[Account]) -> tuple[list[_PreparedAccount], int]:
    prepared: list[_PreparedAccount] = []
    exit_code = 0
    for index, account in enumerate(accounts, 1):
        overrides = {
            "APP_ID": account.app_id,
            "HXACC_TOKEN": account.token,
            "HXACC_DEVICE_ID": account.device_id,
        }
        try:
            config = load_config(overrides)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            notifier = WeChatNotifier(DEFAULT_WECOM_WEBHOOK_URL)
            notifier.send_exception(
                app_id=account.app_id,
                token_prefix="<unknown>",
                device_id=account.device_id,
                session_id="<config-error>",
                exc=exc,
                id_card=account.id_card,
                name=account.name,
            )
            notifier.close()
            _append_compensation(account, f"{type(exc).__name__}: {exc}")
            exit_code = 2
            continue

        notifier = WeChatNotifier(
            config.wecom_webhook_url,
            outbox_path=config.runs_dir / ".wecom_outbox.jsonl",
        )
        notifier.flush_outbox()
        uploader = OssAccountUploader(
            access_key_id=config.oss_access_key_id,
            access_key_secret=config.oss_access_key_secret,
            bucket_name=config.oss_bucket,
            endpoint=config.oss_endpoint,
        )
        prepared.append(
            _PreparedAccount(
                account=account,
                index=index,
                session_id=_make_session_id(index),
                config=config,
                notifier=notifier,
                uploader=uploader,
            )
        )
    return prepared, exit_code


def _resume_account(id_card: str) -> int:
    config = load_config(
        {
            "HXACC_TOKEN": "resume-mode",
            "HXACC_DEVICE_ID": "resume-mode",
        }
    )
    uploader = OssAccountUploader(
        access_key_id=config.oss_access_key_id,
        access_key_secret=config.oss_access_key_secret,
        bucket_name=config.oss_bucket,
        endpoint=config.oss_endpoint,
    )
    stop_key = f"hxacc/account/{id_card}/stop"
    limit_key = _daily_limit_key(id_card)
    marker_keys = [
        key for key in (stop_key, limit_key) if uploader.object_exists(key)
    ]
    if not marker_keys:
        print(f"账户 {id_card} 没有停止标记，无需恢复", file=sys.stderr)
        return 0
    for key in marker_keys:
        result = uploader.delete_object(key)
        if not result.success:
            print(f"删除停止标记失败 {key}: {result.error}", file=sys.stderr)
            return 1

    print(f"已删除停止标记 {', '.join(marker_keys)}，账户 {id_card} 恢复学习")
    notifier = WeChatNotifier(config.wecom_webhook_url)
    notifier.send_markdown(
        "\n".join(
            [
                "✅ 已恢复学习",
                f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
                f"idCard：{id_card}",
            ]
        )
    )
    notifier.close()
    return 0


def _workers_command(args) -> int:
    config = load_config(
        {
            "HXACC_TOKEN": "workers-mode",
            "HXACC_DEVICE_ID": "workers-mode",
        }
    )
    uploader = OssAccountUploader(
        access_key_id=config.oss_access_key_id,
        access_key_secret=config.oss_access_key_secret,
        bucket_name=config.oss_bucket,
        endpoint=config.oss_endpoint,
    )
    current = _oss_worker_ids(uploader) or []
    if args.list:
        if current:
            print("\n".join(current))
        else:
            print("(未设置节点名单)", file=sys.stderr)
        return 0
    if args.set:
        new = sorted({item.strip() for item in args.set if item.strip()})
    else:
        new = sorted(set(current) | {item.strip() for item in args.add if item.strip()})
        new = sorted(set(new) - {item.strip() for item in args.remove if item.strip()})
    if not new:
        print("节点名单不能为空", file=sys.stderr)
        return 2
    result = uploader.upload_text(WORKERS_KEY, json.dumps(new, ensure_ascii=False))
    if not result.success:
        print(f"更新节点名单失败：{result.error}", file=sys.stderr)
        return 1
    print(f"节点名单已更新：{', '.join(new)}")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "watch":
        return _watch_accounts(Path(args.account_dir))

    if args.command == "workers":
        return _workers_command(args)

    if args.command == "resume":
        if not args.id_card:
            print("resume 命令需要 --id-card 参数", file=sys.stderr)
            return 2
        return _resume_account(args.id_card)

    if args.account_file:
        try:
            account = load_account_file(Path(args.account_file).expanduser())
        except AccountLoadError as exc:
            print(str(exc), file=sys.stderr)
            notifier = WeChatNotifier(DEFAULT_WECOM_WEBHOOK_URL)
            notifier.send_exception(
                app_id="<unknown>",
                token_prefix="<unknown>",
                device_id="<unknown>",
                session_id="<account-file-error>",
                exc=exc,
                id_card="<unknown>",
                name="<unknown>",
            )
            notifier.close()
            return 2
        prepared, exit_code = _prepare_accounts([account])
        if not prepared:
            return exit_code
        return _run_prepared(prepared[0])

    if args.compensate:
        accounts = _load_compensation_accounts()
        if not accounts:
            print("没有需要补偿执行的账户", file=sys.stderr)
            return 0
    else:
        try:
            accounts = load_accounts(Path(args.account_dir).expanduser())
        except AccountLoadError as exc:
            print(str(exc), file=sys.stderr)
            notifier = WeChatNotifier(DEFAULT_WECOM_WEBHOOK_URL)
            notifier.send_exception(
                app_id="<unknown>",
                token_prefix="<unknown>",
                device_id="<unknown>",
                session_id="<account-load-error>",
                exc=exc,
                id_card="<unknown>",
                name="<unknown>",
            )
            notifier.close()
            return 2

    prepared, exit_code = _prepare_accounts(accounts)
    if not prepared:
        return exit_code

    max_workers = max(1, min(args.max_workers, len(prepared)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_run_prepared, task): task
            for task in prepared
        }
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                task = futures[future]
                _append_compensation(
                    task.account,
                    f"{type(exc).__name__}: {exc}",
                )
                result = 1
            if result != 0:
                exit_code = max(exit_code, result)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

"""Bounded process-local OSS telemetry and caches; no object keys in summaries.

Kept standalone in each independently installed application.
"""
from __future__ import annotations

import atexit
import json
import math
import os
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any


def seconds(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} 必须为有限的非负秒数")
    return value


def purpose(key: str) -> str:
    if key.rstrip("/") == "hxacc/account":
        return "account_listing"
    if key == "hxacc/workers":
        return "worker_list"
    tail = key.rsplit("/", 1)[-1]
    if tail in {"relogin", "stop", "finished", "daily_limit", "account.json"}:
        return {"account.json": "account_manifest"}.get(tail, tail)
    for suffix, label in ((".process", "lease"), (".account", "account_token"),
                          (".png", "qr_image"), (".json", "qr_metadata"),
                          (".success", "success_marker")):
        if tail.endswith(suffix):
            return label
    return "other"


def _byte_label(size: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f}B" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024


def format_report(report: dict) -> str:
    if os.getenv("OSS_STATS_FORMAT", "summary").strip().lower() == "json":
        return "OSS_STATS " + json.dumps(report, ensure_ascii=False, separators=(",", ":"))
    rows = report["operations"]
    totals = {field: sum(row[field] for row in rows) for field in (
        "count", "success", "404", "errors", "downloaded_bytes", "uploaded_bytes",
    )}
    component = {"integration_harness": "学习", "wechat_login_harvester": "采集"}.get(
        report["component"], report["component"]
    )
    node = " ".join(str(report["worker"] or report["host"]).split())
    return (
        f"OSS_STATS [{component}/{node} pid={report['pid']}] "
        f"{report['window_seconds']:.1f}秒 请求{totals['count']} "
        f"成功{totals['success']} 未找到{totals['404']} 异常{totals['errors']} "
        f"缓存命中{sum(report['cache_hits'].values())} "
        f"下行{_byte_label(totals['downloaded_bytes'])} 上行{_byte_label(totals['uploaded_bytes'])}"
    )


class CallStats:
    def __init__(self, component: str, *, clock=None, emit=None, interval=60.0):
        self.component = component
        self.clock = clock or time.monotonic
        self.emit = emit or (lambda report: print(format_report(report), flush=True))
        self.interval = interval
        self.lock = threading.Lock()
        self.started = self.clock()
        self.rows: dict[tuple[str, str], dict] = {}
        self.cache_hits: dict[str, int] = {}

    def call(self, operation: str, key: str, fn: Callable, *, uploaded=0):
        started = self.clock()
        outcome, downloaded, sent = "success", 0, 0
        try:
            result = fn()
            if operation == "EXISTS" and result is False:
                outcome = "404"
            if operation == "GET" and isinstance(result, bytes):
                downloaded = len(result)
            sent = uploaded
            return result
        except Exception as exc:
            status = getattr(exc, "status", None)
            outcome = "404" if status == 404 else (
                f"{int(status) // 100}xx" if isinstance(status, int) else "transport_error")
            raise
        finally:
            with self.lock:
                row = self.rows.setdefault((operation, purpose(key)), {
                    "count": 0, "success": 0, "404": 0, "errors": 0,
                    "downloaded_bytes": 0, "uploaded_bytes": 0,
                    "duration_ms": 0.0, "max_duration_ms": 0.0, "status_classes": {},
                })
                elapsed = max(0.0, (self.clock() - started) * 1000)
                row["count"] += 1
                row["status_classes"][outcome] = row["status_classes"].get(outcome, 0) + 1
                row[outcome if outcome in {"success", "404"} else "errors"] += 1
                row["downloaded_bytes"] += downloaded
                row["uploaded_bytes"] += sent
                row["duration_ms"] += elapsed
                row["max_duration_ms"] = max(row["max_duration_ms"], elapsed)
            self.flush()

    def cache_hit(self, key):
        label = purpose(key) if isinstance(key, str) else "other"
        with self.lock:
            self.cache_hits[label] = self.cache_hits.get(label, 0) + 1

    def flush(self, *, force=False):
        with self.lock:
            now = self.clock()
            if (not self.rows and not self.cache_hits) or (not force and now - self.started < self.interval):
                return None
            report = {
                "component": self.component, "host": socket.gethostname(),
                "worker": os.getenv("WORKER_ID", ""), "pid": os.getpid(),
                "window_seconds": round(max(0.0, now - self.started), 3),
                "cache_hits": dict(self.cache_hits),
                "operations": [dict(operation=op, purpose=label, **row)
                               for (op, label), row in sorted(self.rows.items())],
            }
            self.rows = {}
            self.cache_hits = {}
            self.started = now
        try:
            self.emit(report)
        except Exception:
            # A broken log sink must not change the outcome of an OSS request.
            pass
        return report


STATS = CallStats(__package__ or "oss")
atexit.register(lambda: STATS.flush(force=True))


def call(operation: str, key: str, fn: Callable, *, uploaded=0):
    return STATS.call(operation, key, fn, uploaded=uploaded)


def file_size(path) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


class ReadCache:
    """Cache complete successful results, including absence; errors never refresh TTL."""
    def __init__(self, *, clock=None, capacity=2048):
        self.clock = clock or (lambda: time.monotonic())
        self.capacity = capacity
        self.lock = threading.RLock()
        self.entries: OrderedDict[Any, tuple[float, Any, Any]] = OrderedDict()

    def get(self, key, ttl: float, loader: Callable, *, version=None):
        if ttl <= 0:
            return loader()
        with self.lock:
            cached = self.entries.get(key)
            if cached and self.clock() < cached[0] and version == cached[1]:
                self.entries.move_to_end(key)
                STATS.cache_hit(key)
                return cached[2]
            result = loader()
            self.entries[key] = (self.clock() + ttl, version, result)
            self.entries.move_to_end(key)
            while len(self.entries) > self.capacity:
                self.entries.popitem(last=False)
            return result

    def invalidate(self, key):
        with self.lock:
            self.entries.pop(key, None)

    def retain(self, predicate: Callable):
        with self.lock:
            for key in list(self.entries):
                if not predicate(key):
                    self.entries.pop(key)

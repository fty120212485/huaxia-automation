"""Read a shared KDocs workbook using a dedicated, persistent browser session."""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import hashlib
import json
import logging
from pathlib import Path
import re
import threading
import sys
import time
from urllib.parse import urlsplit

from .bot import UserStore
from .web_login import private_json

logger = logging.getLogger(__name__)


def failure_reason(exc):
    if isinstance(exc, ModuleNotFoundError):
        return f"缺少依赖 {exc.name}；当前 Python：{sys.executable}，请使用项目 .venv/bin/python 启动，或在该环境安装项目依赖"
    return str(exc) if isinstance(exc, KdocsError) else type(exc).__name__


class KdocsError(RuntimeError):
    pass


class KdocsLoginRequired(KdocsError):
    pass


class KdocsPermissionDenied(KdocsError):
    pass


@dataclass
class Snapshot:
    candidates: list[dict]
    blocked: int = 0
    incomplete: int = 0
    conflicts: int = 0


def _text(value):
    return str(value).strip() if value is not None else ''


def _label(value):
    return re.sub(r'\s+', '', _text(value))


def parse_rows(rows) -> Snapshot:
    """Require named status columns; never infer them from arbitrary remarks."""
    header = None
    names = {'姓名', 'name'}
    ids = {'身份证号码', '身份证号', '身份证', '证件号', '证件号码', 'id_card'}
    sync = {'已同步', '同步状态'}
    auth = {'认证完成', '认证状态', '认证'}
    groups = (names, ids, sync, auth)
    result = Snapshot([])
    by_id = {}
    forbidden = set()
    for row_number, row in enumerate(rows, 1):
        if row_number > 100_000:
            raise KdocsError('表格超过十万行，停止本轮读取')
        if header is None:
            labels = [_label(c) for c in row]
            indices = [[i for i, label in enumerate(labels) if label in group] for group in groups]
            if all(len(x) == 1 for x in indices):
                header = [x[0] for x in indices]
            elif row_number >= 20:
                raise KdocsError('找不到唯一的姓名、证件号、已同步、认证完成表头')
            continue
        values = [row[i] if i < len(row) else None for i in header]
        name, raw_id, synced, certified = values
        ident = _text(raw_id)
        key = ident.upper()
        # Numeric cells longer than Excel's 15-digit precision cannot be trusted.
        unsafe = (not ident or ident in {'.', '..'} or
                  any(c in '/\\' or ord(c) < 32 or ord(c) == 127 for c in ident) or
                  isinstance(raw_id, (int, float)) and len(ident) > 15)
        if unsafe or not _text(name):
            result.incomplete += 1
            continue
        statuses = re.split(r'[/|,，、;；]+', _label(synced) + '/' + _label(certified))
        if any(value in {'已同步', '认证完成', '已删除'} for value in statuses):
            forbidden.add(key)
            result.blocked += 1
            continue
        if _label(name) in names and _label(raw_id) in ids:
            continue
        item = {'name': _text(name), 'id_card': ident, 'skip': False}
        if key in by_id and by_id[key]['name'] != item['name']:
            forbidden.add(key)
            result.conflicts += 1
        else:
            by_id[key] = item
    if header is None:
        raise KdocsError('未找到表格表头，未新增任何账号')
    result.candidates = [v for k, v in by_id.items() if k not in forbidden]
    return result


def parse_workbook(content: bytes, sheet: str) -> Snapshot:
    from openpyxl import load_workbook
    from zipfile import ZipFile
    if len(content) > 20 * 1024 * 1024:
        raise KdocsError('Excel 文件超过 20MB')
    try:
        with ZipFile(BytesIO(content)) as archive:
            if sum(i.file_size for i in archive.infolist()) > 100 * 1024 * 1024:
                raise KdocsError('Excel 解压内容超过 100MB')
        book = load_workbook(BytesIO(content), read_only=True, data_only=True)
        try:
            if not sheet:
                matches = []
                for tab in book:
                    try:
                        matches.append(parse_rows(tab.iter_rows(values_only=True)))
                    except KdocsError:
                        continue
                if len(matches) != 1:
                    raise KdocsError('无法唯一确定账号工作表，请配置 KDOCS_SHEET')
                return matches[0]
            if sheet not in book.sheetnames:
                raise KdocsError('配置的工作表不存在')
            return parse_rows(book[sheet].iter_rows(values_only=True))
        finally:
            book.close()
    except KdocsError:
        raise
    except Exception as exc:
        raise KdocsError('导出内容不是可读取的 Excel 文件') from exc


class NewAccountDetector:
    """Two consecutive successful snapshots before additive import."""
    def __init__(self, store: UserStore, state_path: Path):
        self.store = store
        self.state_path = state_path
        self.previous = set()
        self.seen = set(json.loads(state_path.read_text()).get('seen', [])) if state_path.exists() else set()

    def reset(self):
        self.previous.clear()

    def observe(self, snapshot: Snapshot) -> int:
        now = {(x['id_card'].upper(), x['name']) for x in snapshot.candidates}
        stable = now & self.previous
        self.previous = now
        ready = [x for x in snapshot.candidates if (x['id_card'].upper(), x['name']) in stable
                 and x['id_card'].upper() not in self.seen]
        added = self.store.add_many_new(ready)
        self.seen.update(x['id_card'].upper() for x in ready)
        if ready:
            private_json(self.state_path, {'seen': sorted(self.seen)})
        return added


class KdocsReader:
    def __init__(self, config, *, headed=False):
        self.config = config
        url = urlsplit(config.kdocs_url)
        if url.scheme != 'https' or url.hostname != 'www.kdocs.cn' or not re.fullmatch(r'/l/[A-Za-z0-9]+', url.path):
            raise KdocsError('KDOCS_URL 必须是 https://www.kdocs.cn/l/ 分享链接')
        self.url = 'https://www.kdocs.cn' + url.path
        self.alias = url.path.rsplit('/', 1)[-1]
        self.headed = headed
        self.runtime = self.context = self.page = None
        self._cached = None
        self._version = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        profile = self.config.kdocs_profile_dir
        profile.mkdir(parents=True, exist_ok=True)
        profile.chmod(0o700)
        self.runtime = sync_playwright().start()
        try:
            self.context = self.runtime.chromium.launch_persistent_context(
                str(profile.resolve()), channel='chrome', headless=not self.headed, accept_downloads=True)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.goto(self.url, wait_until='domcontentloaded', timeout=60_000)
            # A saved session may briefly visit account.kdocs.cn while SSO
            # refreshes it. DOMContentLoaded is not the end of that redirect.
            if not self.headed:
                self.wait_for_document()
            return self
        except KdocsLoginRequired:
            self.__exit__(None, None, None)
            raise
        except Exception:
            self.__exit__(None, None, None)
            raise KdocsError('专用浏览器启动失败：请关闭占用该配置目录的浏览器后重试') from None

    def wait_for_document(self):
        if urlsplit(self.page.url).hostname == 'www.kdocs.cn':
            return
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        try:
            self.page.wait_for_url(
                lambda url: urlsplit(url).hostname == 'www.kdocs.cn',
                wait_until='domcontentloaded', timeout=15_000)
        except PlaywrightTimeoutError:
            raise KdocsLoginRequired('等待金山单点登录跳转 15 秒后仍未返回文档，请运行 kdocs-login') from None

    def __exit__(self, *args):
        try:
            if self.context:
                self.context.close()
        finally:
            if self.runtime:
                self.runtime.stop()

    def request(self, path, body=None):
        # Requests stay in the authorized site's browser context. No copied cookie files.
        self.wait_for_document()
        script = '''async ({path,body}) => {
            const controller=new AbortController(); const timer=setTimeout(()=>controller.abort(),30000);
            try {const r=await fetch(path,{method:body?'POST':'GET',credentials:'include',
              headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined,
              cache:'no-store',signal:controller.signal});
              let data;try{data=await r.json()}catch{return {status:r.status,data:null}};
              return {status:r.status,data};}finally{clearTimeout(timer)}
        }'''
        from playwright.sync_api import Error as PlaywrightError
        for attempt in range(3):
            try:
                self.wait_for_document()
                result = self.page.evaluate(script, {'path': path, 'body': body})
                break
            except PlaywrightError as exc:
                if 'Execution context was destroyed' not in str(exc) or attempt == 2:
                    raise KdocsError('浏览器请求被中断，请稍后重试') from None
                self.page.wait_for_load_state('domcontentloaded', timeout=30_000)
        data = result.get('data')
        stage = '导出 Excel' if path.endswith('/export') else '读取文档信息'
        if isinstance(data, dict) and data.get('result') in {'userNotLogin', 'SessionDeleted', 'notLogin', 'clinkUserNotLogin'}:
            raise KdocsLoginRequired(f"{stage}返回 {data['result']}，请运行 kdocs-login 重新登录")
        if isinstance(data, dict) and data.get('result') == 'forbidProtectedFile':
            raise KdocsPermissionDenied('文档设置了“内容自动隐藏”或“保护区域禁止查看”，不支持导出（forbidProtectedFile）；请文档所有者检查内容保护设置')
        if result['status'] == 401:
            raise KdocsLoginRequired(f"{stage}返回 HTTP 401，请运行 kdocs-login 重新登录")
        if result['status'] == 403:
            raise KdocsPermissionDenied(f"{stage}返回 HTTP 403：访问权限不足，请检查文档权限")
        if result['status'] != 200 or not isinstance(data, dict):
            raise KdocsError('金山网页接口返回异常，未新增账号')
        return data

    def metadata(self):
        data = self.request('/api/v3/office/file/' + self.alias)
        file = data.get('file')
        if not isinstance(file, dict) or not file.get('id'):
            raise KdocsError('无法读取分享文档信息')
        return file

    def snapshot(self):
        before = self.metadata()
        version = (before['id'], before.get('version'), before.get('modify_time'))
        if before.get('version') is not None and version == self._version:
            return self._cached
        result = self.request('/api/v3/office/file/' + str(before['id']) + '/export',
                              {'format': 'xlsx', 'store_type': 'ks3'})
        url = result.get('url') or (result.get('fileinfo') or {}).get('url')
        if not url:
            raise KdocsError('金山未返回 Excel 下载地址，未新增账号')
        target = urlsplit(url)
        allowed = ('kdocs.cn', 'wps.cn', 'wpscdn.cn', 'ksyun.com', 'ks3-cn-beijing.ksyun.com')
        if target.scheme != 'https' or not target.hostname or not any(
                target.hostname == h or target.hostname.endswith('.' + h) for h in allowed):
            raise KdocsError('导出下载地址不属于受信任的金山域名')
        # A separate HTTP client sends no account cookies to the signed-download host.
        import httpx
        import ssl
        import truststore
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with httpx.Client(verify=context, timeout=30, follow_redirects=False) as client:
            with client.stream('GET', url) as response:
                if response.status_code != 200:
                    raise KdocsError('Excel 下载失败')
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 20 * 1024 * 1024:
                        raise KdocsError('Excel 文件超过 20MB')
        after = self.metadata()
        if before.get('version') != after.get('version') or before.get('modify_time') != after.get('modify_time'):
            raise KdocsError('表格正在修改，等待下一次稳定版本')
        snapshot = parse_workbook(bytes(content), self.config.kdocs_sheet)
        self._cached, self._version = snapshot, version
        return snapshot


def state_path(config):
    key = hashlib.sha256((config.kdocs_url + '#' + config.kdocs_sheet).encode()).hexdigest()[:16]
    return config.token_output_dir / 'kdocs' / (key + '.json')


def start_kdocs_thread(config, stop_event, notifier=None):
    def work():
        detector = NewAccountDetector(UserStore(config.user_file), state_path(config))
        last_notice = 0.0
        while not stop_event.is_set():
            try:
                with KdocsReader(config) as reader:
                    while not stop_event.is_set():
                        try:
                            snapshot = reader.snapshot()
                            added = detector.observe(snapshot)
                            if added:
                                logger.info('金山表格已新增 %d 个账号', added)
                        except Exception as exc:
                            detector.reset()
                            if isinstance(exc, (KdocsLoginRequired, KdocsPermissionDenied)):
                                raise
                            logger.warning('金山表格本轮读取失败：%s，未新增账号', failure_reason(exc))
                        if stop_event.wait(config.kdocs_poll_seconds):
                            break
            except Exception as exc:
                detector.reset()
                reason = failure_reason(exc)
                logger.warning('金山表格监控暂停：%s', reason)
                if notifier and time.monotonic() - last_notice > 300:
                    if isinstance(exc, ModuleNotFoundError):
                        action = '请使用项目 .venv/bin/python 启动，并在同一环境运行 -m pip install -e .'
                    elif isinstance(exc, KdocsPermissionDenied):
                        action = '请文档所有者检查内容自动隐藏、保护区域禁止查看及导出权限；普通重新登录不能解除文档保护'
                    elif isinstance(exc, KdocsLoginRequired):
                        action = '运行 kdocs-login 登录专用浏览器'
                    else:
                        action = '检查网络、工作表配置和专用浏览器目录是否被占用'
                    notifier.send_failure('金山表格读取失败', details={'原因': reason, '处理': action})
                    last_notice = time.monotonic()
                stop_event.wait(30)
    thread = threading.Thread(target=work, name='kdocs-sync', daemon=True)
    thread.start()
    return thread

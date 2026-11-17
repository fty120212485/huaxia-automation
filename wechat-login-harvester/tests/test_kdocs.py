from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json

import pytest
from openpyxl import Workbook

from wechat_login_harvester.bot import UserStore
from wechat_login_harvester.kdocs import KdocsError, NewAccountDetector, Snapshot, parse_rows, parse_workbook

HEADER = ['日期', '姓名', '身份证号码', '已同步', '认证完成', '备注']


def test_status_filter_is_either_column_and_not_remarks():
    result = parse_rows([HEADER,
        ['', 'A', 'A123', '已同步', '', ''],
        ['', 'B', 'B123', '', '认证完成', ''],
        ['', 'C', 'Z00000000', '', '认证中', '认证完成'],
        ['', 'D', 'D123', ' 已 同 步 ', '', ''],
    ])
    assert result.blocked == 3
    assert [x['id_card'] for x in result.candidates] == ['Z00000000']


def test_any_duplicate_completed_row_blocks_the_same_id():
    result = parse_rows([HEADER, ['', 'A', 'abc', '', '', ''], ['', 'A', 'ABC', '', '认证完成', '']])
    assert not result.candidates


@pytest.mark.parametrize('synced,certified', [('已删除', ''), ('', '已删除'), (' 已 删 除 ', ''), ('已删除/待处理', '')])
def test_deleted_status_blocks_duplicate_and_cancels_pending(tmp_path, synced, certified):
    store = UserStore(tmp_path/'user')
    detector = NewAccountDetector(store, tmp_path/'state.json')
    pending = parse_rows([HEADER, ['', 'A', 'Z00000000', '', '', '']])
    assert detector.observe(pending) == 0
    deleted = parse_rows([HEADER, ['', 'A', 'Z00000000', '', '', ''],
                         ['', 'A', 'Z00000000', synced, certified, '']])
    assert deleted.blocked == 1
    assert not deleted.candidates
    assert detector.observe(deleted) == 0
    assert not store.read()


def test_deleted_in_remark_is_not_a_status():
    snapshot = parse_rows([HEADER, ['', 'A', 'Z00000000', '', '', '已删除']])
    assert len(snapshot.candidates) == 1


def test_conflicting_names_and_incomplete_or_precision_lost_ids_are_not_added():
    result = parse_rows([HEADER,
        ['', 'A', 'id1', '', '', ''], ['', 'B', 'id1', '', '', ''],
        ['', '', 'id2', '', '', ''], ['', 'C', 110101199001010016, '', '', ''],
        ['', 'E', '../escape', '', '', '']])
    assert not result.candidates
    assert result.conflicts == 1
    assert result.incomplete == 3


def test_missing_status_columns_fails_closed():
    with pytest.raises(KdocsError):
        parse_rows([['姓名', '身份证号码'], ['A', 'id']])


def workbook_bytes(two_sheets=False):
    book = Workbook()
    book.active.title = 'Sheet1 (2)'
    book.active.append(HEADER)
    book.active.append(['', 'A', 'Z00000000', '', '', ''])
    if two_sheets:
        book.copy_worksheet(book.active)
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_reads_actual_export_name_and_requires_unambiguous_sheet():
    assert len(parse_workbook(workbook_bytes(), '').candidates) == 1
    with pytest.raises(KdocsError):
        parse_workbook(workbook_bytes(True), '')
    assert len(parse_workbook(workbook_bytes(True), 'Sheet1 (2)').candidates) == 1


def test_invalid_export_does_not_become_an_empty_table():
    with pytest.raises(KdocsError):
        parse_workbook(b'<html>login</html>', '')


def test_two_successive_observations_then_durable_dedup(tmp_path):
    store = UserStore(tmp_path/'user')
    store.write([{'name': 'local', 'id_card': 'existing', 'skip': True, 'custom': 1}])
    state = tmp_path/'state.json'
    detector = NewAccountDetector(store, state)
    snapshot = Snapshot([{'name': 'A', 'id_card': 'Z00000000', 'skip': False},
                         {'name': 'changed', 'id_card': 'EXISTING', 'skip': False}])
    assert detector.observe(snapshot) == 0
    assert detector.observe(snapshot) == 1
    assert store.read()[0] == {'name': 'local', 'id_card': 'existing', 'skip': True, 'custom': 1}
    assert detector.observe(snapshot) == 0
    # Deleting a local account must not cause the same sheet record to be re-added.
    store.delete('Z00000000')
    detector = NewAccountDetector(store, state)
    detector.observe(snapshot)
    assert detector.observe(snapshot) == 0


def test_edited_or_completed_row_cancels_pending_addition(tmp_path):
    store = UserStore(tmp_path/'user')
    detector = NewAccountDetector(store, tmp_path/'state.json')
    a = Snapshot([{'name': 'A', 'id_card': 'M1'}])
    b = Snapshot([{'name': 'B', 'id_card': 'M1'}])
    assert detector.observe(a) == 0
    assert detector.observe(b) == 0
    assert detector.observe(Snapshot([], blocked=1)) == 0
    assert not store.read()
    detector.observe(a)
    detector.reset()  # Network failure breaks consecutive-snapshot stability.
    assert detector.observe(a) == 0


def test_parallel_bot_and_sheet_additions_are_not_lost(tmp_path):
    path = tmp_path/'user'
    def add(i):
        store = UserStore(path)
        if i % 2:
            store.add({'name': f'N{i}', 'id_card': f'I{i}'})
        else:
            store.add_many_new([{'name': f'N{i}', 'id_card': f'I{i}'}])
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(add, range(40)))
    assert len(UserStore(path).read()) == 40
    assert len(json.loads(path.read_text())) == 40


def test_watch_receives_sheet_additions_through_existing_user_flow(tmp_path, monkeypatch):
    from dataclasses import replace
    from wechat_login_harvester import __main__ as main
    from wechat_login_harvester.config import load_config
    import wechat_login_harvester.kdocs as kdocs
    monkeypatch.chdir(tmp_path)
    store = UserStore(tmp_path/'user')
    store.write([{'name': 'old', 'id_card': 'old', 'skip': False}])
    cfg = replace(load_config(), kdocs_url='https://www.kdocs.cn/l/cm89y8KI1qbf',
                  account_dir=tmp_path/'account', wecom_bot_id='', wecom_bot_secret='')
    cfg.account_dir.mkdir()
    (cfg.account_dir/'old.account').write_text('{}')
    monkeypatch.setattr(main, 'load_config', lambda: replace(
        load_config(), kdocs_url=cfg.kdocs_url, account_dir=cfg.account_dir,
        wecom_bot_id='', wecom_bot_secret=''))
    monkeypatch.setattr(main, '_make_oss_client', lambda _: None)
    monkeypatch.setattr(main, '_poll_relogin_signals', lambda *args: None)
    class Worker:
        def join(self, timeout=None):
            pass
    monkeypatch.setattr(kdocs, 'start_kdocs_thread', lambda *args: Worker())
    detector = NewAccountDetector(store, tmp_path/'state.json')
    snapshots = parse_rows([HEADER, ['', 'new', 'Z00000000', '', '', ''],
                           ['', 'blocked', 'blocked', '已同步', '', '']])
    calls = []
    turns = [0]
    def tick(_):
        turns[0] += 1
        if turns[0] > 1:
            raise KeyboardInterrupt
        detector.observe(snapshots)
        detector.observe(snapshots)
    monkeypatch.setattr(main.time, 'sleep', tick)
    monkeypatch.setattr(main, '_harvest_users', lambda config, users, notifier: calls.extend(u.id_card for u in users))
    with pytest.raises(KeyboardInterrupt):
        main._command_watch(cfg, None)
    assert calls == ['Z00000000']


@pytest.mark.parametrize('status,result,error_type,reason', [
    (403, 'forbidProtectedFile', 'permission', '内容自动隐藏'),
    (403, 'permissionDenied', 'permission', 'HTTP 403'),
    (401, 'unauthorized', 'login', 'HTTP 401'),
    (200, 'SessionDeleted', 'login', 'SessionDeleted'),
    (403, 'clinkUserNotLogin', 'login', 'clinkUserNotLogin'),
])
def test_request_distinguishes_login_from_document_protection(status, result, error_type, reason):
    from types import SimpleNamespace
    from wechat_login_harvester.kdocs import KdocsReader, KdocsLoginRequired, KdocsPermissionDenied
    reader = KdocsReader(SimpleNamespace(kdocs_url='https://www.kdocs.cn/l/test'))
    reader.page = SimpleNamespace(url='https://www.kdocs.cn/l/test', evaluate=lambda *args: {
        'status': status, 'data': {'result': result}})
    expected = KdocsPermissionDenied if error_type == 'permission' else KdocsLoginRequired
    with pytest.raises(expected, match=reason):
        reader.request('/api/v3/office/file/123/export', {'format': 'xlsx'})


def test_saved_session_can_finish_sso_before_auth_check():
    from types import SimpleNamespace
    from wechat_login_harvester.kdocs import KdocsReader
    reader = KdocsReader(SimpleNamespace(kdocs_url='https://www.kdocs.cn/l/test'))
    page = SimpleNamespace(url='https://account.kdocs.cn/passport/singlesign')
    def finish(predicate, **kwargs):
        assert kwargs['timeout'] == 15000
        page.url = 'https://www.kdocs.cn/l/test'
        assert predicate(page.url)
    page.wait_for_url = finish
    page.evaluate = lambda *args: {'status': 200, 'data': {'file': {'id': '123'}}}
    reader.page = page
    assert reader.metadata()['id'] == '123'


def test_request_retries_a_navigation_interruption():
    from types import SimpleNamespace
    from playwright.sync_api import Error
    from wechat_login_harvester.kdocs import KdocsReader
    reader = KdocsReader(SimpleNamespace(kdocs_url='https://www.kdocs.cn/l/test'))
    calls = []
    def evaluate(*args):
        calls.append(True)
        if len(calls) == 1:
            raise Error('Execution context was destroyed, most likely because of a navigation')
        return {'status': 200, 'data': {'file': {'id': '123'}}}
    reader.page = SimpleNamespace(url='https://www.kdocs.cn/l/test', evaluate=evaluate,
                                  wait_for_load_state=lambda *args, **kwargs: None)
    assert reader.metadata()['id'] == '123'
    assert len(calls) == 2


def test_missing_dependency_diagnostic_names_module_and_interpreter():
    import sys
    from wechat_login_harvester.kdocs import failure_reason
    message = failure_reason(ModuleNotFoundError(name='playwright'))
    assert 'playwright' in message
    assert sys.executable in message
    assert '.venv/bin/python' in message

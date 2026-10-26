from concurrent.futures import ThreadPoolExecutor

import pytest

from wechat_login_harvester.oss_runtime import CallStats, ReadCache, seconds


def test_stats_aggregate_payload_and_never_include_object_identity():
    now = [0.0]
    reports = []
    stats = CallStats('test', clock=lambda: now[0], emit=reports.append)
    key = 'hxacc/account/secret-id/secret-token.account'
    def read():
        now[0] += .25
        return b'secret-token'
    assert stats.call('GET', key, read) == b'secret-token'
    stats.call('EXISTS', 'hxacc/account/secret-id/stop', lambda: False)
    stats.call('PUT', key, lambda: None, uploaded=20)
    class Forbidden(Exception):
        status = 403
    with pytest.raises(Forbidden):
        stats.call('GET', key, lambda: (_ for _ in ()).throw(Forbidden('secret-message')))
    assert reports == []
    now[0] = 61
    report = stats.flush()
    assert report['window_seconds'] == 61
    rows = {(r['operation'], r['purpose']): r for r in report['operations']}
    read_row = rows['GET', 'account_token']
    assert read_row['count'] == 2
    assert read_row['success'] == 1 and read_row['errors'] == 1
    assert read_row['downloaded_bytes'] == len(b'secret-token')
    assert read_row['duration_ms'] == 250
    assert rows['EXISTS', 'stop']['404'] == 1
    assert rows['PUT', 'account_token']['uploaded_bytes'] == 20
    assert 'secret' not in str(report)
    assert stats.flush(force=True) is None


def test_stats_are_shared_safely_by_threads_and_bad_sink_does_not_break_requests():
    stats = CallStats('test', emit=lambda _: None)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: stats.call('GET', 'marker', lambda: b'x'), range(100))) == [b'x'] * 100
    report = stats.flush(force=True)
    assert report['operations'][0]['count'] == 100
    stats.emit = lambda _: (_ for _ in ()).throw(OSError('log sink failed'))
    stats.interval = 0
    assert stats.call('GET', 'marker', lambda: b'ok') == b'ok'


def test_cache_preserves_absence_and_empty_values_and_invalidates():
    clock = [0.0]
    cache = ReadCache(clock=lambda: clock[0])
    calls = []
    def load():
        calls.append(True)
        return None
    assert cache.get('stop', 10, load) is None
    assert cache.get('stop', 10, load) is None
    assert len(calls) == 1
    cache.invalidate('stop')
    assert cache.get('stop', 10, lambda: '') == ''
    clock[0] = 10
    assert cache.get('stop', 10, lambda: 'updated') == 'updated'


def test_cache_refresh_failure_is_not_hidden_by_old_snapshot():
    clock = [0.0]
    cache = ReadCache(clock=lambda: clock[0])
    assert cache.get('workers', 60, lambda: ['old']) == ['old']
    clock[0] = 60
    def fail():
        raise TimeoutError('timeout')
    with pytest.raises(TimeoutError):
        cache.get('workers', 60, fail)
    assert cache.get('workers', 60, lambda: ['new']) == ['new']


def test_cache_version_expiry_bounds_and_disabled_cache():
    cache = ReadCache(capacity=2)
    assert cache.get('daily', 60, lambda: 'yesterday', version=1) == 'yesterday'
    assert cache.get('daily', 60, lambda: 'today', version=2) == 'today'
    cache.get('a', 60, lambda: 1)
    cache.get('b', 60, lambda: 2)
    assert len(cache.entries) == 2
    assert 'daily' not in cache.entries
    assert cache.get('b', 0, lambda: 3) == 3


@pytest.mark.parametrize('value', ['-1', 'nan', 'inf'])
def test_invalid_intervals_rejected(monkeypatch, value):
    monkeypatch.setenv('TEST_INTERVAL', value)
    with pytest.raises(ValueError):
        seconds('TEST_INTERVAL', 60)


def test_default_output_is_one_short_summary_and_preserves_full_report(monkeypatch, capsys):
    monkeypatch.delenv('OSS_STATS_FORMAT', raising=False)
    clock = [0.0]
    stats = CallStats('integration_harness', clock=lambda: clock[0])
    stats.call('GET', 'hxacc/account/private-id/account.json', lambda: b'abc')
    stats.call('EXISTS', 'hxacc/account/private-id/stop', lambda: False)
    stats.call('PUT', 'hxacc/account/private-id/id.process', lambda: None, uploaded=1024)
    stats.cache_hit('hxacc/account/private-id/stop')
    clock[0] = 61.841
    report = stats.flush()
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1
    assert len(output) < 250
    assert '61.8秒 请求3 成功2 未找到1 异常0 缓存命中1 下行3B 上行1.0KiB' in output
    assert '学习/' in output
    assert 'private-id' not in output
    assert 'duration_ms' not in output and 'status_classes' not in output
    assert len(report['operations']) == 3


def test_json_output_remains_available(monkeypatch, capsys):
    import json
    monkeypatch.setenv('OSS_STATS_FORMAT', 'json')
    stats = CallStats('test')
    stats.call('GET', 'marker', lambda: b'x')
    report = stats.flush(force=True)
    output = capsys.readouterr().out
    assert output.startswith('OSS_STATS {')
    assert json.loads(output.removeprefix('OSS_STATS ')) == report


def test_cache_only_window_has_zero_requests_in_summary(monkeypatch, capsys):
    monkeypatch.setenv('OSS_STATS_FORMAT', 'summary')
    stats = CallStats('test')
    stats.cache_hit('hxacc/workers')
    stats.flush(force=True)
    output = capsys.readouterr().out
    assert '请求0 成功0 未找到0 异常0 缓存命中1 下行0B 上行0B' in output

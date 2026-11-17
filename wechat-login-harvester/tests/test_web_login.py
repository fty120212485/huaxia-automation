import json
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import httpx
import pytest

from wechat_login_harvester.config import UserCredential
from wechat_login_harvester.web_login import WebLoginError, WebLoginRunner, preprocess_captcha

USER = UserCredential('测试', '110101199001010010')
SVG = '<svg width="150" height="50"><path fill="none" stroke="#444" d="M0 0L100 20"/><path fill="#333" d="M1 1L2 2Z"/></svg>'


def config(tmp_path):
    return SimpleNamespace(web_login_base_url='https://guangdong.hxacc.com',
        learn_base_url='https://learn.hxacc.com', web_login_ca_file='',
        token_output_dir=tmp_path/'tokens', account_dir=tmp_path/'account')


def test_preprocess_removes_line_preserves_glyph_and_scales():
    root = ET.fromstring(preprocess_captcha(SVG))
    assert len(root) == 1
    assert root[0].get('fill') == '#333'
    assert float(root.get('width')) == 600
    assert root.get('viewBox') == '0 0 150.0 50.0'


@pytest.mark.parametrize('svg', ['<!DOCTYPE svg><svg/>', '<html/>', '<svg><image href="https://example.org"/></svg>', '<svg width="9999"/>', '<svg><path style="fill:url(https://example.org)"/></svg>'])
def test_rejects_unsupported_svg(svg):
    with pytest.raises(WebLoginError):
        preprocess_captcha(svg)


def setup_client(cfg, *, mismatch=False, task_error=False, captcha_failures=0, user=USER):
    calls = []
    state = {'device': None, 'attempts': 0}

    def reply(data):
        return httpx.Response(200, json={'status': {'code': 0}, 'data': data})

    def handler(req):
        payload = json.loads(req.content) if req.content else {}
        calls.append((req.url.host, req.url.path, payload, dict(req.headers)))
        if req.url.path == '/api/auth/captchaimg':
            return reply({'svg': SVG, 'uniqueId': 'challenge'})
        if req.url.path == '/api/auth/login':
            state['device'] = payload['deviceId']
            assert 'code' not in payload
            if not payload.get('relogin'):
                state['attempts'] += 1
                if state['attempts'] <= captcha_failures:
                    return httpx.Response(200, json={'status': {'code': -1, 'message': '验证码错误'}})
                return reply({'sameDevice': False})
            return httpx.Response(200, json={'status': {'code': 0}, 'data': {}},
                headers={'set-cookie': 'sid=website-token; Path=/; Secure; HttpOnly'})
        if req.url.path == '/api/users/me':
            assert 'sid=website-token' in req.headers.get('cookie', '')
            return reply({'idcard': 'wrong' if mismatch else user.id_card,
                'appId': 'app', 'deviceId': state['device']})
        if req.url.path == '/api/mytask/getTaskList':
            assert req.url.host == 'learn.hxacc.com'
            assert req.headers['cookie'] == f"sid=website-token;deviceId={state['device']}"
            if task_error:
                return httpx.Response(401, json={'message': '登录失效'})
            return reply({'list': []})
        raise AssertionError(req.url)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_success_relogin_cookie_transfer_and_stable_device(tmp_path):
    cfg = config(tmp_path)
    client, calls = setup_client(cfg)
    runner = WebLoginRunner(cfg, client=client, ocr=lambda svg: 'Ab12')
    path = runner.run(USER)
    first = json.loads(path.read_text())
    assert first['token'] == 'website-token'
    assert path.stat().st_mode & 0o777 == 0o600
    assert [p for _, p, _, _ in calls][-1] == '/api/mytask/getTaskList'
    runner.run(USER)
    assert json.loads(path.read_text())['deviceId'] == first['deviceId']


@pytest.mark.parametrize('document', ['Z00000000', 'A123456(7)', 'A1234567', 'other-document'])
def test_non_mainland_documents_are_submitted_unchanged(tmp_path, document):
    cfg = config(tmp_path)
    user = UserCredential('测试', document)
    client, calls = setup_client(cfg, user=user)
    path = WebLoginRunner(cfg, client=client, ocr=lambda svg: 'Ab12').run(user)
    login_payloads = [body for _, route, body, _ in calls if route == '/api/auth/login']
    assert all(body['idcard'] == document for body in login_payloads)
    assert path.name == f'{document}.account'
    assert json.loads(path.read_text())['idCard'] == document


@pytest.mark.parametrize('document', ['', ' ', '..', '../escape', 'a/b', 'a\\b', 'a\x00b', 'a\nb'])
def test_unsafe_document_filenames_are_rejected(tmp_path, document):
    cfg = config(tmp_path)
    client, calls = setup_client(cfg)
    with pytest.raises(WebLoginError):
        WebLoginRunner(cfg, client=client).run(UserCredential('测试', document))
    assert not calls


@pytest.mark.parametrize('kwargs', [{'mismatch': True}, {'task_error': True}, {'captcha_failures': 3}])
def test_failure_preserves_old_account(tmp_path, kwargs):
    cfg = config(tmp_path)
    path = cfg.account_dir / f'{USER.id_card}.account'
    path.parent.mkdir()
    path.write_text('old-account')
    client, calls = setup_client(cfg, **kwargs)
    with pytest.raises(WebLoginError):
        WebLoginRunner(cfg, client=client, ocr=lambda svg: 'Ab12').run(USER)
    assert path.read_text() == 'old-account'
    assert sum(p == '/api/auth/captchaimg' for _, p, _, _ in calls) <= 3


def test_refreshes_captcha_on_rejection(tmp_path):
    cfg = config(tmp_path)
    client, calls = setup_client(cfg, captcha_failures=1)
    WebLoginRunner(cfg, client=client, ocr=lambda svg: 'Ab12').run(USER)
    assert sum(p == '/api/auth/captchaimg' for _, p, _, _ in calls) == 2


def test_ocr_failure_is_bounded_and_never_submits_login(tmp_path):
    cfg = config(tmp_path)
    client, calls = setup_client(cfg)
    def fail(svg):
        raise WebLoginError('OCR failed')
    with pytest.raises(WebLoginError):
        WebLoginRunner(cfg, client=client, ocr=fail).run(USER)
    assert len(calls) == 3
    assert all(p == '/api/auth/captchaimg' for _, p, _, _ in calls)


def test_rejects_untrusted_domain(tmp_path):
    cfg = config(tmp_path)
    cfg.web_login_base_url = 'https://guangdong.hxacc.com.example.org'
    with pytest.raises(WebLoginError):
        WebLoginRunner(cfg)


def test_relogin_accepts_verified_web_session_with_unchanged_device(tmp_path, monkeypatch):
    from wechat_login_harvester import __main__ as main
    cfg = config(tmp_path)
    cfg.login_backend = 'web'
    cfg.account_dir.mkdir()
    (cfg.account_dir / f'{USER.id_card}.account').write_text(json.dumps({'deviceId': 'stable'}))
    monkeypatch.setattr(main, '_harvest_users', lambda *args: 0)
    assert main._relogin_user(cfg, USER, None)
    monkeypatch.setattr(main, '_harvest_users', lambda *args: 1)
    assert not main._relogin_user(cfg, USER, None)


def test_relogin_exception_does_not_accept_old_account(tmp_path, monkeypatch):
    from wechat_login_harvester import __main__ as main
    cfg = config(tmp_path)
    cfg.login_backend = 'web'
    cfg.account_dir.mkdir()
    (cfg.account_dir / f'{USER.id_card}.account').write_text(json.dumps({'deviceId': 'old'}))
    def fail(*args):
        raise RuntimeError('failed')
    monkeypatch.setattr(main, '_harvest_users', fail)
    assert not main._relogin_user(cfg, USER, None)

import base64
import hashlib
import hmac
from unittest.mock import Mock

import pytest
import requests

from agent.notifications.feishu import (
    FeishuNotificationError,
    gen_sign,
    send_feishu_text,
)


def test_gen_sign_uses_feishu_signature_rule():
    timestamp = '1710000000'
    secret = 'test-secret'
    key = f'{timestamp}\n{secret}'.encode('utf-8')
    expected = base64.b64encode(
        hmac.new(key, digestmod=hashlib.sha256).digest()
    ).decode('utf-8')

    assert gen_sign(timestamp, secret) == expected


def test_missing_webhook_skips_silently(monkeypatch, caplog):
    monkeypatch.delenv('FEISHU_WEBHOOK', raising=False)

    assert send_feishu_text('任务完成', 'status=success') is False
    assert 'FEISHU_WEBHOOK' not in caplog.text


def test_sends_text_with_default_keyword(monkeypatch):
    monkeypatch.setenv('FEISHU_WEBHOOK', 'https://example.test/webhook')
    monkeypatch.delenv('FEISHU_WEBHOOK_SECRET', raising=False)
    monkeypatch.delenv('FEISHU_KEYWORD', raising=False)
    response = Mock()
    response.json.return_value = {'code': 0, 'msg': 'success'}
    post = Mock(return_value=response)
    monkeypatch.setattr('agent.notifications.feishu.requests.post', post)

    assert send_feishu_text('任务完成', 'run_id=abc\nstatus=success') is True
    post.assert_called_once_with(
        'https://example.test/webhook',
        json={
            'msg_type': 'text',
            'content': {
                'text': '[ACoder] 任务完成\nrun_id=abc\nstatus=success'
            },
        },
        timeout=5,
    )


def test_signed_request_uses_custom_keyword(monkeypatch):
    monkeypatch.setenv('FEISHU_WEBHOOK', 'https://example.test/webhook')
    monkeypatch.setenv('FEISHU_WEBHOOK_SECRET', 'secret')
    monkeypatch.setenv('FEISHU_KEYWORD', '[Forge]')
    monkeypatch.setattr(
        'agent.notifications.feishu.time.time', lambda: 1710000000
    )
    response = Mock()
    response.json.return_value = {'code': 0}
    post = Mock(return_value=response)
    monkeypatch.setattr('agent.notifications.feishu.requests.post', post)

    send_feishu_text('任务失败', 'status=failed')

    payload = post.call_args.kwargs['json']
    assert payload['timestamp'] == '1710000000'
    assert payload['sign'] == gen_sign('1710000000', 'secret')
    assert payload['content']['text'].startswith('[Forge] 任务失败\n')


def test_raises_clear_error_for_feishu_error_code(monkeypatch):
    monkeypatch.setenv('FEISHU_WEBHOOK', 'https://example.test/webhook')
    response = Mock()
    response.json.return_value = {'code': 19024, 'msg': 'Key Words Not Found'}
    monkeypatch.setattr(
        'agent.notifications.feishu.requests.post', Mock(return_value=response)
    )

    with pytest.raises(FeishuNotificationError, match='code=19024'):
        send_feishu_text('任务完成', 'status=success')


def test_raises_clear_error_for_http_failure(monkeypatch):
    monkeypatch.setenv('FEISHU_WEBHOOK', 'https://example.test/webhook')
    monkeypatch.setattr(
        'agent.notifications.feishu.requests.post',
        Mock(side_effect=requests.Timeout('timed out')),
    )

    with pytest.raises(FeishuNotificationError, match='HTTP 请求失败'):
        send_feishu_text('任务完成', 'status=success')

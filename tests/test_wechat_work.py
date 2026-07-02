from unittest.mock import Mock

import pytest
import requests

from agent.notifications.wechat_work import (
    WechatWorkNotificationError,
    send_wechat_work_text,
)


def test_missing_webhook_skips_silently(monkeypatch, caplog):
    monkeypatch.delenv('WECHAT_WORK_WEBHOOK', raising=False)
    post = Mock()
    monkeypatch.setattr('agent.notifications.wechat_work.requests.post', post)

    assert send_wechat_work_text('任务完成', 'status=success') is False
    post.assert_not_called()
    assert 'WECHAT_WORK_WEBHOOK' not in caplog.text


def test_sends_text_with_default_keyword(monkeypatch):
    monkeypatch.setenv(
        'WECHAT_WORK_WEBHOOK', 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send'
    )
    monkeypatch.delenv('WECHAT_WORK_KEYWORD', raising=False)
    response = Mock()
    response.json.return_value = {'errcode': 0, 'errmsg': 'ok'}
    post = Mock(return_value=response)
    monkeypatch.setattr('agent.notifications.wechat_work.requests.post', post)

    assert send_wechat_work_text(
        '任务完成', 'run_id=abc\nstatus=success'
    ) is True
    post.assert_called_once_with(
        'https://qyapi.weixin.qq.com/cgi-bin/webhook/send',
        json={
            'msgtype': 'text',
            'text': {
                'content': '[ACoder] 任务完成\nrun_id=abc\nstatus=success'
            },
        },
        timeout=5,
    )


def test_uses_custom_keyword(monkeypatch):
    monkeypatch.setenv('WECHAT_WORK_WEBHOOK', 'https://example.test/webhook')
    monkeypatch.setenv('WECHAT_WORK_KEYWORD', '[Forge]')
    response = Mock()
    response.json.return_value = {'errcode': 0}
    post = Mock(return_value=response)
    monkeypatch.setattr('agent.notifications.wechat_work.requests.post', post)

    send_wechat_work_text('任务失败', 'status=failed')

    payload = post.call_args.kwargs['json']
    assert payload['text']['content'].startswith('[Forge] 任务失败\n')


def test_raises_clear_error_for_wechat_error_code(monkeypatch):
    monkeypatch.setenv('WECHAT_WORK_WEBHOOK', 'https://example.test/webhook')
    response = Mock()
    response.json.return_value = {'errcode': 93000, 'errmsg': 'invalid webhook'}
    monkeypatch.setattr(
        'agent.notifications.wechat_work.requests.post',
        Mock(return_value=response),
    )

    with pytest.raises(WechatWorkNotificationError, match='errcode=93000'):
        send_wechat_work_text('任务完成', 'status=success')


def test_raises_clear_error_for_http_failure(monkeypatch):
    monkeypatch.setenv('WECHAT_WORK_WEBHOOK', 'https://example.test/webhook')
    monkeypatch.setattr(
        'agent.notifications.wechat_work.requests.post',
        Mock(side_effect=requests.Timeout('timed out')),
    )

    with pytest.raises(WechatWorkNotificationError, match='HTTP 请求失败'):
        send_wechat_work_text('任务完成', 'status=success')

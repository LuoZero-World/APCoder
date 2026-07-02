'''企业微信群机器人文本通知。'''

import os

import requests


class WechatWorkNotificationError(RuntimeError):
    '''企业微信通知发送失败。'''


def send_wechat_work_text(title: str, body: str) -> bool:
    '''发送企业微信文本；未配置 Webhook 时静默跳过。'''
    webhook = os.getenv('WECHAT_WORK_WEBHOOK', '').strip()
    if not webhook:
        return False

    keyword = os.getenv('WECHAT_WORK_KEYWORD', '').strip() or '[ACoder]'
    text = f'{keyword} {title}' + (f'\n{body}' if body else '')
    payload = {
        'msgtype': 'text',
        'text': {'content': text},
    }

    try:
        response = requests.post(webhook, json=payload, timeout=5)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise WechatWorkNotificationError(
            f'企业微信 Webhook HTTP 请求失败：{exc}'
        ) from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise WechatWorkNotificationError(
            '企业微信 Webhook 返回了非 JSON 数据'
        ) from exc
    if not isinstance(data, dict):
        raise WechatWorkNotificationError(f'企业微信返回格式异常：{data!r}')

    errcode = data.get('errcode')
    if errcode not in (0, '0'):
        errmsg = data.get('errmsg', '未知错误')
        raise WechatWorkNotificationError(
            f'企业微信拒绝发送消息：errcode={errcode}, errmsg={errmsg}'
        )
    return True

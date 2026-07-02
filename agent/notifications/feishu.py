'''飞书自定义机器人文本通知。'''

import base64
import hashlib
import hmac
import os
import time

import requests


class FeishuNotificationError(RuntimeError):
    '''飞书通知发送失败。'''


def gen_sign(timestamp: str, secret: str) -> str:
    '''按飞书规则生成机器人签名。'''
    string_to_sign = f'{timestamp}\n{secret}'.encode('utf-8')
    digest = hmac.new(string_to_sign, digestmod=hashlib.sha256)
    return base64.b64encode(digest.digest()).decode('utf-8')


def send_feishu_text(title: str, body: str) -> bool:
    '''发送飞书文本；未配置 Webhook 时静默跳过。'''
    webhook = os.getenv('FEISHU_WEBHOOK', '').strip()
    if not webhook:
        return False

    keyword = os.getenv('FEISHU_KEYWORD', '').strip() or '[ACoder]'
    text = f'{keyword} {title}' + (f'\n{body}' if body else '')
    payload = {'msg_type': 'text', 'content': {'text': text}}

    # 配置签名密钥时，在请求体中加入时间戳和签名。
    secret = os.getenv('FEISHU_WEBHOOK_SECRET')
    if secret:
        timestamp = str(int(time.time()))
        payload.update(timestamp=timestamp, sign=gen_sign(timestamp, secret))

    try:
        response = requests.post(webhook, json=payload, timeout=5)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise FeishuNotificationError(
            f'飞书 Webhook HTTP 请求失败：{exc}'
        ) from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise FeishuNotificationError('飞书 Webhook 返回了非 JSON 数据') from exc
    if not isinstance(data, dict):
        raise FeishuNotificationError(f'飞书返回格式异常：{data!r}')

    code = data.get('code', data.get('StatusCode', 0))
    if code not in (0, '0', None):
        message = data.get('msg', data.get('StatusMessage', '未知错误'))
        raise FeishuNotificationError(
            f'飞书拒绝发送消息：code={code}, msg={message}'
        )
    return True

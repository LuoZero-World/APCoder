import os

from config.schema import (
    FeishuNotificationConfig,
    NotificationsConfig,
    WechatWorkNotificationConfig,
    load_config,
)
from entry.cli import _apply_notification_config


def test_load_notification_config_from_yaml(tmp_path):
    config_file = tmp_path / 'config.yaml'
    config_file.write_text(
        '''
notifications:
  feishu:
    webhook: https://feishu.example/webhook
    secret: feishu-secret
    keyword: '[Feishu]'
  wechat_work:
    webhook: https://wechat.example/webhook
    keyword: '[WeChat]'
''',
        encoding='utf-8',
    )

    config = load_config(config_file)

    assert config.notifications.feishu.webhook == 'https://feishu.example/webhook'
    assert config.notifications.feishu.secret == 'feishu-secret'
    assert config.notifications.feishu.keyword == '[Feishu]'
    assert config.notifications.wechat_work.webhook == 'https://wechat.example/webhook'
    assert config.notifications.wechat_work.keyword == '[WeChat]'


def test_apply_notification_config_keeps_environment_priority(monkeypatch):
    monkeypatch.setenv('FEISHU_KEYWORD', '[Environment]')
    monkeypatch.delenv('FEISHU_WEBHOOK', raising=False)
    monkeypatch.delenv('WECHAT_WORK_WEBHOOK', raising=False)
    config = NotificationsConfig(
        feishu=FeishuNotificationConfig(
            webhook='https://feishu.example/webhook',
            keyword='[Yaml]',
        ),
        wechat_work=WechatWorkNotificationConfig(
            webhook='https://wechat.example/webhook',
        ),
    )

    _apply_notification_config(config)

    assert os.environ['FEISHU_KEYWORD'] == '[Environment]'
    assert os.environ['FEISHU_WEBHOOK'] == 'https://feishu.example/webhook'
    assert os.environ['WECHAT_WORK_WEBHOOK'] == 'https://wechat.example/webhook'

'''手动测试飞书自定义机器人 Webhook。'''

import os
from datetime import datetime

from agent.notifications.feishu import FeishuNotificationError, send_feishu_text


# 必填：飞书自定义机器人的 Webhook 地址。
WEBHOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/395c0484-6254-49b6-b74b-cc8f18c67685'

# 可选：机器人安全设置中的关键词，默认使用 [ACoder]。
KEYWORD = '[ACoder]'

# 可选：机器人启用签名校验时填写，否则保持为空。
SECRET = ''


def main() -> int:
    webhook = WEBHOOK.strip()
    if not webhook:
        print('请先在脚本顶部填写 WEBHOOK。')
        return 1

    keyword = KEYWORD.strip() or '[ACoder]'
    os.environ['FEISHU_WEBHOOK'] = webhook
    os.environ['FEISHU_KEYWORD'] = keyword

    if SECRET.strip():
        os.environ['FEISHU_WEBHOOK_SECRET'] = SECRET.strip()
    else:
        os.environ.pop('FEISHU_WEBHOOK_SECRET', None)

    try:
        send_feishu_text(
            title='飞书通知测试',
            body=f'测试时间={datetime.now():%Y-%m-%d %H:%M:%S}\nstatus=success',
        )
    except FeishuNotificationError as exc:
        print(f'推送失败：{exc}')
        return 1

    print(f'推送成功，关键词：{keyword}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

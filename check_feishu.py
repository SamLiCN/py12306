# -*- coding: utf-8 -*-
"""
飞书通知自检脚本

用法（在 NAS 上，先 source .venv/bin/activate）：
    python check_feishu.py                      # 用 env.py 里的配置发一条测试消息
    python check_feishu.py --no-sign            # 不加签名发送（用来区分「签名错」和别的问题）
    python check_feishu.py --dry-run            # 只打印将要发送的请求体，不真的发
    python check_feishu.py --webhook 'https://open.feishu.cn/open-apis/bot/v2/hook/xxx' --secret 'xxx'
    python check_feishu.py --text '自定义测试内容' --no-at

这个脚本走的是正式代码路径（py12306/feishu/bot.py 的签名与发送），
所以它通了就代表下单成功时的飞书通知也通了。
"""
import argparse
import json
import sys
import time

from py12306.config import Config
from py12306.helpers.func import Const

TROUBLESHOOTING = """
排查清单（按顺序看）：
  1. 返回 19021：飞书把「签名算错」和「timestamp 与飞书服务器相差超过 1 小时」合并成同一个错误码，
     无法区分。先核对 FEISHU_SECRET 是否与后台「签名校验」里的一模一样（注意首尾空格），
     再对比上面打印的本地时间与你手机/电脑上的真实时间 —— NAS 时钟漂移是常见原因。
  2. 返回 19024 `Key Words Not Found`：机器人的安全设置里勾了「自定义关键词」，
     消息必须包含该关键词。建议去后台把关键词校验关掉，只留「签名校验」。
  3. 返回 19022：勾了「IP 白名单」但没放行 NAS 的出口 IP，关掉或加白名单。
  4. 返回 19001：Webhook 地址（token）填错了，重新从群机器人设置里复制。
  5. 返回 9499 / 11232 / 99991400：参数错或触发限流（官方限 5 次/秒、100 次/分钟），等一分钟再试。
  6. 返回 code=0 但【手机没弹通知】：这几乎总是飞书客户端设置问题，不是代码问题 ——
     官方明确「飞书默认为所有会话开启消息提醒」，且官方只在「折叠的会话」场景承诺
     「@ 你仍会收到通知」，并【没有】承诺 @ 能穿透「消息免打扰」。逐条检查：
       a) 手机系统设置里是否允许飞书发通知；
       b) 飞书「个人头像 → 设置 → 通知 → 新消息通知」是否被改成了「部分新消息」，
          如果是，确认勾上了 @我的消息 与 @所有人的消息；
       c) 是否开了「关闭手机通知」（桌面端/iPad 在线时手机端不再重复提醒）；
       d) 是否开了「请勿打扰」状态，或该群被设成了「消息免打扰」；
       e) 直接用官方内置工具：飞书「设置 → 通知 → 消息通知故障诊断」。
  7. 如果群里看到的「所有人」是灰色/没有高亮的纯文本，说明 @所有人 没生效：
     群设置里开了「仅群主和群管理员可@所有人」，而自定义机器人不是群主/管理员
     （官方明说这种情况卡片会直接发送失败）。你是群主的话，去群设置关掉这个开关；
     或者改用 @指定人（FEISHU_AT_USER_IDS，只支持 open_id）。
"""


def build_parser():
    parser = argparse.ArgumentParser(description='py12306 飞书通知自检')
    parser.add_argument('--webhook', help='覆盖 env.py 里的 FEISHU_WEBHOOK')
    parser.add_argument('--secret', help='覆盖 env.py 里的 FEISHU_SECRET')
    parser.add_argument('--text', default='py12306 飞书通知自检消息', help='测试消息内容')
    parser.add_argument('--at-user-ids', help='@指定人，逗号分隔的 open_id 列表')
    parser.add_argument('--no-at', action='store_true', help='本次不 @所有人')
    parser.add_argument('--no-sign', action='store_true', help='不加签名发送（用于区分签名错与其它问题）')
    parser.add_argument('--dry-run', action='store_true', help='只打印将要发送的请求体，不真的发送')
    return parser


def mask_webhook(webhook):
    webhook = (webhook or '').strip()
    if not webhook:
        return '未配置'
    head, _, token = webhook.rpartition('/')
    if not head:
        return '****'
    return '{}/****{}'.format(head, token[-4:] if len(token) > 4 else '')


def main():
    Const.IS_TEST = True  # 让日志直接打到屏幕上，不写进 runtime/12306.log
    args = build_parser().parse_args()

    config = Config()  # 与主程序同一套加载逻辑（读的是项目根目录的 env.py）
    if args.webhook:
        config.FEISHU_WEBHOOK = args.webhook
    if args.secret is not None:
        config.FEISHU_SECRET = args.secret
    if args.at_user_ids is not None:
        config.FEISHU_AT_USER_IDS = args.at_user_ids
    if args.no_at:
        config.FEISHU_AT_ALL = 0

    from py12306.feishu.bot import FeishuBot  # 延迟导入，保证上面的覆盖先生效

    secret = (config.FEISHU_SECRET or '').strip()
    print('**** 飞书通知自检 ****')
    print('本地时间:  {}'.format(time.strftime('%Y-%m-%d %H:%M:%S')))
    print('Webhook:   {}'.format(mask_webhook(config.FEISHU_WEBHOOK)))
    print('签名密钥:  {}'.format('已配置（长度 {}）'.format(len(secret)) if secret else '未配置（则不会签名）'))
    print('@所有人:   {}'.format('是' if config.FEISHU_AT_ALL else '否'))
    print('@指定人:   {}'.format(config.FEISHU_AT_USER_IDS or '无'))
    print('本次签名:  {}'.format('否（--no-sign）' if args.no_sign else ('是' if secret else '否（未配置密钥）')))
    print('-' * 64)

    if args.dry_run:
        payload = FeishuBot.build_payload(args.text, sign=not args.no_sign)
        print('dry-run：下面是将要发送的请求体（未真的发送）')
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    ok = FeishuBot().send(args.text, sign=not args.no_sign)
    print('-' * 64)
    if ok:
        print('# 发送成功（飞书返回 code == 0） #')
        print('现在看手机：收到通知即代表飞书渠道已打通。')
        print('若群里能看到消息、手机却没弹通知，按第 6 条排查（这是客户端设置问题，不是代码问题）。')
        print(TROUBLESHOOTING)
        return 0
    print('!! 发送失败 !!')
    print(TROUBLESHOOTING)
    return 1


if __name__ == '__main__':
    sys.exit(main())

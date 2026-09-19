# -*- coding: utf-8 -*-
"""
飞书群自定义机器人（Webhook 单向推送）

官方文档：https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot

改这个文件之前先读这几条（都是查过官方文档/实测的结论，别再靠猜）：

1. 只有 POST https://open.feishu.cn/open-apis/bot/v2/hook/<token> 这一条路径，
   /bot/v1/hook/... 已下线（实测 404）。
2. 签名算法不是「secret 当 HMAC key」——这是网上博客最常见的错误写法。
   官方口径：把 "{timestamp}\\n{secret}" 整体当作 key，对待签内容「空字符串」计算
   HMAC-SHA256，再 base64；timestamp 为秒级；timestamp / sign 都放请求体顶层。
3. 飞书业务错误也返回 HTTP 200，所以成功判据只能是响应体里的 code == 0；
   旧文档里的 StatusCode / StatusMessage 是冗余字段，实际响应已不再返回。
4. 自定义机器人无数据访问权限：@ 指定人只支持 open_id（不支持 user_id / email），
   且 open_id 是「应用维度」的；@ 无效 ID 官方明确「只取名字展示，不产生实际 @ 效果」，且不报错。
5. text 消息不渲染 Markdown（post 富文本也没有 Markdown，只有 interactive 卡片有）。
6. 手机端能否弹通知主要取决于客户端设置（新群默认开启提醒），@ 只是辅助手段；
   官方只在「折叠的会话」场景承诺「@ 你仍会通知」，并未承诺穿透「消息免打扰」。
7. 本模块【故意不 import py12306.helpers.request】——那个模块顶层 `from requests_html import ...`，
   会把 HTML 解析 + pyppeteer 整套栈拖进 import 链，导致 `check_feishu.py` 这种只要发一个
   JSON POST 的自检脚本，在没有 requests_html 的环境里（例如 Mac 上的系统 python3，
   见 AGENTS.md「部署环境」）直接 `ModuleNotFoundError: No module named 'requests_html'`，
   飞书代码一行都跑不到。飞书 webhook 不需要任何 HTML 能力，用 requests 就够，
   改动前想清楚：这里加回 Request() 会把自检脚本重新绑死在 NAS 上。
"""
import base64
import hashlib
import hmac
import json
import time

import requests

from py12306.config import Config
from py12306.log.common_log import CommonLog


class FeishuBot:
    """
    飞书群自定义机器人
    """

    API_URL_OF_FEISHU_BOT = 'https://open.feishu.cn/open-apis/bot/v2/hook/'

    # 飞书错误码 -> 人话
    # 注意：19021 / 19022 / 19024 / 19001 只出现在「自定义机器人」文档里，通用错误码表里查不到
    ERROR_MESSAGES = {
        9499: '请求参数非法（Bad Request）',
        11232: '触发飞书频率限制（官方限制 5 次/秒、100 次/分钟）',
        19001: 'Webhook 地址里的 token 无效，请核对 FEISHU_WEBHOOK',
        19002: 'msg_type 缺失或非法（本渠道固定为 text，出现即代码问题）',
        19021: '签名校验失败，或 timestamp 与飞书服务器时间相差超过 1 小时'
               '（官方把这两种情况合并成同一个码，无法区分：请先核对 FEISHU_SECRET 是否填错，'
               '再检查本机/NAS 时钟是否同步）',
        19022: '本机出口 IP 不在机器人安全设置的 IP 白名单内',
        19024: '消息未命中「自定义关键词」，请在飞书机器人安全设置里关掉关键词校验，或把关键词补进消息',
        19036: '消息体超过大小限制（官方文档写 20KB、错误码文案写 30KB，按小的来）',
        99991400: '触发飞书频率限制',
    }

    def __init__(self):
        # 用 requests.Session 而不是项目的 Request()，原因见文件头第 7 条
        self.session = requests.Session()

    @classmethod
    def send_text(cls, content):
        self = cls()
        return self.send(content)

    @staticmethod
    def gen_sign(timestamp, secret):
        """
        飞书官方签名算法
        原文：将 timestamp + "\\n" + 密钥 当做签名字符串，使用 HmacSHA256 算法计算空字符串的签名结果，再进行 Base64 编码
        """
        string_to_sign = '{}\n{}'.format(timestamp, secret)
        hmac_code = hmac.new(string_to_sign.encode('utf-8'), digestmod=hashlib.sha256).digest()
        return base64.b64encode(hmac_code).decode('utf-8')

    @staticmethod
    def build_at_text():
        """
        拼 @ 片段
        - @所有人 不需要任何 ID，最省事；但若群开启了「仅群主和群管理员可@所有人」，
          自定义机器人（不是群主/管理员）就无法 @所有人（官方明说卡片会直接发送失败，text 场景未明说）
        - @指定人 只支持 open_id，且 open_id 是「应用维度」的，别人给的不一定能用
        """
        ats = []
        if Config().FEISHU_AT_ALL:
            ats.append('<at user_id="all">所有人</at>')
        user_ids = Config().FEISHU_AT_USER_IDS or ''
        if isinstance(user_ids, str):
            user_ids = user_ids.split(',')
        for user_id in user_ids:
            user_id = (user_id or '').strip()
            if user_id:
                ats.append('<at user_id="{}"></at>'.format(user_id))
        return ' '.join(ats)

    @classmethod
    def format_content(cls, content):
        at_text = cls.build_at_text()
        if not at_text:
            return content
        return '{}\n{}'.format(at_text, content)

    @classmethod
    def build_payload(cls, content, sign=True):
        payload = {
            'msg_type': 'text',
            'content': {
                'text': cls.format_content(content),
            },
        }
        secret = (Config().FEISHU_SECRET or '').strip()
        if sign and secret:  # 有密钥就自动签名，不设单独开关
            timestamp = str(int(time.time()))
            payload['timestamp'] = timestamp
            payload['sign'] = cls.gen_sign(timestamp, secret)
        return payload

    @classmethod
    def describe_error(cls, result, status_code=None):
        """
        把飞书返回翻译成人话
        19021 无法区分「签名算错」和「时钟超窗」，所以固定带上本地时间，便于判断时钟是否漂移
        """
        local_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
        code = result.get('code')
        message = result.get('msg') or result.get('message') or ''
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = None
        if not result:
            return '飞书接口无有效返回（HTTP {}，本地时间 {}），请检查本机出网与 FEISHU_WEBHOOK 地址'.format(
                status_code, local_time)
        detail = cls.ERROR_MESSAGES.get(code, '未知错误码，请对照飞书开放平台文档')
        return 'code={} msg={}（HTTP {}，本地时间 {}）；{}'.format(code, message, status_code, local_time, detail)

    def send(self, content, sign=True):
        webhook = (Config().FEISHU_WEBHOOK or '').strip()
        if not webhook:
            CommonLog.add_quick_log(CommonLog.MESSAGE_SEND_FEISHU_FAIL.format('未配置 FEISHU_WEBHOOK')).flush()
            return False
        payload = self.build_payload(content, sign=sign)
        try:
            response = self.session.request(url=webhook, method='POST',
                                            headers={'Content-Type': 'application/json'},
                                            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                                            timeout=Config().TIME_OUT_OF_REQUEST)
            status_code = response.status_code
            try:
                # 注意：项目的 Request() 给 response.json() 加过 default= 参数，
                # 裸 requests 没有这个参数，这里必须自己兜住非 JSON 响应（例如网关 HTML 错误页）
                result = response.json()
            except ValueError:
                result = {}
            if not isinstance(result, dict):
                result = {}
        except Exception as e:  # 通知失败绝不允许影响下单主流程
            CommonLog.add_quick_log(CommonLog.MESSAGE_SEND_FEISHU_FAIL.format(
                '请求异常 {}（本地时间 {}）'.format(e, time.strftime('%Y-%m-%d %H:%M:%S')))).flush()
            return False
        # 飞书业务错误也返回 HTTP 200，只能看响应体里的 code
        if status_code == 200 and str(result.get('code')) == '0':
            CommonLog.add_quick_log(CommonLog.MESSAGE_SEND_FEISHU_SUCCESS).flush()
            return True
        CommonLog.add_quick_log(
            CommonLog.MESSAGE_SEND_FEISHU_FAIL.format(self.describe_error(result, status_code))).flush()
        return False


if __name__ == '__main__':
    # 手动调试：python -m py12306.feishu.bot
    FeishuBot.send_text('测试发送信息')

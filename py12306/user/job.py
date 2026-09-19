import base64
import pickle
import re
from os import path

from py12306.cluster.cluster import Cluster
from py12306.helpers.api import *
from py12306.app import *
from py12306.helpers.auth_code import AuthCode
from py12306.helpers.event import Event
from py12306.helpers.func import *
from py12306.helpers.request import Request
from py12306.helpers.type import UserType
from py12306.helpers.qrcode import print_qrcode
from py12306.log.order_log import OrderLog
from py12306.log.user_log import UserLog
from py12306.log.common_log import CommonLog
from py12306.order.order import Browser


def extract_js_object(text, var_name):
    """
    从 HTML 里提取 `var <var_name> = {...}` 的 JS 对象字面量（按大括号配对，能正确处理字符串里的括号）。
    原实现用贪婪正则 '({.+.})'：对象后面只要还有内容就会多吃，遇到换行还会截断。
    """
    match = re.search(r'var\s+%s\s*=\s*' % var_name, text)
    if not match:
        return None
    start = text.find('{', match.end())
    if start < 0:
        return None
    depth = 0
    in_str = None
    i = start
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == '\\':  # 跳过转义字符
                i += 2
                continue
            if ch == in_str:
                in_str = None
        else:
            if ch in ('"', "'"):
                in_str = ch
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        i += 1
    return None


def js_literal_to_json(text):
    """
    把 JS 对象字面量转成合法 JSON。
    原实现直接 .replace("'", '"')，于是遇到 JS 的 \\xNN 转义（例如 \\xA5 就是 ¥）时，
    json.loads 会报 Invalid \\escape —— 下单页 ticketInfoForPassengerForm 里正好有 \\xA5（票价 ¥），
    所以解析必然失败、整条下单链路在这里被静默掐断。
    这里做两件事：1) \\xNN -> \\u00NN；2) 只把「字符串定界符」的单引号替换成双引号。
    """
    if not text:
        return text
    text = re.sub(r'\\x([0-9a-fA-F]{2})', lambda m: '\\u00' + m.group(1).lower(), text)

    def _quote(match):
        inner = match.group(1).replace('\\"', '"').replace("\\'", "'")
        return '"%s"' % inner.replace('"', '\\"')

    return re.sub(r"'((?:\\.|[^'\\])*)'", _quote, text, flags=re.S)


class UserJob:
    # heartbeat = 60 * 2  # 心跳保持时长
    is_alive = True
    check_interval = 5
    key = None
    user_name = ''
    password = ''
    type = 'qr'
    user = None
    info = {}  # 用户信息
    last_heartbeat = None
    is_ready = False
    user_loaded = False  # 用户是否已加载成功
    passengers = []
    retry_time = 3
    retry_count = 0
    login_num = 0  # 尝试登录次数
    sleep_interval = {'min': 0.1, 'max': 5}

    # Init page
    global_repeat_submit_token = None
    ticket_info_for_passenger_form = None
    order_request_dto = None

    cluster = None
    lock_init_user_time = 3 * 60
    cookie = False
    _init_dc_dumped = False  # 诊断用：initDc 页面只 dump 一次，避免刷屏
    _raw_dumped = set()  # 诊断用：记录已 dump 过的接口名，避免刷屏
    passengers_fail_until = 0  # 乘客列表连续失败后的冷却截止时间戳，避免无限刷 12306
    passengers_fail_cooldown = 60  # 冷却秒数（原代码是无限递归重试，会把接口刷到限流）
    # tk 续期（会话保活）：用仍有效的 uamtk 换新 tk，避免 tk 到期后被强制重新扫码
    last_tk_renew = 0
    tk_renew_interval = 20 * 60
    # 心跳接口被 12306 限流后的退避（此时不重新登录，避免白扫一次码）
    heartbeat_rate_limit_until = 0
    heartbeat_rate_limit_cooldown = 120

    def __init__(self, info):
        self.cluster = Cluster()
        self.init_data(info)

    def init_data(self, info):
        self.session = Request()
        self.session.add_response_hook(self.response_login_check)
        self.key = str(info.get('key'))
        self.user_name = info.get('user_name')
        self.password = info.get('password')
        self.type = info.get('type')

    def update_user(self):
        from py12306.user.user import User
        self.user = User()
        self.load_user()

    def run(self):
        # load user
        self.update_user()
        self.start()

    def start(self):
        """
        检测心跳
        :return:
        """
        while True and self.is_alive:
            app_available_check()
            if Config().is_slave():
                self.load_user_from_remote()
            else:
                if Config().is_master() and not self.cookie: self.load_user_from_remote()  # 主节点加载一次 Cookie
                self.check_heartbeat()
            if Const.IS_TEST: return
            stay_second(self.check_interval)

    def check_heartbeat(self):
        # 心跳检测
        if self.get_last_heartbeat() and (time_int() - self.get_last_heartbeat()) < Config().USER_HEARTBEAT_INTERVAL:
            return True
        # 心跳接口被限流后的退避期内：本次跳过，不要再撞接口
        if time_int() < self.heartbeat_rate_limit_until:
            return True
        # 只有主节点才能走到这
        # 从未登录过（无 cookie 文件）：走 load_user / handle_login 恢复或登录
        if self.is_first_time():
            if not self.load_user() and not self.handle_login():
                return
            self.user_did_load()
            message = UserLog.MESSAGE_USER_HEARTBEAT_NORMAL.format(self.get_name(), Config().USER_HEARTBEAT_INTERVAL)
            UserLog.add_quick_log(message).flush()
            return

        is_login = self.check_user_is_login()
        if is_login is None:  # 12306 限流/网络软错误：无法确认登录态，退避而非重新登录
            self.heartbeat_rate_limit_until = time_int() + self.heartbeat_rate_limit_cooldown
            UserLog.add_quick_log(
                '心跳检测被 12306 限流（302/error.html），{} 秒内暂停探测，不会要求重新扫码'.format(
                    self.heartbeat_rate_limit_cooldown)).flush()
            return
        if not is_login:  # 明确未登录 → 才重新登录
            self.handle_login(expire=True)
            return

        passengers_ok = self.can_access_passengers()
        if passengers_ok is None:  # 乘客接口被限流：退避而非重新登录
            self.heartbeat_rate_limit_until = time_int() + self.heartbeat_rate_limit_cooldown
            UserLog.add_quick_log(
                '乘客接口探测被 12306 限流，{} 秒内暂停探测，不会要求重新扫码'.format(
                    self.heartbeat_rate_limit_cooldown)).flush()
            return
        if not passengers_ok:  # 明确未登录 → 重新登录
            self.handle_login(expire=True)
            return

        # 会话正常：顺带续期 tk，防止 tk 到期后被强制重新扫码
        self.renew_session()
        self.user_did_load()
        message = UserLog.MESSAGE_USER_HEARTBEAT_NORMAL.format(self.get_name(), Config().USER_HEARTBEAT_INTERVAL)
        UserLog.add_quick_log(message).flush()

    def get_last_heartbeat(self):
        if Config().is_cluster_enabled():
            return int(self.cluster.session.get(Cluster.KEY_USER_LAST_HEARTBEAT, 0))

        return self.last_heartbeat

    def set_last_heartbeat(self, time=None):
        time = time if time != None else time_int()
        if Config().is_cluster_enabled():
            self.cluster.session.set(Cluster.KEY_USER_LAST_HEARTBEAT, time)
        self.last_heartbeat = time

    # def init_cookies
    def is_first_time(self):
        if Config().is_cluster_enabled():
            return not self.cluster.get_user_cookie(self.key)
        return not path.exists(self.get_cookie_path())

    def handle_login(self, expire=False):
        if expire: UserLog.print_user_expired()
        self.is_ready = False
        UserLog.print_start_login(user=self)
        if self.type == 'qr':
            return self.qr_login()
        else:
            return self.login2()

    def login(self):
        """
        获取验证码结果
        :return 权限校验码
        """
        data = {
            'username': self.user_name,
            'password': self.password,
            'appid': 'otn'
        }
        answer = AuthCode.get_auth_code(self.session)
        data['answer'] = answer
        self.request_device_id()
        response = self.session.post(API_BASE_LOGIN.get('url'), data)
        result = response.json()
        if result.get('result_code') == 0:  # 登录成功
            """
            login 获得 cookie uamtk
            auth/uamtk      不请求，会返回 uamtk票据内容为空
            /otn/uamauthclient 能拿到用户名
            """
            new_tk = self.auth_uamtk()
            user_name = self.auth_uamauthclient(new_tk)
            self.update_user_info({'user_name': user_name})
            self.login_did_success()
            return True
        elif result.get('result_code') == 2:  # 账号之内错误
            # 登录失败，用户名或密码为空
            # 密码输入错误
            UserLog.add_quick_log(UserLog.MESSAGE_LOGIN_FAIL.format(result.get('result_message'))).flush()
        else:
            UserLog.add_quick_log(
                UserLog.MESSAGE_LOGIN_FAIL.format(result.get('result_message', result.get('message',
                                                                                          CommonLog.MESSAGE_RESPONSE_EMPTY_ERROR)))).flush()

        return False

    def qr_login(self):
        self.request_device_id()
        image_uuid, png_path = self.download_code()
        last_time = time_int()
        while True:
            data = {
                'RAIL_DEVICEID': self.session.cookies.get('RAIL_DEVICEID'),
                'RAIL_EXPIRATION': self.session.cookies.get('RAIL_EXPIRATION'),
                'uuid': image_uuid,
                'appid': 'otn'
            }
            response = self.session.post(API_AUTH_QRCODE_CHECK.get('url'), data)
            result = response.json()
            try:
                result_code = int(result.get('result_code'))
            except Exception:
                if time_int() - last_time > 300:
                    last_time = time_int()
                    image_uuid, png_path = self.download_code()
                continue
            if result_code == 0:
                time.sleep(get_interval_num(self.sleep_interval))
            elif result_code == 1:
                UserLog.add_quick_log('请确认登录').flush()
                time.sleep(get_interval_num(self.sleep_interval))
            elif result_code == 2:
                break
            elif result_code == 3:
                try:
                    os.remove(png_path)
                except Exception as e:
                    UserLog.add_quick_log('无法删除文件: {}'.format(e)).flush()
                image_uuid, png_path = self.download_code()
            if time_int() - last_time > 300:
                last_time = time_int()
                image_uuid, png_path = self.download_code()
        try:
            os.remove(png_path)
        except Exception as e:
            UserLog.add_quick_log('无法删除文件: {}'.format(e)).flush()

        self.session.get(API_USER_LOGIN, allow_redirects=True)
        new_tk = self.auth_uamtk()
        user_name = self.auth_uamauthclient(new_tk)
        self.update_user_info({'user_name': user_name})
        self.session.get(API_USER_LOGIN, allow_redirects=True)
        self.login_did_success()
        return True

    def login2(self):
        data = {
            'username': self.user_name,
            'password': self.password,
        }
        headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/94.0.4606.61 Safari/537.36",
                }
        self.session.headers.update(headers)
        cookies, post_data = Browser().request_init_slide2(self.session, data)
        while not cookies or not post_data:
            cookies, post_data = Browser().request_init_slide2(self.session, data)
        for cookie in cookies:
            self.session.cookies.update({
                   cookie['name']: cookie['value']
            })
        response = self.session.post(API_BASE_LOGIN.get('url')+ '?' + post_data)
        result = response.json()
        if result.get('result_code') == 0:  # 登录成功
            """
            login 获得 cookie uamtk
            auth/uamtk      不请求，会返回 uamtk票据内容为空
            /otn/uamauthclient 能拿到用户名
            """
            new_tk = self.auth_uamtk()
            user_name = self.auth_uamauthclient(new_tk)
            self.update_user_info({'user_name': user_name})
            self.login_did_success()
            return True
        elif result.get('result_code') == 2:  # 账号之内错误
            # 登录失败，用户名或密码为空
            # 密码输入错误
            UserLog.add_quick_log(UserLog.MESSAGE_LOGIN_FAIL.format(result.get('result_message'))).flush()
        else:
            UserLog.add_quick_log(
                UserLog.MESSAGE_LOGIN_FAIL.format(result.get('result_message', result.get('message',
                                                                                          CommonLog.MESSAGE_RESPONSE_EMPTY_ERROR)))).flush()

        return False

    def download_code(self):
        try:
            UserLog.add_quick_log(UserLog.MESSAGE_QRCODE_DOWNLOADING).flush()
            response = self.session.post(API_AUTH_QRCODE_BASE64_DOWNLOAD.get('url'), data={'appid': 'otn'})
            result = response.json()
            if result.get('result_code') == '0':
                img_bytes = base64.b64decode(result.get('image'))
                try:
                    os.mkdir(Config().USER_DATA_DIR + '/qrcode')
                except FileExistsError:
                    pass
                png_path = path.normpath(Config().USER_DATA_DIR + '/qrcode/%d.png' % time.time())
                with open(png_path, 'wb') as file:
                    file.write(img_bytes)
                    file.close()
                if os.name == 'nt':
                    os.startfile(png_path)
                else:
                    print_qrcode(png_path)
                UserLog.add_log(UserLog.MESSAGE_QRCODE_DOWNLOADED.format(png_path)).flush()
                Notification.send_email_with_qrcode(Config().EMAIL_RECEIVER, '你有新的登录二维码啦!', png_path)
                self.retry_count = 0
                return result.get('uuid'), png_path
            raise KeyError('获取二维码失败: {}'.format(result.get('result_message')))
        except Exception as e:
            sleep_time = get_interval_num(self.sleep_interval)
            UserLog.add_quick_log(
                UserLog.MESSAGE_QRCODE_FAIL.format(e, sleep_time)).flush()
            time.sleep(sleep_time)
            self.request_device_id(self.retry_count % 20 == 0)
            self.retry_count += 1
            return self.download_code()

    def is_rate_limit_response(self, response):
        """判断响应是否属于「12306 限流/风控」软错误，而不是登录态失效。
        典型表现：请求 302 到 https://www.12306.cn/mormhweb/logFiles/error.html
        （正文「网络可能存在问题，请您重试一下」），或直接非 200。
        这类响应只说明当前被打满/被风控，不应被当成「登录失效」去重新扫码。"""
        if getattr(response, 'status_code', 0) != 200:
            return True
        if getattr(response, 'history', None):  # 发生跳转（302 → error.html）
            return True
        return False

    def check_user_is_login(self):
        """探测登录态（/otn/login/conf）。
        返回 True  = 已登录；
        返回 False = 明确未登录（需要重新扫码）；
        返回 None  = 被 12306 限流/网络软错误，无法确认（调用方应退避，不要重新登录）。"""
        retry = 0
        saw_soft_error = False
        while retry < Config().REQUEST_MAX_RETRY:
            retry += 1
            response = self.session.get(API_USER_LOGIN_CHECK)
            if self.is_rate_limit_response(response):
                saw_soft_error = True
                time.sleep(get_interval_num(self.sleep_interval))
                continue
            is_login = response.json().get('data.is_login', False) == 'Y'
            if is_login:
                self.save_user()
                self.set_last_heartbeat()
                self.get_user_info()  # 尽力刷新用户信息，失败也不影响「已登录」判定
                return True
            return False
        return None if saw_soft_error else False

    def renew_session(self):
        """用仍有效的 uamtk cookie 换取新的 tk（免扫码续期）。
        12306 的 tk 有过期时间；定时在过期前续期，可避免「跑一段时间后 tk 失效 → 被迫重新扫码」。
        仅当 uamtk 也被风控吊销（拿到不到 newapptk）时才失败，那才是真正需要重新扫码的情形。"""
        if time_int() - self.last_tk_renew < self.tk_renew_interval:
            return True
        new_tk = self.auth_uamtk()
        if not new_tk:
            return False
        user_name = self.auth_uamauthclient(new_tk)
        if user_name:
            self.update_user_info({'user_name': user_name})
            self.save_user()
            self.last_tk_renew = time_int()
            return True
        return False

    def auth_uamtk(self):
        retry = 0
        while retry < Config().REQUEST_MAX_RETRY:
            retry += 1
            response = self.session.post(API_AUTH_UAMTK.get('url'), {'appid': 'otn'}, headers={
                'Referer': 'https://kyfw.12306.cn/otn/passport?redirect=/otn/login/userLogin',
                'Origin': 'https://kyfw.12306.cn'
            })
            result = response.json()
            if result.get('newapptk'):
                return result.get('newapptk')
            # TODO 处理获取失败情况
        return False

    def auth_uamauthclient(self, tk):
        retry = 0
        while retry < Config().REQUEST_MAX_RETRY:
            retry += 1
            response = self.session.post(API_AUTH_UAMAUTHCLIENT.get('url'), {'tk': tk})
            result = response.json()
            if result.get('username'):
                return result.get('username')
            # TODO 处理获取失败情况
        return False

    def request_device_id(self, force_renew=False, _retry=0):
        """
        获取加密后的浏览器特征 ID
        注意：原第三方服务已停服，这里最多重试 1 次后放弃，避免无限递归。
        :return:
        """
        # 优先使用 env.py 手动缓存的设备 ID：只需在 env.py 填好 RAIL_EXPIRATION/RAIL_DEVICEID
        # 并开启 CACHE_RAIL_ID_ENABLED=1，即可直接注入，彻底跳过已停服的第三方设备 ID 服务。
        if self.apply_cached_rail_id():
            return
        # 判断cookie 是否过期，未过期可以不必下载
        expire_time =  self.session.cookies.get('RAIL_EXPIRATION')
        if not force_renew and expire_time and int(expire_time) - time_int_ms() > 0:
            return
        if 'pjialin' not in API_GET_BROWSER_DEVICE_ID:
            return self.request_device_id2()
        try:
            response = self.session.get(API_GET_BROWSER_DEVICE_ID)
            if response.status_code == 200:
                result = json.loads(response.text)
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/94.0.4606.61 Safari/537.36"
                }
                self.session.headers.update(headers)
                response = self.session.get(base64.b64decode(result['id']).decode())
                if response.text.find('callbackFunction') >= 0:
                    result = response.text[18:-2]
                result = json.loads(result)
                if not Config().is_cache_rail_id_enabled():
                   self.session.cookies.update({
                       'RAIL_EXPIRATION': result.get('exp'),
                       'RAIL_DEVICEID': result.get('dfp'),
                   })
                else:
                   self.session.cookies.update({
                       'RAIL_EXPIRATION': Config().RAIL_EXPIRATION,
                       'RAIL_DEVICEID': Config().RAIL_DEVICEID,
                   })
        except Exception:
            if _retry < 1:
                return self.request_device_id(force_renew, _retry + 1)

    def request_device_id2(self, _retry=0):
        headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/94.0.4606.61 Safari/537.36"
        }
        self.session.headers.update(headers)
        try:
            response = self.session.get(API_GET_BROWSER_DEVICE_ID)
            if response.status_code == 200:
                if response.text.find('callbackFunction') >= 0:
                    result = response.text[18:-2]
                    result = json.loads(result)
                    if not Config().is_cache_rail_id_enabled():
                       self.session.cookies.update({
                           'RAIL_EXPIRATION': result.get('exp'),
                           'RAIL_DEVICEID': result.get('dfp'),
                       })
                    else:
                       self.session.cookies.update({
                           'RAIL_EXPIRATION': Config().RAIL_EXPIRATION,
                           'RAIL_DEVICEID': Config().RAIL_DEVICEID,
                       })
        except Exception:
            if _retry < 1:
                return self.request_device_id2(_retry + 1)

    def apply_cached_rail_id(self):
        """若 env.py 配置了手动缓存的设备 ID（CACHE_RAIL_ID_ENABLED=1 且 RAIL_DEVICEID 非空），
        直接把 RAIL_EXPIRATION/RAIL_DEVICEID 注入会话 cookie 并返回 True；否则返回 False。"""
        if not (Config().is_cache_rail_id_enabled() and Config().RAIL_DEVICEID):
            return False
        try:
            self.session.cookies.update({
                'RAIL_EXPIRATION': available_value(Config().RAIL_EXPIRATION),
                'RAIL_DEVICEID': available_value(Config().RAIL_DEVICEID),
            })
            return True
        except Exception:
            return False

    def login_did_success(self):
        """
        用户登录成功
        :return:
        """
        self.login_num += 1
        self.welcome_user()
        self.save_user()
        self.get_user_info()
        self.set_last_heartbeat()
        self.is_ready = True

    def welcome_user(self):
        UserLog.print_welcome_user(self)
        pass

    def get_cookie_path(self):
        return Config().USER_DATA_DIR + self.user_name + '.cookie'

    def update_user_info(self, info):
        self.info = {**self.info, **info}

    def get_name(self):
        return self.info.get('user_name', '')

    def save_user(self):
        if Config().is_master():
            self.cluster.set_user_cookie(self.key, self.session.cookies)
            self.cluster.set_user_info(self.key, self.info)
        with open(self.get_cookie_path(), 'wb') as f:
            pickle.dump(self.session.cookies, f)

    def did_loaded_user(self):
        """
        恢复用户成功
        :return:
        """
        UserLog.add_quick_log(UserLog.MESSAGE_LOADED_USER.format(self.user_name)).flush()
        is_login = self.check_user_is_login()
        if is_login is None:  # 被限流，无法确认登录态：先保留现有会话，等下一轮心跳再确认，不立刻重新登录
            UserLog.add_quick_log('恢复用户时登录接口被 12306 限流，暂保持现有会话，稍后自动再确认').flush()
            return True
        if not is_login:
            UserLog.add_quick_log(UserLog.MESSAGE_LOADED_USER_BUT_EXPIRED).flush()
            self.set_last_heartbeat(0)
            return False
        passengers_ok = self.can_access_passengers()
        if passengers_ok is None:  # 乘客接口被限流：先保留现有会话，不立刻重新登录
            UserLog.add_quick_log('恢复用户时乘客接口被 12306 限流，暂保持现有会话，稍后自动再确认').flush()
            return True
        if not passengers_ok:
            UserLog.add_quick_log(UserLog.MESSAGE_LOADED_USER_BUT_EXPIRED).flush()
            self.set_last_heartbeat(0)
            return False
        UserLog.add_quick_log(UserLog.MESSAGE_LOADED_USER_SUCCESS.format(self.user_name)).flush()
        UserLog.print_welcome_user(self)
        self.user_did_load()
        return True

    def user_did_load(self):
        """
        用户已经加载成功
        :return:
        """
        self.is_ready = True
        if self.user_loaded: return
        self.user_loaded = True
        Event().user_loaded({'key': self.key})  # 发布通知

    def get_user_info(self):
        retry = 0
        while retry < Config().REQUEST_MAX_RETRY:
            retry += 1
            response = self.session.get(API_USER_INFO.get('url'))
            result = response.json()
            user_data = result.get('data.userDTO.loginUserDTO')
            # 子节点访问会导致主节点登录失效 TODO 可快考虑实时同步 cookie
            if user_data:
                self.update_user_info({**user_data, **{'user_name': user_data.get('name')}})
                self.save_user()
                return True
            time.sleep(get_interval_num(self.sleep_interval))
        return False

    def load_user(self):
        if Config().is_cluster_enabled(): return
        cookie_path = self.get_cookie_path()

        if path.exists(cookie_path):
            with open(self.get_cookie_path(), 'rb') as f:
                cookie = pickle.load(f)
                self.cookie = True
                self.session.cookies.update(cookie)
                self.apply_cached_rail_id()  # 注入手动缓存的设备 ID（若配置）
                return self.did_loaded_user()
        return None

    def load_user_from_remote(self):
        cookie = self.cluster.get_user_cookie(self.key)
        info = self.cluster.get_user_info(self.key)
        if Config().is_slave() and (not cookie or not info):
            while True:  # 子节点只能取
                UserLog.add_quick_log(UserLog.MESSAGE_USER_COOKIE_NOT_FOUND_FROM_REMOTE.format(self.user_name)).flush()
                stay_second(self.retry_time)
                return self.load_user_from_remote()
        if info: self.info = info
        if cookie:
            self.session.cookies.update(cookie)
            if not self.cookie:  # 第一次加载
                self.cookie = True
                if not Config().is_slave():
                    self.did_loaded_user()
                else:
                    self.is_ready = True  # 设置子节点用户 已准备好
                    UserLog.print_welcome_user(self)
            return True
        return False

    def check_is_ready(self):
        return self.is_ready

    def wait_for_ready(self):
        if self.is_ready: return self
        UserLog.add_quick_log(UserLog.MESSAGE_WAIT_USER_INIT_COMPLETE.format(self.retry_time)).flush()
        stay_second(self.retry_time)
        return self.wait_for_ready()

    def destroy(self):
        """
        退出用户
        :return:
        """
        UserLog.add_quick_log(UserLog.MESSAGE_USER_BEING_DESTROY.format(self.user_name)).flush()
        self.is_alive = False

    def response_login_check(self, response, **kwargs):
        if Config().is_master() and response.json().get('data.noLogin') == 'true':  # relogin
            self.handle_login(expire=True)

    def get_user_passengers(self, _retry=0):
        if self.passengers: return self.passengers
        # 冷却期：连续失败后先别再打接口，否则会被 12306 限流（返回 302 → error.html）
        if time_int() < self.passengers_fail_until:
            return None
        response = self.session.post(API_USER_PASSENGERS)
        result = response.json()
        if result.get('data.normal_passengers'):
            self.passengers = result.get('data.normal_passengers')
            self.passengers_fail_until = 0
            # 将乘客写入到文件
            with open(Config().USER_PASSENGERS_FILE % self.user_name, 'w', encoding='utf-8') as f:
                f.write(json.dumps(self.passengers, indent=4, ensure_ascii=False))
            return self.passengers
        else:
            self.__dump_raw_response('getPassengerDTOs', response)
            wait_time = get_interval_num(self.sleep_interval)
            UserLog.add_quick_log(
                UserLog.MESSAGE_GET_USER_PASSENGERS_FAIL.format(
                    result.get('messages', CommonLog.MESSAGE_RESPONSE_EMPTY_ERROR), wait_time)).flush()
            if Config().is_slave():
                self.load_user_from_remote()  # 加载最新 cookie
            # 重试必须有上限：原实现失败后无限递归、每 0.1~5 秒重刷一次，会把 12306 刷到限流，
            # 结果越刷越坏。这里到上限就进入冷却并返回 None（上层会跳过本轮，不销毁任务）。
            if _retry >= Config().REQUEST_MAX_RETRY:
                self.passengers_fail_until = time_int() + self.passengers_fail_cooldown
                UserLog.add_quick_log(
                    '获取乘客列表连续失败 {} 次，冷却 {} 秒后再试（避免刷爆 12306 接口）'.format(
                        _retry, self.passengers_fail_cooldown)).flush()
                return None
            stay_second(wait_time)
            return self.get_user_passengers(_retry + 1)

    def can_access_passengers(self):
        """确认 otn 会话（乘客接口 /otn/confirmPassenger/getPassengerDTOs）是否可用。
        返回 True  = 可用；False = 明确未登录；None = 被限流/软错误，无法确认，应退避而非重新登录。"""
        retry = 0
        saw_soft_error = False
        while retry < Config().REQUEST_MAX_RETRY:
            retry += 1
            response = self.session.post(API_USER_PASSENGERS)
            if self.is_rate_limit_response(response):
                saw_soft_error = True
                wait_time = get_interval_num(self.sleep_interval)
                UserLog.add_quick_log(
                    UserLog.MESSAGE_TEST_GET_USER_PASSENGERS_FAIL.format('限流(302/error.html)', wait_time)).flush()
                stay_second(wait_time)
                continue
            result = response.json()
            if result.get('data.normal_passengers'):
                return True
            # 明确的「未登录」信号（12306 返回 noLogin / exMsg=用户未登录）
            if result.get('data.noLogin') == 'true' or result.get('noLogin') == 'true' or result.get('exMsg') == '用户未登录':
                return False
            # 其它失败（空响应/网络抖动等）视为软错误：退避，不判定登出
            saw_soft_error = True
            wait_time = get_interval_num(self.sleep_interval)
            UserLog.add_quick_log(
                UserLog.MESSAGE_TEST_GET_USER_PASSENGERS_FAIL.format(
                    result.get('messages', CommonLog.MESSAGE_RESPONSE_EMPTY_ERROR), wait_time)).flush()
            if Config().is_slave():
                self.load_user_from_remote()  # 加载最新 cookie
            stay_second(wait_time)
        return None if saw_soft_error else False

    def get_passengers_by_members(self, members):
        """
        获取格式化后的乘客信息
        :param members:
        :return:
        [{
            name: '项羽',
            type: 1,
            id_card: 0000000000000000000,
            type_text: '成人',
            enc_str: 'aaaaaa'
        }]
        """
        if not self.get_user_passengers():
            # 乘客列表没取到（网络/登录态问题）：返回 None 交给上层跳过本轮，
            # 不要继续往下走，否则会被误判成「乘客不存在」而销毁查询任务
            return None
        results = []
        for member in members:
            is_member_code = is_number(member)
            if not is_member_code:
                if member[0] == "*":
                    audlt = 1
                    member = member[1:]
                else:
                    audlt = 0
                child_check = array_dict_find_by_key_value(results, 'name', member)
            if not is_member_code and child_check:
                new_member = child_check.copy()
                new_member['type'] = UserType.CHILD
                new_member['type_text'] = dict_find_key_by_value(UserType.dicts, int(new_member['type']))
            else:
                if is_member_code:
                    passenger = array_dict_find_by_key_value(self.passengers, 'code', member)
                else:
                    passenger = array_dict_find_by_key_value(self.passengers, 'passenger_name', member)
                    if audlt:
                        passenger['passenger_type'] = UserType.ADULT
                if not passenger:
                    UserLog.add_quick_log(
                        UserLog.MESSAGE_USER_PASSENGERS_IS_INVALID.format(self.user_name, member)).flush()
                    return False
                new_member = {
                    'name': passenger.get('passenger_name'),
                    'id_card': passenger.get('passenger_id_no'),
                    'id_card_type': passenger.get('passenger_id_type_code'),
                    'mobile': passenger.get('mobile_no'),
                    'type': passenger.get('passenger_type'),
                    'type_text': dict_find_key_by_value(UserType.dicts, int(passenger.get('passenger_type'))),
                    'enc_str': passenger.get('allEncStr')
                }
            results.append(new_member)

        return results

    def request_init_dc_page(self):
        """
        请求下单页面 拿到 token
        :return:
        """
        data = {'_json_att': ''}
        response = self.session.post(API_INITDC_URL, data)
        html = response.text
        token = re.search(r'var globalRepeatSubmitToken = \'(.+?)\'', html)
        form_raw = extract_js_object(html, 'ticketInfoForPassengerForm')
        order_raw = extract_js_object(html, 'orderRequestDTO')
        # 系统忙，请稍后重试
        if html.find('系统忙，请稍后重试') != -1:
            OrderLog.add_quick_log(OrderLog.MESSAGE_REQUEST_INIT_DC_PAGE_FAIL).flush()  # 重试无用，直接跳过
            self.__dump_init_dc_html(response, html, token, form_raw, order_raw, '系统忙，请稍后重试')
            return False, False, html
        try:
            if not token or not form_raw:
                raise ValueError('未匹配到 globalRepeatSubmitToken / ticketInfoForPassengerForm')
            self.global_repeat_submit_token = token.groups()[0]
            self.ticket_info_for_passenger_form = json.loads(js_literal_to_json(form_raw))
            self.order_request_dto = json.loads(js_literal_to_json(order_raw)) if order_raw else None
        except Exception as e:
            self.__dump_init_dc_html(response, html, token, form_raw, order_raw, repr(e))
            return False, False, html  # TODO Error

        slide_val = re.search(r"var if_check_slide_passcode.*='(\d?)'", html)
        is_slide = False
        if slide_val:
            is_slide = int(slide_val[1]) == 1
        return True, is_slide, html

    def __dump_init_dc_html(self, response, html, token, form, order, err):
        """诊断用：initDc 页面解析失败时，把 HTML 与页面变量名 dump 到文件，便于定位新结构"""
        if self._init_dc_dumped:
            return
        self._init_dc_dumped = True
        try:
            dump_dir = Config().RUNTIME_DIR + 'debug/'
            os.makedirs(dump_dir, exist_ok=True)
            html_path = dump_dir + 'initDc_dump.html'
            vars_path = dump_dir + 'initDc_vars.txt'
            status = getattr(response, 'status_code', '?')
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write('<!-- status=%s -->\n' % status)
                f.write(html if html else '')
            var_names = sorted(set(re.findall(r'var\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=', html or '')))
            with open(vars_path, 'w', encoding='utf-8') as f:
                f.write('status=%s\n' % status)
                f.write('token(globalRepeatSubmitToken)=%s\n' % ('命中' if token else '未命中'))
                f.write('form(ticketInfoForPassengerForm)=%s\n' % ('命中' if form else '未命中'))
                f.write('order(orderRequestDTO)=%s\n' % ('命中' if order else '未命中'))
                f.write('err=%s\n' % err)
                f.write('含"请登录/登录页"=%s\n' % ('是' if (html and ('请登录' in html or 'login.html' in html)) else '否'))
                f.write('\n页面中出现的 var 变量名:\n')
                f.write('\n'.join(var_names))
            OrderLog.add_quick_log(
                'initDc 页面解析失败(token={} form={} order={} err={})，已 dump 到 {}'.format(
                    '命中' if token else '未命中',
                    '命中' if form else '未命中',
                    '命中' if order else '未命中',
                    err, vars_path)).flush()
        except Exception as e:
            OrderLog.add_quick_log('dump initDc 页面失败: {}'.format(e)).flush()

    def __dump_raw_response(self, name, response):
        """诊断用：把某个接口的原始响应（状态码/URL/跳转历史/正文）dump 到文件，便于定位失败原因"""
        if name in self._raw_dumped:
            return
        self._raw_dumped.add(name)
        try:
            dump_dir = Config().RUNTIME_DIR + 'debug/'
            os.makedirs(dump_dir, exist_ok=True)
            p = dump_dir + '%s_dump.txt' % name
            with open(p, 'w', encoding='utf-8') as f:
                f.write('status=%s\n' % getattr(response, 'status_code', '?'))
                f.write('url=%s\n' % getattr(response, 'url', '?'))
                try:
                    f.write('content-type=%s\n' % response.headers.get('Content-Type', ''))
                except Exception:
                    f.write('content-type=?\n')
                try:
                    f.write('history(跳转记录)=%s\n' % [(r.status_code, r.url) for r in response.history])
                except Exception:
                    f.write('history=?\n')
                f.write('\n--- BODY (前 8000 字符) ---\n')
                f.write((response.text or '')[:8000])
            OrderLog.add_quick_log('已 dump {} 原始响应到 {}'.format(name, p)).flush()
        except Exception as e:
            OrderLog.add_quick_log('dump {} 失败: {}'.format(name, e)).flush()

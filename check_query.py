# -*- coding: utf-8 -*-
"""余票查询连通性自检脚本（仅标准库，无项目依赖）。

用法：
    python3 check_query.py                                  # 默认 广州 -> 郑州 @ 2026-10-01
    python3 check_query.py 2026-10-05                       # 只换日期
    python3 check_query.py 2026-10-05 ZZF GZQ               # 日期 + 出发站码 + 到达站码
    python3 check_query.py --probe 2026-10-05 ZZF GZQ       # 只报状态码（同一个 session 连续试几个日期）

⚠️ 必须用同一个 session（先访问 init 页拿 JSESSIONID 等 cookie）再查余票，否则 12306 一律 302。
   「超出预售期（今天 + 14 天以外）的日期」也是 302 到 error.html，和限流响应一模一样 —— 见 AGENTS.md「十一」。
"""
import argparse, datetime, json, re, ssl, sys, urllib.error, urllib.parse, urllib.request
from http.cookiejar import CookieJar

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
REFERER = 'https://kyfw.12306.cn/otn/leftTicket/init'
INIT_URL = 'https://kyfw.12306.cn/otn/leftTicket/init'
LEFT_STATION = 'GZQ'
ARRIVE_STATION = 'ZZF'
TRAIN_DATE = '2026-10-01'
TARGET_TRAINS = ['G404', 'G1186', 'G382', 'G408', 'G426', 'G696', 'G838', 'G842', 'G846', 'G914']
SEAT_IDX = {'二等座': 30, '一等座': 31, '商务座': 32, '无座': 26}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """不自动跟随跳转：302 要显式看到状态码和 Location，才能判断是不是被跳到了 error.html。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_opener(redirect=True):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    handlers = [urllib.request.HTTPSHandler(context=ctx),
                urllib.request.HTTPCookieProcessor(CookieJar())]
    if not redirect:
        handlers.append(NoRedirect())
    return urllib.request.build_opener(*handlers)


def get(opener, url, referer=None):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    if referer:
        req.add_header('Referer', referer)
    return opener.open(req, timeout=12)


def fetch(opener, api_type, date, left, arrive):
    """返回 (状态码, 跳转地址, 正文)。302 不抛异常，直接报出来，方便定位。"""
    url = ('https://kyfw.12306.cn/otn/{type}?leftTicketDTO.train_date={date}'
           '&leftTicketDTO.from_station={left}&leftTicketDTO.to_station={arrive}'
           '&purpose_codes=ADULT').format(type=api_type, date=date, left=left, arrive=arrive)
    try:
        resp = get(opener, url, referer=REFERER)
        return resp.status, None, resp.read().decode('utf-8', 'ignore')
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get('Location'), ''


def print_302_help(date, location, left, arrive):
    print('    跳转到：%s' % location)
    print('[失败] 未返回 200。两种可能，必须区分：')
    print('    ① 会话/Referer 不对，或已被 12306 限流；')
    print('    ② 乘车日期超出 12306 预售期（今天 + 14 天以外）—— 预售期外的日期就是 302 到 error.html，')
    print('       响应与限流一模一样，无法从响应本身区分。')
    print('    用 --probe 换几个日期对比即可定性：')
    print('      python3 check_query.py --probe %s %s %s' % (date, left, arrive))


def probe_presale_boundary(opener, api_type, left, arrive):
    """探测预售期边界：今天+13/+14/+15/+16 四个日期的状态码，用来确认「能查到哪一天」。"""
    now = datetime.date.today()
    print('\n== 预售期边界探测（今天 %s）==' % now)
    for offset in (13, 14, 15, 16):
        date = (now + datetime.timedelta(days=offset)).strftime('%Y-%m-%d')
        status, location, body = fetch(opener, api_type, date, left, arrive)
        if status == 200:
            try:
                count = len(json.loads(body).get('data', {}).get('result') or [])
            except Exception:
                count = -1
            print('    今天+%-2d %s  HTTP 200  result=%d' % (offset, date, count))
        else:
            print('    今天+%-2d %s  HTTP %s -> %s' % (offset, date, status, location))


def main():
    today = datetime.date.today()
    default_date = TRAIN_DATE
    if datetime.datetime.strptime(TRAIN_DATE, '%Y-%m-%d').date() < today:
        default_date = (today + datetime.timedelta(days=1)).strftime('%Y-%m-%d')

    parser = argparse.ArgumentParser(description='12306 余票查询连通性自检')
    parser.add_argument('date', nargs='?', default=default_date,
                        help='乘车日期 YYYY-MM-DD（默认 %s）' % default_date)
    parser.add_argument('from_station', nargs='?', default=LEFT_STATION, help='出发站电报码，如 GZQ/ZZF')
    parser.add_argument('to_station', nargs='?', default=ARRIVE_STATION, help='到达站电报码')
    parser.add_argument('--probe', action='store_true', help='额外对比 今天+13/+14/+15/+16，判断预售期边界')
    args = parser.parse_args()

    date, left, arrive = args.date, args.from_station, args.to_station

    opener = build_opener(redirect=False)
    print('== 1) 访问 init 页 ==')
    try:
        resp = get(opener, INIT_URL)
        html = resp.read().decode('utf-8', 'ignore')
    except Exception as e:
        print('[失败] 无法访问 12306 init 页：%s' % e)
        print('请确认本机能否访问 https://kyfw.12306.cn')
        return 1
    m = re.search(r"var CLeftTicketUrl = '(.*?)';", html)
    api_type = m.group(1) if m else 'leftTicket/queryG'
    print('    HTTP %s, 查询类型=%s' % (resp.status, api_type))

    print('== 2) 查询 %s -> %s @ %s ==' % (left, arrive, date))
    status, location, body = fetch(opener, api_type, date, left, arrive)
    print('    HTTP %s' % status)
    if status != 200:
        print_302_help(date, location, left, arrive)
        if args.probe:
            probe_presale_boundary(opener, api_type, left, arrive)
        return 1

    data = json.loads(body)
    result = data.get('data', {}).get('result', []) or []
    rows = []
    for item in result:
        for line in urllib.parse.unquote(item).split('\n'):
            if '|' in line:
                rows.append(line.split('|'))
    print('    共 %d 条车次记录\n' % len(rows))
    print('%-8s %-12s %-8s %-8s %-8s %-6s' % ('车次', '出发-到达', '二等座', '一等座', '商务座', '可购'))
    found = set()
    for f in rows:
        if len(f) < 33:
            continue
        train = f[3]
        if train not in TARGET_TRAINS:
            continue
        found.add(train)
        seat = lambda k: (f[SEAT_IDX[k]] if SEAT_IDX[k] < len(f) and f[SEAT_IDX[k]] else '-')
        print('%-8s %-12s %-8s %-8s %-8s %-6s' % (train, '%s-%s' % (f[8], f[9]),
              seat('二等座'), seat('一等座'), seat('商务座'), f[11]))
    missing = set(TARGET_TRAINS) - found
    print('\n    命中目标车次 %d/%d' % (len(found), len(TARGET_TRAINS)))
    if missing:
        print('    未命中：%s' % ', '.join(sorted(missing)))
    if not rows:
        print('\n[提示] 查询成功但一条记录都没有：可能是该日期刚开售、或这趟方向当天确实无车。')
    if args.probe:
        probe_presale_boundary(opener, api_type, left, arrive)
    print('\n[OK] 查询接口连通正常。')
    return 0


if __name__ == '__main__':
    sys.exit(main())

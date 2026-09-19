# -*- coding: utf-8 -*-
"""余票查询连通性自检脚本（仅标准库，无项目依赖）。用法：python3 check_query.py"""
import json, re, ssl, sys, urllib.parse, urllib.request
from http.cookiejar import CookieJar

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
REFERER = 'https://kyfw.12306.cn/otn/leftTicket/init'
INIT_URL = 'https://kyfw.12306.cn/otn/leftTicket/init'
LEFT_STATION = 'GZQ'
ARRIVE_STATION = 'ZZF'
TRAIN_DATE = '2026-10-01'
TARGET_TRAINS = ['G404', 'G1186', 'G382', 'G408', 'G426', 'G696', 'G838', 'G842', 'G846', 'G914']
SEAT_IDX = {'二等座': 30, '一等座': 31, '商务座': 32, '无座': 26}


def build_opener():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(CookieJar()))


def get(opener, url, referer=None):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    if referer:
        req.add_header('Referer', referer)
    return opener.open(req, timeout=12)


def main():
    opener = build_opener()
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

    print('== 2) 查询 %s -> %s @ %s ==' % (LEFT_STATION, ARRIVE_STATION, TRAIN_DATE))
    url = ('https://kyfw.12306.cn/otn/{type}?leftTicketDTO.train_date={date}'
           '&leftTicketDTO.from_station={left}&leftTicketDTO.to_station={arrive}'
           '&purpose_codes=ADULT').format(type=api_type, date=TRAIN_DATE,
                                          left=LEFT_STATION, arrive=ARRIVE_STATION)
    try:
        resp = get(opener, url, referer=REFERER)
        body = resp.read().decode('utf-8', 'ignore')
    except Exception as e:
        print('[失败] 查询请求异常：%s' % e)
        return 1
    print('    HTTP %s' % resp.status)
    if resp.status != 200:
        print('[失败] 未返回 200（可能 302 到错误页，缺少 Referer/会话）。')
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
    print('\n[OK] 查询接口连通正常。')
    return 0


if __name__ == '__main__':
    sys.exit(main())

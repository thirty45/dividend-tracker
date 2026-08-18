# -*- coding: utf-8 -*-
"""可转债数据生成：已上市 + 已过会·未上市（含待申购）。

数据源（均为公开接口，纯标准库）：
1) 东财行情 clist（push2 优先，push2delay 兜底）
   fs=m:0+b:MK0354,m:1+b:MK0354（深/沪可转债板块）
   字段：f12代码 f14名称 f2现价 f3涨跌幅 f232正股代码 f234正股名称
        f235转股价 f236转股价值 f237转股溢价率 f243申购日期
2) 东财数据中心 RPT_BOND_CB_LIST（datacenter-web）
   补充：上市日期、配售登记日（公告日=申购日前一交易日）、原股东每股配售、网上中签率

输出：data/cbonds.json
  {"date": 交易日期, "updated_at": ISO, "total": {listed, pending},
   "listed": [{code,name,price,premium_rt,convert_price,stock_code,stock_name,
               apply_date,list_date,record_date,ration,lot_rate}...],
   "pending": [...同上，price/premium_rt 未上市时按面值计算或空]}
"""
import datetime
import json
import math
import os
import sys
import urllib.parse
import urllib.request

# 复用股票 K 线抓取（含东财/腾讯双源与熔断）
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import sources_market as sm  # noqa: E402

QUOTE_HOSTS = [
    "https://push2.eastmoney.com",
    "https://push2delay.eastmoney.com",
]
DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
ANN_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
UT = "bd1d9ddb04089700cf9c27f6f7426281"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def fetch(url, referer="https://quote.eastmoney.com/", timeout=15, retry=3):
    """GET 并返回文本；失败重试，最终失败返回 None。"""
    last = None
    for _ in range(retry):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Referer": referer})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            last = e
    print("fetch fail: %s (%s)" % (url[:140], last), file=sys.stderr)
    return None


def pick_host():
    """找到可用的行情主机。"""
    for host in QUOTE_HOSTS:
        url = host + "/api/qt/clist/get?" + urllib.parse.urlencode({
            "pn": 1, "pz": 1, "po": 1, "np": 1, "fltt": 2, "invt": 2, "ut": UT,
            "fid": "f243", "fs": "m:0+b:MK0354,m:1+b:MK0354",
            "fields": "f12",
        })
        txt = fetch(url, referer="https://quote.eastmoney.com/", timeout=8)
        if txt and '"f12"' in txt:
            return host
    return QUOTE_HOSTS[0]


def fetch_quotes(host):
    """分页拉取全部可转债行情，返回 {code: row}。"""
    rows = {}
    pn = 1
    while pn <= 8:
        url = host + "/api/qt/clist/get?" + urllib.parse.urlencode({
            "pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2, "ut": UT,
            "fid": "f243", "fs": "m:0+b:MK0354,m:1+b:MK0354",
            "fields": "f12,f14,f2,f3,f232,f234,f235,f236,f237,f243",
        })
        txt = fetch(url)
        if not txt:
            break
        try:
            data = (json.loads(txt).get("data") or {}).get("diff") or []
        except Exception:
            break
        if not data:
            break
        for r in data:
            code = r.get("f12")
            if code:
                rows[code] = r
        if len(data) < 100:
            break
        pn += 1
    return rows


def fetch_issue():
    """拉取 RPT_BOND_CB_LIST（发行/申购明细），按代码索引。"""
    out = {}
    pn = 1
    while pn <= 4:
        url = DC_URL + "?" + urllib.parse.urlencode({
            "reportName": "RPT_BOND_CB_LIST", "columns": "ALL",
            "pageSize": 500, "pageNumber": pn,
            "sortColumns": "PUBLIC_START_DATE", "sortTypes": "-1",
        })
        txt = fetch(url, referer="https://data.eastmoney.com/xg/xg/")
        if not txt:
            break
        try:
            data = (json.loads(txt).get("result") or {}).get("data") or []
        except Exception:
            break
        if not data:
            break
        for r in data:
            code = r.get("SECURITY_CODE")
            if code:
                out[code] = r
        if len(data) < 500:
            break
        pn += 1
    return out


def d10(v):
    """'2026-08-17 00:00:00' -> '2026-08-17'；空 -> ''"""
    if not v:
        return ""
    s = str(v)[:10]
    return s if len(s) == 10 else ""


def fetch_stock_bars(code, days=250):
    """正股近 days 个自然日的日K（前复权，东财优先/腾讯兜底）。返回 bars 或 []。
    注意 end 必须用真实日期：腾讯接口拒绝未来日期（2050 会 501）。
    网络偶发失败时重试最多 3 次。"""
    today = datetime.date.today()
    beg = (today - datetime.timedelta(days=days)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    for _ in range(3):
        try:
            _, bars = sm.fetch_kline(code, beg=beg, end=end)
            if bars:
                return bars
        except Exception:  # noqa: BLE001
            pass
    return []


def fetch_review_date(stock_code):
    """正股最近公告里匹配「可转换…审核通过」（上市委过会）公告日期。
    返回 'YYYY-MM-DD' 或 None。"""
    for page in (1, 2):
        url = ANN_URL + "?" + urllib.parse.urlencode({
            "sr": "-1", "page_size": 100, "page_index": page,
            "ann_type": "A", "client_source": "web",
            "stock_list": stock_code,
        })
        txt = fetch(url, referer="https://data.eastmoney.com/", timeout=15)
        if not txt:
            return None
        try:
            lst = ((json.loads(txt).get("data") or {}).get("list") or [])
        except Exception:  # noqa: BLE001
            return None
        if not lst:
            return None
        for x in lst:
            t = x.get("title") or ""
            if ("可转换" in t or "可转债" in t) and "审核通过" in t:
                d = d10(x.get("notice_date"))
                if d:
                    return d
    return None


def pct_from_bars(bars, n):
    """近 n 个交易日的累计涨跌幅：(末收盘/倒数第 n+1 收盘 − 1)×100。"""
    if not bars or len(bars) < n + 1:
        return None
    try:
        return round((bars[-1]["c"] / bars[-n - 1]["c"] - 1.0) * 100.0, 2)
    except (KeyError, TypeError, ZeroDivisionError):
        return None


def pct_since(bars, date_s):
    """自 date_s（含）起第一根K线的收盘至今累计涨跌幅。"""
    if not bars or not date_s:
        return None
    base = None
    for b in bars:
        if (b.get("d") or b.get("date") or "") >= date_s:
            base = b.get("c")
            break
    if base is None:
        return None
    try:
        return round((bars[-1]["c"] / base - 1.0) * 100.0, 2)
    except (KeyError, TypeError, ZeroDivisionError):
        return None


def num(v):
    """数字清洗：'-'/None/'' -> None"""
    if v is None or v == "-" or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build():
    today = datetime.date.today()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    host = pick_host()
    quotes = fetch_quotes(host)
    issue = fetch_issue()
    print("行情: %d 只 (host=%s) | 发行明细: %d 条" % (len(quotes), host, len(issue)))

    listed, pending = [], []
    for code, q in quotes.items():
        isu = issue.get(code) or {}
        name = q.get("f14") or ""
        # 排除定向可转债（不可公开申购）
        if "定转" in name:
            continue
        price = num(q.get("f2"))
        stock_code = q.get("f232") or ""
        stock_name = q.get("f234") or ""
        convert_price = num(q.get("f235"))
        convert_value = num(q.get("f236"))       # 转股价值 = 100/转股价×正股价
        premium = num(q.get("f237"))
        apply_date = d10(q.get("f243")) or d10(isu.get("PUBLIC_START_DATE"))
        list_date = d10(isu.get("LISTING_DATE"))
        record_date = d10(isu.get("SECURITY_START_DATE"))  # 公告日=申购日前一交易日(配售登记)
        ration = num(isu.get("FIRST_PER_PREPLACING"))     # 原股东每股配售(元)
        lot_rate = num(isu.get("ONLINE_GENERAL_LWR"))     # 网上中签率(小数)

        # 正股当前价 = 转股价值 × 转股价 ÷ 100（与溢价率同口径，实测与行情误差<0.3%）
        stock_price = None
        if convert_value is not None and convert_price:
            stock_price = round(convert_value * convert_price / 100.0, 2)
        # 配售：1手=1000元面值。需股数 = ceil(所需面值 / 每股配售额)；资金 = 正股价 × 股数
        need1 = need2 = amt1 = amt2 = None
        if ration:
            need1 = int(math.ceil(1000.0 / ration))
            need2 = int(math.ceil(2000.0 / ration))
            if stock_price:
                amt1 = round(need1 * stock_price)
                amt2 = round(need2 * stock_price)

        item = {
            "code": code, "name": name,
            "price": round(price, 3) if price is not None else None,
            "premium_rt": round(premium, 2) if premium is not None else None,
            "convert_price": convert_price,
            "stock_code": stock_code, "stock_name": stock_name,
            "apply_date": apply_date, "list_date": list_date,
            "record_date": record_date, "ration": ration,
            "lot_rate": lot_rate,
            "stock_price": stock_price,
            "need1": need1, "need2": need2, "amt1": amt1, "amt2": amt2,
        }
        if list_date and list_date <= today.isoformat():
            listed.append(item)
        else:
            pending.append(item)

    # —— 正股行情与过会日期（近5/10日涨跌幅、过会至今涨跌幅）——
    from concurrent.futures import ThreadPoolExecutor
    all_items = listed + pending
    stocks_needed = sorted({it.get("stock_code") for it in all_items if it.get("stock_code")})
    print("抓正股行情 %d 只..." % len(stocks_needed), flush=True)
    bars_map = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for code, bars in zip(stocks_needed, ex.map(fetch_stock_bars, stocks_needed)):
            bars_map[code] = bars
    # 过会日期：未上市 + 上市 180 天内（老转债公告太远，不再追溯）
    need_review = set()
    for it in all_items:
        ld = it.get("list_date") or ""
        if not ld or ld >= (today - datetime.timedelta(days=180)).isoformat():
            if it.get("stock_code"):
                need_review.add(it["stock_code"])
    print("查过会日期 %d 只..." % len(need_review), flush=True)
    review_map = {}
    if need_review:
        sl = sorted(need_review)
        with ThreadPoolExecutor(max_workers=8) as ex:
            for code, rd in zip(sl, ex.map(fetch_review_date, sl)):
                review_map[code] = rd
    for it in all_items:
        bars = bars_map.get(it.get("stock_code")) or []
        it["pct5"] = pct_from_bars(bars, 5)      # 正股近5个工作日累计涨跌幅
        it["pct10"] = pct_from_bars(bars, 10)    # 正股近10个工作日累计涨跌幅
        rd = review_map.get(it.get("stock_code"))
        it["review_date"] = rd                   # 过会日期(上市委审核通过公告)
        it["pct_review"] = pct_since(bars, rd) if rd else None  # 正股过会至今累计涨跌幅

    # 排序：未上市按申购日倒序（未来可申购在前）；已上市按上市日倒序
    pending.sort(key=lambda x: x["apply_date"], reverse=True)
    listed.sort(key=lambda x: (x["list_date"] or ""), reverse=True)

    out = {
        "date": today.isoformat(),
        "updated_at": now,
        "total": {"listed": len(listed), "pending": len(pending)},
        "listed": listed,
        "pending": pending,
    }
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "cbonds.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print("已写 %s | listed=%d pending=%d" % (path, len(listed), len(pending)))
    return out


if __name__ == "__main__":
    build()

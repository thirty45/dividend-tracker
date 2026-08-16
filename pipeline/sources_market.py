# -*- coding: utf-8 -*-
"""行情数据源：估值、ROE、分红、K线、板块、指数成分股（东方财富/新浪）。"""

import datetime
import re
import threading

from .net import fetch, fetch_json

DC_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
DC_WEB_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
SINA_INDEX_URL = ("http://vip.stock.finance.sina.com.cn/corp/go.php/"
                  "vII_NewestComponent/indexid/%s.phtml")


def latest_trade_date():
    """估值数据中心里最新的交易日。"""
    j = fetch_json(DC_URL, params={
        "reportName": "RPT_VALUEANALYSIS_DET",
        "columns": "TRADE_DATE",
        "pageSize": "1", "pageNumber": "1",
        "sortColumns": "TRADE_DATE", "sortTypes": "-1",
        "source": "WEB", "client": "WEB"},
        referer="https://data.eastmoney.com/", retries=4, delay=1.0)
    data = (j.get("result") or {}).get("data") or []
    if data:
        return str(data[0].get("TRADE_DATE"))[:10]
    return None


def fetch_valuation(trade_date=None, page_size=500, progress=None):
    """全市场估值快照（含 PE_TTM、PB_MRQ、总市值、行业板块等）。"""
    if not trade_date:
        trade_date = latest_trade_date()
    rows, page = [], 1
    while True:
        j = fetch_json(DC_URL, params={
            "reportName": "RPT_VALUEANALYSIS_DET",
            "columns": "ALL",
            "filter": "(TRADE_DATE='%s')" % trade_date,
            "pageSize": str(page_size), "pageNumber": str(page),
            "sortColumns": "SECURITY_CODE", "sortTypes": "1",
            "source": "WEB", "client": "WEB"},
            referer="https://data.eastmoney.com/", retries=4, delay=1.0)
        res = j.get("result") or {}
        data = res.get("data") or []
        rows.extend(data)
        pages = res.get("pages") or 0
        if page >= pages or not data:
            break
        page += 1
        if progress:
            progress("估值第 %d/%d 页" % (page, pages))
    return trade_date, rows


def _roe_candidates(today=None):
    today = today or datetime.date.today()
    y = today.year
    return ["%d-06-30" % y, "%d-03-31" % y, "%d-12-31" % (y - 1),
            "%d-09-30" % (y - 1)]


def fetch_roe():
    """最新报告期加权ROE：优先最新报告期，缺失的用更早报告期回填。"""
    merged = {}
    used_date = None
    for rd in _roe_candidates():
        rows, page = [], 1
        while True:
            j = fetch_json(DC_WEB_URL, params={
                "reportName": "RPT_LICO_FN_CPD",
                "columns": "SECURITY_CODE,WEIGHTAVG_ROE",
                "filter": "(REPORTDATE='%s')" % rd,
                "pageSize": "500", "pageNumber": str(page),
                "sortColumns": "SECURITY_CODE", "sortTypes": "1",
                "source": "WEB", "client": "WEB"},
                referer="https://data.eastmoney.com/", retries=4, delay=1.0)
            res = j.get("result") or {}
            data = res.get("data") or []
            if not data:
                break
            rows.extend(data)
            pages = res.get("pages") or 0
            if page >= pages:
                break
            page += 1
        if rows and used_date is None:
            used_date = rd
        for r in rows:
            code = r.get("SECURITY_CODE")
            if code and code not in merged and r.get("WEIGHTAVG_ROE") is not None:
                merged[code] = r["WEIGHTAVG_ROE"]
        if used_date is not None and len(merged) > 5200:
            break
    return used_date, merged


def fetch_sharebonus(start_date, end_date, page_size=500):
    """近一年已实施分红 -> {code: 每股税前现金分红}。"""
    per_share = {}
    page = 1
    while True:
        j = fetch_json(DC_WEB_URL, params={
            "reportName": "RPT_SHAREBONUS_DET",
            "columns": "SECURITY_CODE,EX_DIVIDEND_DATE,PRETAX_BONUS_RMB",
            "filter": "(EX_DIVIDEND_DATE>='%s')(EX_DIVIDEND_DATE<='%s')"
                      % (start_date, end_date),
            "pageSize": str(page_size), "pageNumber": str(page),
            "sortColumns": "EX_DIVIDEND_DATE", "sortTypes": "1",
            "source": "WEB", "client": "WEB"},
            referer="https://data.eastmoney.com/", retries=4, delay=1.0)
        res = j.get("result") or {}
        data = res.get("data") or []
        for r in data:
            v = r.get("PRETAX_BONUS_RMB")
            if v is None:
                continue
            per_share[r["SECURITY_CODE"]] = (
                per_share.get(r["SECURITY_CODE"], 0.0) + float(v) / 10.0
            )
        pages = res.get("pages") or 0
        if page >= pages or not data:
            break
        page += 1
    return per_share


def fetch_dividends_by_year(start_date, end_date, page_size=500):
    """区间内已实施分红 -> {code: {年份: 每股税前现金分红合计}}。"""
    out = {}
    page = 1
    while True:
        j = fetch_json(DC_WEB_URL, params={
            "reportName": "RPT_SHAREBONUS_DET",
            "columns": "SECURITY_CODE,EX_DIVIDEND_DATE,PRETAX_BONUS_RMB",
            "filter": "(EX_DIVIDEND_DATE>='%s')(EX_DIVIDEND_DATE<='%s')"
                      % (start_date, end_date),
            "pageSize": str(page_size), "pageNumber": str(page),
            "sortColumns": "EX_DIVIDEND_DATE", "sortTypes": "1",
            "source": "WEB", "client": "WEB"},
            referer="https://data.eastmoney.com/", retries=4, delay=1.0)
        res = j.get("result") or {}
        data = res.get("data") or []
        for r in data:
            code = r.get("SECURITY_CODE")
            v = r.get("PRETAX_BONUS_RMB")
            dt = (r.get("EX_DIVIDEND_DATE") or "")[:10]
            if not code or v is None or not dt:
                continue
            year = int(dt[:4])
            per = out.setdefault(code, {})
            per[year] = per.get(year, 0.0) + float(v) / 10.0
        pages = res.get("pages") or 0
        if page >= pages or not data:
            break
        page += 1
    return out


def _norm_date(s):
    s = str(s)
    if len(s) == 8:
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:])
    return s


def _fetch_kline_tencent(code, beg, end):
    """腾讯前复权日K线（分两段拉取，规避单次条数上限）。"""
    if code.startswith(("6", "9", "5")):
        mkt = "sh"
    elif code.startswith(("8", "4", "92")):
        mkt = "bj"
    else:
        mkt = "sz"
    sym = mkt + code
    bars = []
    for rng in [(beg, "2023-06-30"), ("2023-07-01", end)]:
        try:
            j = fetch_json(
                "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                params={"param": "%s,day,%s,%s,1000,qfq"
                        % (sym, _norm_date(rng[0]), _norm_date(rng[1]))},
                referer="https://gu.qq.com/", timeout=15,
                retries=2, delay=1.0)
            d = (j.get("data") or {}).get(sym) or {}
            rows = d.get("qfqday") or d.get("day") or []
            for row in rows:
                if len(row) < 6:
                    continue
                try:
                    bars.append({
                        "d": row[0], "o": float(row[1]), "c": float(row[2]),
                        "h": float(row[3]), "l": float(row[4]),
                        "v": float(row[5])})
                except ValueError:
                    continue
        except Exception:  # noqa: BLE001
            continue
    seen, out = set(), []
    for b in bars:
        if b["d"] not in seen:
            seen.add(b["d"])
            out.append(b)
    out.sort(key=lambda b: b["d"])
    return out


_em_lock = threading.Lock()
_em_fail_streak = 0


def _em_blocked():
    with _em_lock:
        return _em_fail_streak >= 2


def _em_note(ok):
    global _em_fail_streak
    with _em_lock:
        _em_fail_streak = _em_fail_streak + 1 if not ok else 0


def fetch_kline(code, beg="20200101", end="20500101"):
    """前复权日K线 -> (name, [dict(d,o,h,l,c,v), ...])。
    优先东方财富，连续失败自动切换腾讯备用源。"""
    if not _em_blocked():
        name, bars = _fetch_kline_em(code, beg, end)
        if bars:
            _em_note(True)
            return name, bars
        _em_note(False)
    return "", _fetch_kline_tencent(code, beg, end)


def _fetch_kline_em(code, beg, end):
    markets = [1, 0] if code.startswith(("6", "9", "5")) else [0, 1]
    for market in markets:
        try:
            j = fetch_json(KLINE_URL, params={
                "secid": "%d.%s" % (market, code),
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                "klt": "101", "fqt": "1", "beg": beg, "end": end},
                referer="https://quote.eastmoney.com/",
                retries=3, delay=1.0, timeout=15)
            d = j.get("data") or {}
            klines = d.get("klines") or []
            if klines:
                bars = []
                for line in klines:
                    p = line.split(",")
                    if len(p) < 7:
                        continue
                    bars.append({"d": p[0], "o": float(p[1]), "c": float(p[2]),
                                 "h": float(p[3]), "l": float(p[4]),
                                 "v": float(p[5])})
                return d.get("name") or "", bars
        except Exception:  # noqa: BLE001
            continue
    return "", []


def fetch_industry_boards():
    """行业板块列表（含总市值、涨跌幅），按总市值降序。"""
    rows = []
    for pn in range(1, 10):
        j = fetch_json(CLIST_URL, params={
            "pn": str(pn), "pz": "100", "po": "1", "np": "1",
            "fltt": "2", "invt": "2", "fid": "f20", "fs": "m:90+t:2",
            "fields": "f2,f3,f12,f14,f20"},
            referer="https://quote.eastmoney.com/",
            retries=5, delay=2.0, timeout=25)
        diff = (j.get("data") or {}).get("diff") or []
        rows.extend(diff)
        if len(diff) < 100:
            break
    return rows


def fetch_board_constituents(bk_code, max_pages=5):
    """概念/行业板块成分股 -> [(code, name), ...]（失败返回空列表）。"""
    rows = []
    for pn in range(1, max_pages + 1):
        try:
            j = fetch_json(CLIST_URL, params={
                "pn": str(pn), "pz": "100", "po": "1", "np": "1",
                "fltt": "2", "invt": "2", "fid": "f3", "fs": "b:" + bk_code,
                "fields": "f12,f14"},
                referer="https://quote.eastmoney.com/",
                retries=4, delay=2.0, timeout=25)
        except Exception:  # noqa: BLE001
            break
        diff = (j.get("data") or {}).get("diff") or []
        for d in diff:
            if d.get("f12"):
                rows.append((str(d["f12"]), d.get("f14") or ""))
        if len(diff) < 100:
            break
    return rows


def fetch_sina_index(indexid):
    """新浪指数最新成分股 -> [(code, name), ...]。"""
    rows = []
    seen = set()
    page = 1
    while page <= 10:
        params = {"page": str(page)} if page > 1 else None
        text = fetch(SINA_INDEX_URL % indexid, params=params,
                     referer="http://finance.sina.com.cn/",
                     timeout=25, retries=4, delay=1.0)
        found = re.findall(
            r'<div align="center">(\d{6})</div></td>\s*'
            r'<td><div align="center"><a href="[^"]*"[^>]*>([^<]+)</a></div></td>',
            text)
        new = [(c, n) for c, n in found if c not in seen]
        if not new:
            break
        for c, n in new:
            seen.add(c)
            rows.append((c, n))
        page += 1
    return rows

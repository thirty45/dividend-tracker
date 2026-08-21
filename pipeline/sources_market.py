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
# 新浪历史K线接口：scale=86400（秒）= 年K线，返回上市以来每年末不复权收盘价。
# 经验证与东财不复权日K线年末收盘价逐项一致，可用于补齐早年（东财K线仅到2019/2021）。
SINA_KLINE_URL = ("https://money.finance.sina.com.cn/quotes_service/"
                  "api/json_v2.php/CN_MarketData.getKLineData")


def fetch_sina_yearly_close(code):
    """新浪年K线 -> {年份(4位): 年末不复权收盘价}，覆盖上市以来全历史。

    数据源：新浪 CN_MarketData.getKLineData，scale=86400 表示年线。
    返回 dict（含当年至今的"年末"为最新交易日收盘），失败返回空 dict。
    """
    if code.startswith(("6", "9", "5")):
        mkt = "sh"
    else:
        mkt = "sz"
    sym = mkt + code
    try:
        j = fetch_json(
            SINA_KLINE_URL,
            params={"symbol": sym, "scale": "86400", "ma": "no",
                    "datalen": "1023"},
            referer="https://finance.sina.com.cn/",
            timeout=20, retries=3, delay=1.0)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(j, list):
        return {}
    out = {}
    for r in j:
        day = (r.get("day") or "")[:4]
        try:
            close = float(r.get("close"))
        except (TypeError, ValueError):
            continue
        if len(day) == 4 and close > 0:
            out[day] = close
    return out


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


def fetch_annual_profit(year, page_size=500):
    """某完整年度归母净利润 -> {code: PARENT_NETPROFIT}（亏损为负数）。

    数据源：东方财富业绩报表 RPT_LICO_FN_CPD，REPORTDATE 取当年 12-31。
    """
    out = {}
    page = 1
    while True:
        j = fetch_json(DC_WEB_URL, params={
            "reportName": "RPT_LICO_FN_CPD",
            "columns": "SECURITY_CODE,PARENT_NETPROFIT",
            "filter": "(REPORTDATE='%d-12-31')" % year,
            "pageSize": str(page_size), "pageNumber": str(page),
            "sortColumns": "SECURITY_CODE", "sortTypes": "1",
            "source": "WEB", "client": "WEB"},
            referer="https://data.eastmoney.com/", retries=4, delay=1.0)
        res = j.get("result") or {}
        data = res.get("data") or []
        for r in data:
            code = r.get("SECURITY_CODE")
            v = r.get("PARENT_NETPROFIT")
            if code and v is not None:
                out[code] = v
        pages = res.get("pages") or 0
        if page >= pages or not data:
            break
        page += 1
    return out


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


def probe_latest_bar_date(code):
    """腾讯快速探针：返回某只股票最近一根日K线的日期（YYYY-MM-DD）。

    用于判断“K线是否已更新到新的交易日”。东财估值数据可能晚于K线发布，
    因此以K线探针为准决定是否刷新K线文件。
    """
    if code.startswith(("6", "9", "5")):
        mkt = "sh"
    elif code.startswith(("8", "4", "92")):
        mkt = "bj"
    else:
        mkt = "sz"
    sym = mkt + code
    try:
        j = fetch_json(
            "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": "%s,day,,,6,qfq" % sym},
            referer="https://gu.qq.com/", timeout=15, retries=2, delay=1.0)
        d = (j.get("data") or {}).get(sym) or {}
        rows = d.get("qfqday") or d.get("day") or []
        if rows:
            return str(rows[-1][0])[:10]
    except Exception:  # noqa: BLE001
        pass
    return None


def fetch_dividend_details(start_date, end_date, page_size=500):
    """区间内已实施分红明细 -> {code: [{ex_date, record_date, report_date,
    profile, cash_per10}]}。用于K线图上标注除权除息日（S标志）。"""
    out = {}
    page = 1
    while True:
        j = fetch_json(DC_WEB_URL, params={
            "reportName": "RPT_SHAREBONUS_DET",
            "columns": ("SECURITY_CODE,EX_DIVIDEND_DATE,EQUITY_RECORD_DATE,"
                        "REPORT_DATE,IMPL_PLAN_PROFILE,PRETAX_BONUS_RMB"),
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
            ex = (r.get("EX_DIVIDEND_DATE") or "")[:10]
            if not code or not ex:
                continue
            out.setdefault(code, []).append({
                "ex_date": ex,
                "record_date": (r.get("EQUITY_RECORD_DATE") or "")[:10],
                "report_date": (r.get("REPORT_DATE") or "")[:10],
                "profile": r.get("IMPL_PLAN_PROFILE") or "",
                "cash_per10": r.get("PRETAX_BONUS_RMB"),
            })
        pages = res.get("pages") or 0
        if page >= pages or not data:
            break
        page += 1
    return out


def fetch_insider_changes(start_date, direction=1, page_size=500):
    """近一年高管增减持（二级市场买卖类）-> [{code,name,date,holder,position,
    shares,ratio,reason}]，按日期降序。

    数据源：东财数据中心「高管持股变动」RPT_EXECUTIVE_HOLD_CHANGE。
    direction=1 增持（CHANGE_NUM>0），direction=-1 减持（CHANGE_NUM<0）。
    采用“黑名单”排除非市场交易（回购注销/首发上市/股权激励/分红送转/增发上市），
    其余如 竞价交易/集中竞价/二级市场买卖/个人原因减持/大宗交易 等均计入。
    """
    exclude_reasons = ("回购注销", "首发上市", "股权激励实施", "分红送转", "增发上市")
    out = []
    page = 1
    while True:
        sign = ">0" if direction >= 0 else "<0"
        j = fetch_json(DC_WEB_URL, params={
            "reportName": "RPT_EXECUTIVE_HOLD_CHANGE",
            "columns": ("SECURITY_CODE,SECURITY_NAME_ABBR,CHANGE_DATE,"
                        "HOLDER_NAME,EXECUTIVE_NAME,POSITION,CHANGE_NUM,"
                        "CHANGE_RATIO,CHANGE_REASON"),
            "filter": "(CHANGE_DATE>='%s')(CHANGE_NUM%s)" % (start_date, sign),
            "pageSize": str(page_size), "pageNumber": str(page),
            "sortColumns": "CHANGE_DATE", "sortTypes": "-1",
            "source": "WEB", "client": "WEB"},
            referer="https://data.eastmoney.com/", retries=4, delay=1.0)
        res = j.get("result") or {}
        data = res.get("data") or []
        for r in data:
            reason = r.get("CHANGE_REASON") or ""
            if reason in exclude_reasons:
                continue
            out.append({
                "code": r.get("SECURITY_CODE"),
                "name": r.get("SECURITY_NAME_ABBR") or "",
                "date": (r.get("CHANGE_DATE") or "")[:10],
                "holder": r.get("EXECUTIVE_NAME") or r.get("HOLDER_NAME") or "",
                "position": r.get("POSITION") or "",
                "shares": r.get("CHANGE_NUM"),
                "ratio": r.get("CHANGE_RATIO"),
                "reason": reason,
            })
        pages = res.get("pages") or 0
        if page >= pages or not data:
            break
        page += 1
    return out


def fetch_fund_nav(code):
    """天天基金历史净值接口 -> {date, nav, chg}（最新一条，payload 很小）。

    用于每日轻量刷新基金“当天涨跌幅”，无需重新下载 pingzhongdata 大文件。
    """
    try:
        j = fetch_json(
            "https://api.fund.eastmoney.com/f10/lsjz",
            params={"fundCode": code, "pageIndex": "1", "pageSize": "2",
                    "startDate": "", "endDate": ""},
            referer="https://fundf10.eastmoney.com/",
            timeout=15, retries=3, delay=0.5)
        lst = ((j.get("Data") or {}).get("LSJZList")) or []
        for row in lst:
            chg = row.get("JZZZL")
            return {
                "date": row.get("FSRQ") or "",
                "nav": row.get("DWJZ"),
                "chg": float(chg) if chg not in (None, "") else None,
            }
    except Exception:  # noqa: BLE001
        pass
    return None


def _norm_date(s):
    s = str(s)
    if len(s) == 8:
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:])
    return s


def _fetch_kline_tencent(code, beg, end):
    """腾讯前复权日K线。

    分段策略（每只2次请求，避免触发限流）：
      1) 历史段：beg ~ 2023-12-31（接口单次最多约640根，自动截取最近640根）；
      2) 最近段：count 接口返回最近约640根且包含最新交易日（区间接口会滞后1天，
         因此最新交易日必须靠 count 接口补）。
    两段按日期合并，最近段优先（同日期以后者为准）。
    """
    if code.startswith(("6", "9", "5")):
        mkt = "sh"
    elif code.startswith(("8", "4", "92")):
        mkt = "bj"
    else:
        mkt = "sz"
    sym = mkt + code
    bars = []
    try:
        j = fetch_json(
            "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": "%s,day,%s,2023-12-31,1000,qfq"
                    % (sym, _norm_date(beg))},
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
        pass
    # 最近段：count 接口返回最近约640根且含最新交易日
    try:
        j2 = fetch_json(
            "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": "%s,day,,,700,qfq" % sym},
            referer="https://gu.qq.com/", timeout=15,
            retries=2, delay=1.0)
        d2 = (j2.get("data") or {}).get(sym) or {}
        rows2 = d2.get("qfqday") or d2.get("day") or []
        for row in rows2:
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
        pass
    by_date = {}
    for b in bars:
        by_date[b["d"]] = b  # 同日期保留后出现（最近8根优先）
    return [by_date[d] for d in sorted(by_date)]


def _shift_date(s, days):
    """日期字符串(YYYY-MM-DD)平移 N 天（简单按日加减，仅用于抓取窗口，容错即可）。"""
    from datetime import datetime, timedelta
    try:
        dt = datetime.strptime(str(s), "%Y-%m-%d")
    except ValueError:
        return s
    return (dt + timedelta(days=days)).strftime("%Y-%m-%d")


def fetch_raw_ex_open(code, ex_date):
    """取某只股票指定「除权日」的不复权开盘价 —— 配送股折算现金分红用。

    注意：前复权K线会把除权日当天的价格也按「后续分红」调整（如 派能科技
    2024-06-21 真实开盘 40.80，前复权显示 40.24），因此这里必须用不复权数据。
    优先东财 push2his fqt=0 小窗口（带熔断），失败回退腾讯不复权 fqkline。"""
    beg = _shift_date(ex_date, -2)
    end = _shift_date(ex_date, 2)
    # 1) 东财不复权（熔断：连续失败后直接跳过，避免拖慢整体）
    if not _em_blocked():
        markets = [1, 0] if code.startswith(("6", "9", "5")) else [0, 1]
        for market in markets:
            try:
                j = fetch_json(KLINE_URL, params={
                    "secid": "%d.%s" % (market, code),
                    "fields1": "f1,f2,f3",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                    "klt": "101", "fqt": "0",
                    "beg": _norm_date(beg), "end": _norm_date(end)},
                    referer="https://quote.eastmoney.com/",
                    retries=2, delay=1.0, timeout=15)
                for k in ((j.get("data") or {}).get("klines") or []):
                    p = k.split(",")
                    if len(p) >= 2 and p[0] == ex_date:  # f51日期,f52开
                        try:
                            _em_note(True)
                            return float(p[1])
                        except ValueError:
                            _em_note(True)
                            return None
            except Exception:  # noqa: BLE001
                continue
        _em_note(False)
    # 2) 腾讯不复权 fqkline（fq 参数留空 = 不复权，返回 day 数组）
    mkt = "sh" if code.startswith(("6", "9", "5")) else "sz"
    try:
        j = fetch_json(
            "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": "%s,day,%s,%s,20,"
                    % (mkt + code, _norm_date(beg), _norm_date(end))},
            referer="https://gu.qq.com/", timeout=15,
            retries=2, delay=1.0)
        d = (j.get("data") or {}).get(mkt + code) or {}
        for row in (d.get("day") or []):
            if len(row) >= 2 and row[0] == ex_date:
                try:
                    return float(row[1])
                except ValueError:
                    return None
    except Exception:  # noqa: BLE001
        pass
    return None


_em_lock = threading.Lock()
_em_fail_streak = 0


def _em_blocked():
    with _em_lock:
        return _em_fail_streak >= 2


def _em_note(ok):
    global _em_fail_streak
    with _em_lock:
        _em_fail_streak = _em_fail_streak + 1 if not ok else 0


def fetch_kline(code, beg="20200101", end="20500101", min_date=None):
    """前复权日K线 -> (name, [dict(d,o,h,l,c,v), ...])。
    优先东方财富，连续失败自动切换腾讯备用源。
    min_date: 期望覆盖到的最新日期；若返回数据未覆盖则视为陈旧，切换到备用源。"""
    if not _em_blocked():
        name, bars = _fetch_kline_em(code, beg, end)
        if bars and (min_date is None or bars[-1]["d"] >= min_date):
            _em_note(True)
            return name, bars
        if bars:
            _em_note(False)
    tbars = _fetch_kline_tencent(code, beg, end)
    if min_date is not None and tbars and tbars[-1]["d"] < min_date:
        print("   警告: %s K线最新仅到 %s（期望 %s）"
              % (code, tbars[-1]["d"], min_date), flush=True)
    return "", tbars


def _fetch_kline_tencent_raw(code, beg, end):
    """腾讯不复权日K线（fq 参数留空 -> day 数组，分历史段+最近段两段）。"""
    if code.startswith(("6", "9", "5")):
        mkt = "sh"
    elif code.startswith(("8", "4", "92")):
        mkt = "bj"
    else:
        mkt = "sz"
    sym = mkt + code
    bars = []
    try:
        j = fetch_json(
            "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": "%s,day,%s,2023-12-31,1000,"
                    % (sym, _norm_date(beg))},
            referer="https://gu.qq.com/", timeout=15,
            retries=2, delay=1.0)
        rows = ((j.get("data") or {}).get(sym) or {}).get("day") or []
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
        pass
    try:
        j2 = fetch_json(
            "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": "%s,day,,,700," % sym},
            referer="https://gu.qq.com/", timeout=15,
            retries=2, delay=1.0)
        rows2 = ((j2.get("data") or {}).get(sym) or {}).get("day") or []
        for row in rows2:
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
        pass
    by_date = {}
    for b in bars:
        by_date[b["d"]] = b
    return [by_date[d] for d in sorted(by_date)]


def fetch_kline_raw(code, beg="20200101", end="20500101", min_date=None):
    """不复权日K线 -> bars。优先东方财富 fqt=0，连续失败回退腾讯 day。"""
    if not _em_blocked():
        markets = [1, 0] if code.startswith(("6", "9", "5")) else [0, 1]
        for market in markets:
            try:
                j = fetch_json(KLINE_URL, params={
                    "secid": "%d.%s" % (market, code),
                    "fields1": "f1,f2,f3,f4,f5,f6",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                    "klt": "101", "fqt": "0", "beg": beg, "end": end},
                    referer="https://quote.eastmoney.com/",
                    retries=3, delay=1.0, timeout=15)
                klines = (j.get("data") or {}).get("klines") or []
                if klines:
                    bars = []
                    for line in klines:
                        p = line.split(",")
                        if len(p) < 7:
                            continue
                        bars.append({"d": p[0], "o": float(p[1]),
                                     "c": float(p[2]), "h": float(p[3]),
                                     "l": float(p[4]), "v": float(p[5])})
                    if min_date is None or bars[-1]["d"] >= min_date:
                        _em_note(True)
                        return bars
                    _em_note(False)
            except Exception:  # noqa: BLE001
                continue
    tbars = _fetch_kline_tencent_raw(code, beg, end)
    if min_date is not None and tbars and tbars[-1]["d"] < min_date:
        print("   警告: %s 不复权K线最新仅到 %s（期望 %s）"
              % (code, tbars[-1]["d"], min_date), flush=True)
    return tbars


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

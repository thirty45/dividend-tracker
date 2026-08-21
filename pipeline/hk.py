# -*- coding: utf-8 -*-
"""港股通标的每日数据生成：成分/行情/K线/财务指标/分红 -> data/hk/*。

数据源（均无需登录）：
  1) 东财行情 clist（港股通成分 b:DLMK0146,b:DLMK0144，约 620 只）
     字段：现价/涨跌幅/市值/PE_TTM/PB/ROE/行业/上市日期 等
  2) 腾讯港股日K（前复权 + 未复权），历史段+近段两段拼接
  3) 东财港股F10 主要指标 RPT_HKF10_FN_MAININDICATOR
     （ROE/EPS/BPS/PE/PB/股息率/每股派息/派息比率/营收/净利等，年报+季报）

输出：
  data/hk/snapshot.json           列表（口径同 A 股 snapshot）
  data/hk/company/<code>.json     个股财务指标 + 年度分红
  data/hk/kline/<code>.json       前复权日K
  data/hk/kline_raw/<code>.json   未复权日K
"""

import datetime
import json
import math
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from pipeline.net import fetch, fetch_json  # noqa: E402

CLIST_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
KLINE_URL = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
FIN_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
FS = "b:DLMK0146,b:DLMK0144"          # 港股通(沪)+港股通(深) 成分
UT = "bd1d9ddb04089700cf9c27f6f7426281"
CLIST_FIELDS = ("f2,f3,f5,f6,f8,f9,f12,f14,f15,f16,f17,f18,f20,f21,f23,"
                "f25,f26,f37,f100,f115")
KLINE_START = "20210101"              # 与 A 股模块一致：约 5 年


def _norm_date(s):
    """'YYYYMMDD' -> 'YYYY-MM-DD'；已是横线格式则原样返回。"""
    s = str(s)
    if len(s) == 8 and s.isdigit():
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:8])
    return s


def fetch_components():
    """港股通成分行情 -> {code: row}（约 620 只）。"""
    rows = {}
    pn = 1
    while pn <= 20:
        url = CLIST_URL + "?" + urllib.parse.urlencode({
            "pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "ut": UT, "fid": "f12", "fs": FS, "fields": CLIST_FIELDS,
        })
        try:
            j = fetch_json(url, referer="https://quote.eastmoney.com/",
                           timeout=20, retries=3, delay=1.0)
        except Exception:  # noqa: BLE001
            break
        d = (j.get("data") or {}).get("diff") or []
        if not d:
            break
        for r in d:
            code = r.get("f12")
            if code:
                rows[code] = r
        if len(d) < 100:
            break
        pn += 1
    return rows


def _num(v, div=1):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f / div if div and div != 1 else f


def fetch_hk_kline(code, beg=KLINE_START, end=None, qfq=True):
    """腾讯港股日K -> bars [{d,o,c,h,l,v}]；失败返回 []。"""
    if end is None:
        end = datetime.date.today().strftime("%Y%m%d")
    sym = "hk" + code
    beg_d = _norm_date(beg)
    end_d = _norm_date(end)
    adj = "qfq" if qfq else ""
    bars = []
    # 历史段（beg -> 2023-12-31，最多 1000 根）
    try:
        j = fetch_json(KLINE_URL, params={
            "param": "%s,day,%s,2023-12-31,1000,%s" % (sym, beg_d, adj)},
            referer="https://gu.qq.com/", timeout=15, retries=2, delay=1.0)
        for row in ((j.get("data") or {}).get(sym) or {}).get("day") or []:
            if len(row) < 6:
                continue
            try:
                bars.append({"d": row[0], "o": float(row[1]), "c": float(row[2]),
                             "h": float(row[3]), "l": float(row[4]),
                             "v": float(row[5])})
            except ValueError:
                continue
    except Exception:  # noqa: BLE001
        pass
    # 近段（最近 700 根，覆盖到今天）
    try:
        j2 = fetch_json(KLINE_URL, params={
            "param": "%s,day,,,700,%s" % (sym, adj)},
            referer="https://gu.qq.com/", timeout=15, retries=2, delay=1.0)
        for row in ((j2.get("data") or {}).get(sym) or {}).get("day") or []:
            if len(row) < 6:
                continue
            try:
                bars.append({"d": row[0], "o": float(row[1]), "c": float(row[2]),
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


def fetch_financial(codes, page_size=500):
    """按 in-filter 批量拉 RPT_HKF10_FN_MAININDICATOR。
    返回 {code: [row, ...]}（行按报告期倒序）。"""
    out = {c: [] for c in codes}
    codes = sorted(codes)
    batch = 50
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        flt = '(SECUCODE in (%s))' % ",".join('"%s.HK"' % c for c in chunk)
        pn = 1
        while pn <= 8:
            url = FIN_URL + "?" + urllib.parse.urlencode({
                "reportName": "RPT_HKF10_FN_MAININDICATOR",
                "columns": "ALL", "quoteColumns": "",
                "pageNumber": pn, "pageSize": page_size,
                "sortTypes": "-1", "sortColumns": "STD_REPORT_DATE",
                "source": "F10", "client": "PC", "filter": flt,
            })
            try:
                j = fetch_json(url, referer="https://emweb.securities.eastmoney.com/",
                               timeout=25, retries=3, delay=1.0)
            except Exception:  # noqa: BLE001
                break
            rows = ((j.get("result") or {}).get("data")) or []
            if not rows:
                break
            for r in rows:
                c = (r.get("SECURITY_CODE") or "").strip()
                if c in out:
                    out[c].append(r)
            if len(rows) < page_size:
                break
            pn += 1
        if (i // batch + 1) % 5 == 0:
            print("   财务进度 %d/%d..." % (min(i + batch, len(codes)), len(codes)),
                  flush=True)
    return out


def _parse_dps(plan):
    """从分红方案文本解析每股派息（港元）。优先港币口径，人民币兜底。"""
    if not plan:
        return None
    pats = [
        r"相当于每股派([\d.]+)港元",
        r"相当于每股派港币([\d.]+)元",
        r"相当于港币([\d.]+)元",
        r"相当于港元([\d.]+)",
        r"每股派港币([\d.]+)元",
        r"每股派([\d.]+)港元",
        r"每股派港币([\d.]+)",
        r"每股派([\d.]+)港仙",
        r"每10股派港币([\d.]+)元",
    ]
    for p in pats:
        m = re.search(p, plan)
        if m:
            try:
                v = float(m.group(1))
                return v / 100.0 if "港仙" in p else v
            except ValueError:
                return None
    m = re.search(r"每股派人民币([\d.]+)元", plan)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def fetch_dividends(codes):
    """批量拉 RPT_HKF10_MAIN_DIVBASIC 港股分红明细。
    返回 {code: [row, ...]}（按除净日倒序）。"""
    out = {c: [] for c in codes}
    codes = sorted(codes)
    batch = 50
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        flt = '(SECURITY_CODE in (%s))(IS_BFP="0")' % ",".join(
            '"%s"' % c for c in chunk)
        pn = 1
        while pn <= 8:
            url = FIN_URL + "?" + urllib.parse.urlencode({
                "reportName": "RPT_HKF10_MAIN_DIVBASIC",
                "columns": "ALL", "quoteColumns": "",
                "pageNumber": pn, "pageSize": 500,
                "sortTypes": "-1,-1", "sortColumns": "NOTICE_DATE,EX_DIVIDEND_DATE",
                "source": "F10", "client": "PC", "filter": flt,
            })
            try:
                j = fetch_json(url, referer="https://emweb.securities.eastmoney.com/",
                               timeout=25, retries=3, delay=1.0)
            except Exception:  # noqa: BLE001
                break
            rows = ((j.get("result") or {}).get("data")) or []
            if not rows:
                break
            for r in rows:
                c = (r.get("SECURITY_CODE") or "").strip()
                if c in out:
                    out[c].append(r)
            if len(rows) < 500:
                break
            pn += 1
    return out


def _pct_from_bars(bars, n):
    if not bars or len(bars) <= n:
        return None
    last, prev = bars[-1].get("c"), bars[-1 - n].get("c")
    if not last or not prev:
        return None
    return round((last - prev) / prev * 100.0, 2)


def _streak(bars, n, mode):
    """mode: down(收跌)/yin(收阴)/yang(收阳)。数据不足返回 False。"""
    if not bars or len(bars) < n + (0 if mode != "down" else 1):
        return False
    if mode == "down":
        seg = bars[-(n + 1):]
        return all(seg[i]["c"] < seg[i - 1]["c"] for i in range(1, len(seg)))
    seg = bars[-n:]
    if mode == "yin":
        return all(b["c"] < b["o"] for b in seg)
    return all(b["c"] > b["o"] for b in seg)


def _boll_lower(bars, n=20, k=2.0):
    closes = [b.get("c") for b in bars]
    out = [None] * len(bars)
    for i in range(len(bars)):
        if i + 1 < n:
            continue
        win = closes[i - n + 1:i + 1]
        if any(c is None for c in win):
            continue
        m = sum(win) / n
        var = sum((x - m) ** 2 for x in win) / n
        out[i] = m - k * (var ** 0.5)
    return out


def _streak_below_boll(bars, n=5, period=20, k=2.0):
    if not bars or len(bars) < period + n - 1:
        return False
    lowers = _boll_lower(bars, period, k)
    return all(lowers[i] is not None and bars[i]["c"] < lowers[i]
               for i in range(len(bars) - n, len(bars)))


def _fin_vals(row):
    """把一条主要指标行转成常用字段 dict。"""
    return {
        "date": (row.get("STD_REPORT_DATE") or row.get("REPORT_DATE") or "")[:10],
        "rtype": row.get("REPORT_TYPE") or "",
        "roe": _num(row.get("ROE_AVG")),
        "eps": _num(row.get("BASIC_EPS")),
        "eps_ttm": _num(row.get("EPS_TTM")),
        "bps": _num(row.get("BPS")),
        "pe": _num(row.get("PE_TTM")),
        "pb": _num(row.get("PB_TTM")),
        "revenue": _num(row.get("OPERATE_INCOME"), 1e8),
        "profit": _num(row.get("HOLDER_PROFIT"), 1e8),
        "gross_margin": _num(row.get("GROSS_PROFIT_RATIO")),
        "net_margin": _num(row.get("NET_PROFIT_RATIO")),
        "debt_ratio": _num(row.get("DEBT_ASSET_RATIO")),
        "roa": _num(row.get("ROA")),
        "roic": _num(row.get("ROIC_YEARLY")),
        "dps": _num(row.get("DPS_HKD")),
        "div_rate": _num(row.get("DIVIDEND_RATE")),
        "div_ratio": _num(row.get("DIVI_RATIO")),
    }


def build():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    today = datetime.date.today().isoformat()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    data_root = os.path.join(BASE, "data", "hk")
    kdir = os.path.join(data_root, "kline")
    rdir = os.path.join(data_root, "kline_raw")
    cdir = os.path.join(data_root, "company")
    for d in (kdir, rdir, cdir):
        os.makedirs(d, exist_ok=True)

    print("1/4 抓取港股通成分行情 ...", flush=True)
    comps = fetch_components()
    print("   港股通 %d 只" % len(comps), flush=True)
    if not comps:
        print("成分抓取失败，退出")
        return 1

    print("2/4 抓取K线(并发) ...", flush=True)
    codes = sorted(comps.keys())
    klines = {}
    raws = {}
    done = [0]

    def _pair(c):
        return fetch_hk_kline(c, qfq=True), fetch_hk_kline(c, qfq=False)

    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(_pair, c): c for c in codes}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                bars, rbars = fut.result()
                klines[c], raws[c] = bars, rbars
            except Exception:  # noqa: BLE001
                klines[c], raws[c] = [], []
            done[0] += 1
            if done[0] % 100 == 0:
                print("   K线进度 %d/%d" % (done[0], len(codes)), flush=True)
    # 边抓边写
    with ThreadPoolExecutor(max_workers=16) as ex:
        def _write(c):
            with open(os.path.join(kdir, c + ".json"), "w", encoding="utf-8") as f:
                json.dump({"code": c, "name": comps[c].get("f14", ""),
                           "date": today, "updated": now, "bars": klines.get(c, [])},
                          f, ensure_ascii=False)
            with open(os.path.join(rdir, c + ".json"), "w", encoding="utf-8") as f:
                json.dump({"code": c, "name": comps[c].get("f14", ""),
                           "date": today, "updated": now, "bars": raws.get(c, [])},
                          f, ensure_ascii=False)
        list(ex.map(_write, codes))
    print("   K线完成 %d/%d" % (len(codes), len(codes)), flush=True)

    print("3/4 抓取港股F10主要指标(批量) ...", flush=True)
    fin = fetch_financial(codes)
    have_fin = sum(1 for c in codes if fin.get(c))
    print("   有财务数据 %d/%d" % (have_fin, len(codes)), flush=True)

    print("3.5/4 抓取港股分红明细(批量) ...", flush=True)
    divs = fetch_dividends(codes)
    have_div = sum(1 for c in codes if divs.get(c))
    print("   有分红记录 %d/%d" % (have_div, len(codes)), flush=True)

    print("4/4 组装列表与公司数据 ...", flush=True)
    today_d = datetime.date.today()
    ttm_start = today_d - datetime.timedelta(days=365)
    items = []
    for c in codes:
        q = comps[c]
        bars = klines.get(c) or []
        rows = fin.get(c) or []
        annual = [r for r in rows
                  if (r.get("DATE_TYPE_CODE") or "") == "001"]
        periods = [_fin_vals(r) for r in rows]
        latest_period = None
        if periods:
            latest_period = max(periods, key=lambda x: x["date"] or "")
        # 分红明细：近12个月已实施（除净日口径）合计每股派息 -> 股息率TTM
        records = []
        ttm_dps = 0.0
        for r in (divs.get(c) or []):
            exd = (r.get("EX_DIVIDEND_DATE") or "")[:10].replace("/", "-")
            if len(exd) != 10:
                continue
            try:
                exd_d = datetime.date.fromisoformat(exd)
            except ValueError:
                continue
            dps_v = _parse_dps(r.get("PLAN_EXPLAIN"))
            rec = {
                "year": r.get("YEAR"),
                "type": r.get("REPORT_TYPE") or "",
                "ex_date": exd,
                "pay_date": (r.get("DIVIDEND_DATE") or "")[:10].replace("/", "-"),
                "plan": r.get("PLAN_EXPLAIN") or "",
                "dps": round(dps_v, 4) if dps_v is not None else None,
            }
            records.append(rec)
            if dps_v is not None and exd_d >= ttm_start:
                ttm_dps += dps_v
        records.sort(key=lambda x: x["ex_date"] or "", reverse=True)
        close = _num(q.get("f2"))
        dy = round(ttm_dps / close * 100.0, 2) if (ttm_dps and close) else None
        dps = round(ttm_dps, 4) if ttm_dps else None
        eps_ttm = (latest_period or {}).get("eps_ttm")
        div_ratio = round(ttm_dps / eps_ttm * 100.0, 2) if (ttm_dps and eps_ttm) else None
        item = {
            "code": c,
            "name": q.get("f14") or c,
            "close": _num(q.get("f2")),
            "pct": _num(q.get("f3")),
            "pct_1m": _pct_from_bars(bars, 21),
            "pct_3m": _pct_from_bars(bars, 63),
            "pct_7d": _pct_from_bars(bars, 7),
            "down7": _streak(bars, 7, "down"),
            "yin7": _streak(bars, 7, "yin"),
            "yang7": _streak(bars, 7, "yang"),
            "boll5": _streak_below_boll(bars, 5),
            "dy": dy,
            "pe": _num(q.get("f115")),
            "pb": _num(q.get("f23")),
            "mv": round((_num(q.get("f20")) or 0) / 1e8, 2) if _num(q.get("f20")) else None,
            "roe": round((latest_period or {}).get("roe") or 0, 2)
            if (latest_period or {}).get("roe") is not None else None,
            "roe_date": (latest_period or {}).get("date") or "",
            "industry": q.get("f100") or "",
            "list_date": (q.get("f26") or ""),
            "ytd": _num(q.get("f25")),
            "dps": round(dps, 4) if dps is not None else None,
            "div_ratio": round(div_ratio, 2) if div_ratio is not None else None,
            "tags": [],
        }
        items.append(item)
        comp_file = {
            "code": c, "name": item["name"], "updated": now,
            "quote": {
                "close": item["close"], "pct": item["pct"], "mv": item["mv"],
                "pe": item["pe"], "pb": item["pb"], "industry": item["industry"],
                "list_date": item["list_date"], "ytd": item["ytd"],
            },
            "financial": {"periods": periods},
            "dividend": {
                "ttm_dps": dps, "ttm_div_ratio": div_ratio, "dy": dy,
                "records": records,
            },
        }
        with open(os.path.join(cdir, c + ".json"), "w", encoding="utf-8") as f:
            json.dump(comp_file, f, ensure_ascii=False)

    items.sort(key=lambda x: (x.get("dy") is None, -(x.get("dy") or 0)))
    snap = {"date": today, "updated_at": now, "total": len(items),
            "market": "港股通", "items": items}
    with open(os.path.join(data_root, "snapshot.json"), "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)
    print("已写 data/hk/snapshot.json | 港股通 %d 只" % len(items), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(build())

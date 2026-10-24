# -*- coding: utf-8 -*-
"""回测数据加载：watchlist、kline_raw、分红、基准指数。

kline_raw 不在 git，可从 Pages 批量下载到本地 data/kline_raw/。
基准优先腾讯日 K（东财 push2his 在部分环境 403）；H30269 可选，
东财不可用时尝试跟踪基金 005561 累计净值作代理并标注。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from pipeline.net import fetch_json

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PAGES_KLINE = "https://thirty45.github.io/dividend-tracker/data/kline_raw/{code}.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def load_watchlist_codes(path: Optional[str] = None) -> List[str]:
    path = path or os.path.join(BASE, "data", "meta", "watchlist.json")
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    stocks = payload.get("stocks") if isinstance(payload, dict) else payload
    codes = []
    for it in stocks or []:
        if isinstance(it, dict) and it.get("code"):
            codes.append(str(it["code"]))
        elif isinstance(it, str):
            codes.append(it)
    return codes


def load_bars(code: str, kline_dir: Optional[str] = None) -> List[dict]:
    kline_dir = kline_dir or os.path.join(BASE, "data", "kline_raw")
    path = os.path.join(kline_dir, "%s.json" % code)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return []
    bars = payload.get("bars") if isinstance(payload, dict) else payload
    return list(bars or []) if isinstance(bars, list) else []


def load_dividends(code: str, div_dir: Optional[str] = None) -> List[dict]:
    div_dir = div_dir or os.path.join(BASE, "data", "dividends")
    path = os.path.join(div_dir, "%s.json" % code)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return []
    return list(rows or []) if isinstance(rows, list) else []


def download_kline_raw(codes: Sequence[str], kline_dir: Optional[str] = None,
                       workers: int = 16, skip_existing: bool = True,
                       progress=None) -> Tuple[int, int]:
    """从 Pages 下载 kline_raw。返回 (ok, fail)。"""
    kline_dir = kline_dir or os.path.join(BASE, "data", "kline_raw")
    os.makedirs(kline_dir, exist_ok=True)
    todo = []
    for code in codes:
        path = os.path.join(kline_dir, "%s.json" % code)
        if skip_existing and os.path.isfile(path) and os.path.getsize(path) > 50:
            continue
        todo.append(code)

    ok = fail = 0
    done_existing = len(codes) - len(todo)

    def _one(code: str) -> bool:
        url = PAGES_KLINE.format(code=code)
        path = os.path.join(kline_dir, "%s.json" % code)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            if len(data) < 50:
                return False
            # 粗验 JSON
            json.loads(data.decode("utf-8"))
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                ValueError, UnicodeDecodeError):
            return False

    if not todo:
        if progress:
            progress("kline_raw 已齐 %d 只" % done_existing)
        return done_existing, 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_one, c): c for c in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            if fut.result():
                ok += 1
            else:
                fail += 1
            if progress and (i % 50 == 0 or i == len(todo)):
                progress("下载 kline_raw %d/%d (ok=%d fail=%d)" % (
                    i, len(todo), ok, fail))
    return done_existing + ok, fail


def _tencent_index_bars(symbol: str, beg: str, end: str) -> List[dict]:
    """腾讯日 K。symbol 如 sh000922。返回 [{d,o,c,h,l,v}, ...]。"""
    # 腾讯单次窗口有限，按年分段
    y0 = int(beg[:4])
    y1 = int(end[:4])
    out: Dict[str, dict] = {}
    for year in range(y0, y1 + 1):
        a = max(beg, "%04d-01-01" % year)
        b = min(end, "%04d-12-31" % year)
        # lmt 要足够大；腾讯按 param 的日期截
        param = "%s,day,%s,%s,640," % (symbol, a, b)
        try:
            j = fetch_json(
                "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                params={"param": param},
                referer="https://finance.qq.com/",
                timeout=20, retries=3, delay=0.8)
        except Exception:  # noqa: BLE001
            continue
        data = (j.get("data") or {}).get(symbol) or {}
        rows = data.get("day") or data.get("qfqday") or []
        for row in rows:
            if not row or len(row) < 5:
                continue
            d = str(row[0])[:10]
            if d < beg or d > end:
                continue
            try:
                o, c, h, l = float(row[1]), float(row[2]), float(row[3]), float(row[4])
                v = float(row[5]) if len(row) > 5 else 0.0
            except (TypeError, ValueError):
                continue
            out[d] = {"d": d, "o": o, "c": c, "h": h, "l": l, "v": v}
        time.sleep(0.15)
    return [out[k] for k in sorted(out.keys())]


def _eastmoney_index_bars(secid: str, beg: str, end: str) -> List[dict]:
    try:
        j = fetch_json(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                "klt": "101", "fqt": "1",
                "beg": beg.replace("-", ""),
                "end": end.replace("-", ""),
            },
            referer="https://quote.eastmoney.com/",
            timeout=20, retries=2, delay=1.0)
    except Exception:  # noqa: BLE001
        return []
    klines = ((j.get("data") or {}).get("klines")) or []
    out = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        try:
            out.append({
                "d": parts[0][:10],
                "o": float(parts[1]),
                "c": float(parts[2]),
                "h": float(parts[3]),
                "l": float(parts[4]),
                "v": float(parts[5]),
            })
        except (TypeError, ValueError):
            continue
    return out


def _fund_nav_proxy(fund_code: str, beg: str, end: str) -> List[dict]:
    """用场外基金累计净值作指数代理：o=c=LJJZ。"""
    page, out = 1, {}
    while page < 200:
        try:
            j = fetch_json(
                "https://api.fund.eastmoney.com/f10/lsjz",
                params={
                    "fundCode": fund_code,
                    "pageIndex": str(page),
                    "pageSize": "20",  # 东财接口实际上限约 20
                    "startDate": beg,
                    "endDate": end,
                },
                referer="https://fundf10.eastmoney.com/",
                timeout=20, retries=2, delay=0.5)
        except Exception:  # noqa: BLE001
            break
        rows = ((j.get("Data") or {}).get("LSJZList")) or []
        if not rows:
            break
        for r in rows:
            d = str(r.get("FSRQ") or "")[:10]
            try:
                nav = float(r.get("LJJZ") or r.get("DWJZ"))
            except (TypeError, ValueError):
                continue
            if not d or nav <= 0:
                continue
            if beg <= d <= end:
                out[d] = {"d": d, "o": nav, "c": nav, "h": nav, "l": nav, "v": 0.0}
        total = j.get("TotalCount") or 0
        if page * 20 >= total:
            break
        page += 1
        time.sleep(0.2)
    return [out[k] for k in sorted(out.keys())]


def load_or_fetch_benchmark(code: str, beg: str, end: str,
                            cache_dir: Optional[str] = None
                            ) -> Tuple[List[dict], dict]:
    """返回 (bars, meta)。

    meta 含 source / benchmark_type / role / notes。
    中证红利全收益官方代码 H00922；本环境东财/腾讯/新浪均不可用时
    主基准退回 000922 价格指数，并标记 excess_public=false。
    H30269 真源优先；005561 仅 proxy。
    """
    cache_dir = cache_dir or os.path.join(BASE, "data", "backtest", "cache")
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, "%s_%s_%s.json" % (code, beg, end))
    if os.path.isfile(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            payload = json.load(f)
        # 旧缓存可能缺类型字段，按代码补默认
        code_u = code.upper()
        default_type = {
            "H00922": "total_return", "H20269": "total_return",
            "000922": "price_index", "H30269": "price_index",
        }.get(code_u, "price_index")
        src = payload.get("source") or "cache"
        btype = payload.get("benchmark_type") or default_type
        if isinstance(src, str) and src.startswith("proxy:"):
            btype = "fund_nav_proxy"
        meta = {
            "source": src,
            "benchmark_type": btype,
            "role": payload.get("role"),
            "notes": payload.get("notes") or [],
            "excess_public": payload.get("excess_public", False),
        }
        return payload.get("bars") or [], meta

    bars: List[dict] = []
    source = ""
    benchmark_type = "price_index"
    role = "secondary"
    notes: List[str] = []
    excess_public = False

    code_u = code.upper()
    if code_u == "H00922":
        # 中证红利全收益 —— 官方 TR
        for secid in ("2.H00922", "1.H00922"):
            em = _eastmoney_index_bars(secid, beg, end)
            if len(em) > len(bars):
                bars, source = em, "eastmoney:%s" % secid
        if len(bars) < 100:
            notes.append(
                "H00922(中证红利全收益) 东财/腾讯/新浪在本环境不可用；"
                "已确认 csindex 基本信息存在（CSI Dividend TRI）")
            source = source or "unavailable:H00922"
        else:
            benchmark_type = "total_return"
            role = "primary_tr"
            excess_public = True
    elif code == "000922":
        bars = _tencent_index_bars("sh000922", beg, end)
        source = "tencent:sh000922"
        if len(bars) < 100:
            em = _eastmoney_index_bars("1.000922", beg, end)
            if len(em) > len(bars):
                bars, source = em, "eastmoney:1.000922"
        benchmark_type = "price_index"
        role = "primary_price"
        excess_public = False
        notes.append(
            "000922 为价格指数（不含分红再投资）；"
            "全收益官方代码 H00922 暂无行情源时不得把相对其超额对外展示")
    elif code_u == "H30269":
        bars = _eastmoney_index_bars("2.H30269", beg, end)
        source = "eastmoney:2.H30269"
        benchmark_type = "price_index"
        role = "optional"
        if len(bars) < 100:
            # 真源失败 → 代理，必须标注
            proxy = _fund_nav_proxy("005561", beg, end)
            if len(proxy) > len(bars):
                bars = proxy
                source = "proxy:fund_nav:005561"
                benchmark_type = "fund_nav_proxy"
                notes.append(
                    "H30269 真源不可用；005561 创金合信中证红利低波A 累计净值"
                    "仅为 proxy，≠ 指数本身；全收益对应 H20269 亦不可用")
            else:
                notes.append("H30269 真源与 005561 代理均失败")
        excess_public = False
    elif code_u == "H20269":
        for secid in ("2.H20269", "1.H20269"):
            em = _eastmoney_index_bars(secid, beg, end)
            if len(em) > len(bars):
                bars, source = em, "eastmoney:%s" % secid
        if len(bars) >= 100:
            benchmark_type = "total_return"
            role = "optional_tr"
            excess_public = True
        else:
            source = source or "unavailable:H20269"
            notes.append("H20269(红利低波全收益) 行情不可用")
        role = role if bars else "optional_tr"
    else:
        bars = _tencent_index_bars("sh" + code, beg, end)
        source = "tencent:sh" + code
        if len(bars) < 50:
            em = _eastmoney_index_bars("1." + code, beg, end)
            if len(em) > len(bars):
                bars, source = em, "eastmoney:1." + code

    meta = {
        "source": source,
        "benchmark_type": benchmark_type,
        "role": role,
        "notes": notes,
        "excess_public": excess_public,
    }
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({"code": code, "beg": beg, "end": end, "bars": bars, **meta},
                  f, ensure_ascii=False)
    return bars, meta


def try_fill_missing_klines(codes: Sequence[str], kline_dir: Optional[str] = None,
                            progress=None, workers: int = 6
                            ) -> Tuple[List[str], List[str], str]:
    """尝试从东财不复权日 K 补 Pages 缺失的代码。

    返回 (filled, still_missing, note)。窗口从 2010 起（够 MA60+回测）。
    """
    from pipeline import sources_market as sm

    kline_dir = kline_dir or os.path.join(BASE, "data", "kline_raw")
    os.makedirs(kline_dir, exist_ok=True)
    todo = []
    for code in codes:
        path = os.path.join(kline_dir, "%s.json" % code)
        if os.path.isfile(path) and os.path.getsize(path) > 50:
            continue
        todo.append(code)
    note = "Pages 404 后尝试 eastmoney fetch_kline_raw(fqt=0,beg=20100101)"
    filled, still = [], []

    def _one(code: str):
        path = os.path.join(kline_dir, "%s.json" % code)
        try:
            bars = sm.fetch_kline_raw(code, beg="20100101", end="20500101")
        except Exception:  # noqa: BLE001
            return code, False
        if not bars or len(bars) < 60:
            return code, False
        payload = {
            "code": code, "name": code, "bars": list(bars),
            "source": "eastmoney_raw_fill",
            "history_complete": False,
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
        return code, True

    if not todo:
        return [], [], note
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_one, c): c for c in todo}
        done = 0
        for fut in as_completed(futs):
            code, ok = fut.result()
            done += 1
            if ok:
                filled.append(code)
            else:
                still.append(code)
            if progress and (done % 10 == 0 or done == len(todo)):
                progress("补K %d/%d filled=%d" % (done, len(todo), len(filled)))
    return filled, still, note

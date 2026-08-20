# -*- coding: utf-8 -*-
"""每日更新脚本：估值/ROE/分红/K线/板块 -> 生成网页数据。

用法: python pipeline/update_daily.py
"""

import argparse
import datetime
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from pipeline import sources_market as sm  # noqa: E402
from pipeline import company as comp  # noqa: E402


def load_config():
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def load_watchlist():
    with open(os.path.join(BASE, "data", "meta", "watchlist.json"),
              encoding="utf-8") as f:
        return json.load(f)


def _pct_from_bars(bars, n):
    """最新收盘价相对 n 个交易日前收盘的涨跌幅(%)；数据不足返回 None。"""
    if not bars or len(bars) <= n:
        return None
    last = bars[-1].get("c")
    prev = bars[-1 - n].get("c")
    if not last or not prev:
        return None
    return round((last - prev) / prev * 100.0, 2)


def _month_end_series(bars):
    """由日 K 线构造月末收盘价序列（升序）。当前月用最新一根收盘价代表。
    返回 [(ym 'YYYY-MM', close), ...]。"""
    if not bars:
        return []
    ms = {}
    for b in bars:
        d = b.get("d") or b.get("date") or ""
        if len(d) < 7:
            continue
        ms[d[:7]] = b.get("c")  # 同月保留最后出现（已按日期升序）
    return [(ym, ms[ym]) for ym in sorted(ms.keys())]


def _streak_down(bars, n=7):
    """连续 n 个交易日收盘价逐日下跌（每日收盘 < 前一日收盘）。数据不足返回 False。"""
    if not bars or len(bars) < n + 1:
        return False
    seg = bars[-(n + 1):]
    return all(seg[i]["c"] < seg[i - 1]["c"] for i in range(1, len(seg)))


def _streak_yin(bars, n=7):
    """连续 n 个交易日收阴线（收盘价 < 开盘价）。数据不足返回 False。"""
    if not bars or len(bars) < n:
        return False
    return all(b["c"] < b["o"] for b in bars[-n:])


def _streak_yang(bars, n=7):
    """连续 n 个交易日收阳线（收盘价 > 开盘价，含当前日）。数据不足返回 False。"""
    if not bars or len(bars) < n:
        return False
    return all(b["c"] > b["o"] for b in bars[-n:])


def _boll_lower(bars, n=20, k=2.0):
    """布林带下轨序列（与 bars 等长）：n 日均线 - k×标准差（总体标准差）。
    数据不足 n 根的位置返回 None。"""
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
    """连续 n 个交易日收盘价低于布林带下轨。数据不足返回 False。"""
    if not bars or len(bars) < period + n - 1:
        return False
    lowers = _boll_lower(bars, period, k)
    return all(lowers[i] is not None and bars[i]["c"] < lowers[i]
               for i in range(len(bars) - n, len(bars)))


def enrich_items(items, klines, company_dir, cfg, raw_opens=None):
    """为每只股票计算派生字段并写回 item（就地修改）。

    派生：近1月/近3月涨跌幅、每股收益、每股分红、连续分红年数、是否国有、
    近5年平均市净率、近5年平均股息率、预期收益率①/②；并重写 tags。
    raw_opens: {code: {ex_date: 不复权开盘价}} —— 配送股折算现金分红用
    （前复权K线会把除权日价格按后续分红调整，必须用不复权价）。
    """
    today = datetime.date.today()
    raw_opens = raw_opens or {}
    # 近5年窗口：从「今往前5个自然年」的1月起（如 2026 年取 2021-01 起，约68个月）。
    five_start = "%d-01" % max(cfg.get("finance_start_year", 2016),
                               today.year - 5)
    for it in items:
        code = it["code"]
        close = it.get("close")
        bars = klines.get(code) or []
        it["pct_1m"] = _pct_from_bars(bars, 21)
        it["pct_3m"] = _pct_from_bars(bars, 63)
        # 近7日涨跌幅 + 连跌7天 + 连续7天阴线（首页排名框用）
        it["pct_7d"] = _pct_from_bars(bars, 7)
        it["down7"] = _streak_down(bars, 7)
        it["yin7"] = _streak_yin(bars, 7)
        it["yang7"] = _streak_yang(bars, 7)
        # 连续5天跌破布林带下轨（20日 ± 2σ）
        it["boll5"] = _streak_below_boll(bars, 5)

        comp = None
        cp = os.path.join(company_dir, code + ".json")
        if os.path.exists(cp):
            try:
                with open(cp, encoding="utf-8") as f:
                    comp = json.load(f)
            except Exception:  # noqa: BLE001
                comp = None
        holders = (comp or {}).get("holders", {}) or {}
        is_soe = bool(holders.get("is_state_owned"))
        it["is_soe"] = is_soe

        finance = (comp or {}).get("finance", {}) or {}
        periods = finance.get("periods", []) or []
        annual = [p for p in periods if p.get("type") == "annual"]
        eps = None
        bps_by_year = {}
        if annual:
            la = max(annual, key=lambda p: p.get("date", ""))
            eps = (la.get("vals") or {}).get("EPSJB")
            for p in annual:
                bps = (p.get("vals") or {}).get("BPS")
                if bps is not None:
                    bps_by_year[p.get("year")] = bps

        div = (comp or {}).get("dividend", {}) or {}
        years = div.get("years", []) or []
        # 配送股现金等价 = 除权后开盘价 × 每股配送股数（送股+转增）。
        # 开盘价必须用「不复权」数据（raw_opens）：前复权K线会把除权日价格
        # 按后续分红调整（如派能科技 2024-06-21 真实 40.80 vs 前复权 40.24）。
        div_changed = False
        raw = raw_opens.get(code) or {}
        for y in years:
            if (y.get("send_ratio") or 0) or (y.get("trans_ratio") or 0):
                ex_open = raw.get(y.get("ex_date")) if y.get("ex_date") else None
                y["ex_open"] = ex_open
                if ex_open:
                    extra = ex_open * ((y.get("send_ratio") or 0) + (y.get("trans_ratio") or 0))
                    y["per_share_real"] = round((y.get("per_share") or 0) + extra, 4)
                else:
                    y["per_share_real"] = y.get("per_share")
                div_changed = True
        if div_changed and comp:
            try:
                with open(cp, "w", encoding="utf-8") as f:
                    json.dump(comp, f, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                pass
        dps = None
        for y in sorted(years, key=lambda x: x.get("year", 0), reverse=True):
            v = y.get("per_share_real")
            if v is None:
                v = y.get("per_share")
            if v is not None:
                dps = v
                break
        yset = set()
        for y in years:
            if (y.get("total_div") or 0) > 0 or y.get("per_share") is not None:
                yset.add(y.get("year"))
        div_years = 0
        if yset:
            ymax = max(yset)
            while ymax in yset:
                div_years += 1
                ymax -= 1
        it["dps"] = dps
        it["div_years"] = div_years
        it["eps"] = eps

        # 月度序列 -> 近5年平均市净率 / 近5年平均股息率
        mes = _month_end_series(bars)
        dps_by_year = {y.get("year"): y.get("per_share") for y in years}
        pb_list, dy_list = [], []
        for ym, c in mes:
            if ym < five_start or not c:
                continue
            y = int(ym[:4])
            app_bps = None
            for by in sorted(bps_by_year.keys()):
                if by <= y:
                    app_bps = bps_by_year[by]
            if app_bps:
                pb_list.append(c / app_bps)
            d = dps_by_year.get(y)
            if d is not None:
                dy_list.append(d / c * 100.0)
        it["pb5"] = round(sum(pb_list) / len(pb_list), 2) if pb_list else None
        it["div_yield_5y"] = round(sum(dy_list) / len(dy_list), 2) if dy_list else None

        # 连续2年股息率 < 1% 判定（最近两个完整年度：当年-1、当年-2；年度股息率=每股分红/年末收盘价）
        year_close = {}
        for b in bars:
            d = b.get("d") or b.get("date") or ""
            if len(d) >= 4:
                year_close[d[:4]] = b.get("c")  # 同年度保留最后（年末收盘）
        dy_vals = []
        for yr in (today.year - 1, today.year - 2):
            c = year_close.get(str(yr))
            if not c:
                break
            d = dps_by_year.get(yr)
            dy_vals.append((d / c * 100.0) if d is not None else 0.0)
        it["dy_bad2"] = len(dy_vals) == 2 and dy_vals[0] < 1.0 and dy_vals[1] < 1.0

        dy = it.get("dy")
        pb = it.get("pb")
        if None not in (dy, pb, eps, dps, close) and close:
            it["exp_ret"] = round(dy / 100.0 + (eps - dps) * pb / close, 2)
            it["exp_ret_pb5"] = round(
                dy / 100.0 + (eps - dps) * (it["pb5"] or pb) / close, 2)
        else:
            it["exp_ret"] = None
            it["exp_ret_pb5"] = None

        tags = [t for t in (it.get("tags") or []) if t != "基金持仓"]
        if is_soe and "国有企业" not in tags:
            tags.append("国有企业")
        it["tags"] = tags
    return items


def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只更新前N只(测试用)")
    ap.add_argument("--force", action="store_true", help="忽略幂等检查强制更新")
    args = ap.parse_args()
    cfg = load_config()

    data_root = os.path.join(BASE, "data")
    meta_dir = os.path.join(data_root, "meta")
    kline_dir = os.path.join(data_root, "kline")
    raw_dir = os.path.join(data_root, "kline_raw")
    hist_dir = os.path.join(data_root, "history")
    latest_file = os.path.join(meta_dir, "latest.json")
    os.makedirs(meta_dir, exist_ok=True)
    os.makedirs(kline_dir, exist_ok=True)
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(hist_dir, exist_ok=True)

    watch = load_watchlist()
    stocks = watch["stocks"]
    if args.limit:
        stocks = stocks[:args.limit]
    codes = [s["code"] for s in stocks]
    name_map = {s["code"]: s["name"] for s in stocks}

    trade_date = sm.latest_trade_date()
    if not trade_date:
        print("无法获取最新交易日，退出")
        return 1
    # K线最新日期探针：估值数据中心可能晚于K线发布，K线可能已更新到当天
    probe_date = None
    if codes:
        try:
            probe_date = sm.probe_latest_bar_date(codes[0])
        except Exception as exc:  # noqa: BLE001
            print("K线探针失败(将按估值日期更新): %s" % exc)
    stored = {}
    if os.path.exists(latest_file):
        with open(latest_file, encoding="utf-8") as f:
            stored = json.load(f)
    kline_stale = bool(probe_date) and probe_date != (stored.get("kline_date") or "")
    snapshot_stale = trade_date != (stored.get("date") or "")
    print("估值交易日=%s 探针K线日=%s 已存(date=%s, kline=%s)"
          % (trade_date, probe_date, stored.get("date"), stored.get("kline_date")))
    if not args.force and not snapshot_stale and not kline_stale:
        kline_files = [f for f in os.listdir(kline_dir)
                       if f.endswith(".json")]
        if kline_files:
            print("数据已是最新(%s)，跳过" % trade_date)
            return 0
        print("日期一致但缺少K线文件，重新生成K线数据")
    if kline_stale and not snapshot_stale:
        print("K线已有新交易日(%s)，刷新K线；估值数据尚未更新则快照沿用 %s"
              % (probe_date, trade_date))
    kline_target = probe_date or trade_date
    print("股票池: %d 只（交易日 %s）" % (len(codes), trade_date))

    print("1/5 抓取全市场估值快照 ...")
    _, valuation = sm.fetch_valuation(trade_date)
    val = {v["SECURITY_CODE"]: v for v in valuation if v.get("SECURITY_CODE")}
    print("   估值记录 %d 条" % len(val))

    print("2/5 抓取最新报告期 ROE ...")
    roe_date, roe_map = sm.fetch_roe()
    print("   报告期 %s，%d 条" % (roe_date, len(roe_map)))

    print("3/5 抓取近12个月已实施分红 ...")
    end = datetime.date.today()
    start = end - datetime.timedelta(days=366)
    bonus = sm.fetch_sharebonus(start.isoformat(), end.isoformat())
    print("   %d 只股票有分红记录" % len(bonus))

    print("4/5 抓取K线(并发) ...", flush=True)
    klines = {}
    raw_klines = {}
    todo = []
    for c in codes:
        pa = os.path.join(kline_dir, c + ".json")
        pr = os.path.join(raw_dir, c + ".json")
        adj_ok = raw_ok = False
        if not args.force and os.path.exists(pa):
            try:
                with open(pa, encoding="utf-8") as f:
                    old = json.load(f)
                if old.get("bars") and old["bars"][-1].get("d", "") >= kline_target:
                    adj_ok = True
                    klines[c] = old["bars"]
            except Exception:  # noqa: BLE001
                pass
        if not args.force and os.path.exists(pr):
            try:
                with open(pr, encoding="utf-8") as f:
                    oldr = json.load(f)
                if oldr.get("bars") and oldr["bars"][-1].get("d", "") >= kline_target:
                    raw_ok = True
                    raw_klines[c] = oldr["bars"]
            except Exception:  # noqa: BLE001
                pass
        if not (adj_ok and raw_ok):
            todo.append(c)
    print("   待抓取K线 %d 只（前复权+未复权均最新 %d 只）"
          % (len(todo), len(codes) - len(todo)),
          flush=True)
    done_count = [0]

    def _fetch_pair(c):
        name, bars = sm.fetch_kline(c, cfg["kline_start"], "20500101",
                                    kline_target)
        rbars = sm.fetch_kline_raw(c, cfg["kline_start"], "20500101",
                                   kline_target)
        return c, name, bars, rbars

    with ThreadPoolExecutor(max_workers=cfg.get("workers", 6)) as ex:
        futs = {ex.submit(_fetch_pair, c): c
                for c in todo}
        now_iso = datetime.datetime.now().isoformat(timespec="seconds")
        for fut in as_completed(futs):
            try:
                c, name, bars, rbars = fut.result()
                klines[c] = bars
                raw_klines[c] = rbars
                if name:
                    name_map[c] = name
            except Exception as exc:  # noqa: BLE001
                c = futs[fut]
                print("   K线失败 %s: %s" % (c, exc), flush=True)
                klines[c] = []
                raw_klines[c] = []
            # 边抓边写，断点可续
            path = os.path.join(kline_dir, c + ".json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"code": c, "name": name_map.get(c, ""),
                           "date": kline_target, "updated": now_iso,
                           "bars": klines[c]}, f, ensure_ascii=False)
            pathr = os.path.join(raw_dir, c + ".json")
            with open(pathr, "w", encoding="utf-8") as f:
                json.dump({"code": c, "name": name_map.get(c, ""),
                           "date": kline_target, "updated": now_iso,
                           "bars": raw_klines[c]}, f, ensure_ascii=False)
            done_count[0] += 1
            if done_count[0] % 100 == 0:
                print("   K线进度 %d/%d" % (done_count[0], len(todo)), flush=True)
    print("   K线完成 %d/%d" % (len(klines), len(codes)), flush=True)
    # 实际K线最新日期（以抓到的数据为准，避免探针日期超前导致后续跳过）
    actual_kline = ""
    for _c, _bars in klines.items():
        if _bars:
            actual_kline = max(actual_kline, _bars[-1]["d"])
    for _c, _bars in raw_klines.items():
        if _bars:
            actual_kline = max(actual_kline, _bars[-1]["d"])
    if not actual_kline:
        actual_kline = kline_target
    if actual_kline != kline_target:
        print("   注意: 实际K线最新日期 %s（探针 %s）" % (actual_kline, kline_target),
              flush=True)

    # 抓取公司基本面（控股股东/分红/财务绩效/高管增减持/简介）
    # 带缓存：仅在文件缺失或超过 company_cache_days 天才重抓，避免每日全量请求。
    company_dir = os.path.join(data_root, "company")
    os.makedirs(company_dir, exist_ok=True)
    cache_days = cfg.get("company_cache_days", 30)
    force_company = args.force
    todo_company = []
    for c in codes:
        p = os.path.join(company_dir, c + ".json")
        need = True
        if not force_company and os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    old = json.load(f)
                upd = old.get("updated")
                if upd:
                    dt = datetime.datetime.fromisoformat(upd)
                    if (datetime.datetime.now() - dt).days < cache_days:
                        need = False
            except Exception:  # noqa: BLE001
                pass
        if need:
            todo_company.append(c)
    print("5/5 抓取公司基本面（待更新 %d/%d，缓存 %d 天）..." %
          (len(todo_company), len(codes), cache_days), flush=True)
    done_c = [0]
    cw = max(1, min(cfg.get("company_workers", 6), 8))

    def _fetch_one(code):
        try:
            d = comp.fetch_company(code, name_map.get(code, code))
            with open(os.path.join(company_dir, code + ".json"), "w",
                      encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            return code, None
        except Exception as exc:  # noqa: BLE001
            return code, str(exc)

    if todo_company:
        with ThreadPoolExecutor(max_workers=cw) as ex:
            futs = {ex.submit(_fetch_one, c): c for c in todo_company}
            for fut in as_completed(futs):
                code, err = fut.result()
                done_c[0] += 1
                if err:
                    print("   公司数据失败 %s: %s" % (code, err), flush=True)
                if done_c[0] % 100 == 0:
                    print("   公司数据进度 %d/%d" %
                          (done_c[0], len(todo_company)), flush=True)
    else:
        print("   公司基本面均最新，跳过", flush=True)

    # 收集送转分红记录，抓「不复权」除权日开盘价（配送股折算现金分红用）
    raw_opens = {}
    need = []
    for code in codes:
        p = os.path.join(company_dir, code + ".json")
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                compd = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        for y in ((compd.get("dividend", {}) or {}).get("years", []) or []):
            if ((y.get("send_ratio") or 0) or (y.get("trans_ratio") or 0)) and y.get("ex_date"):
                need.append((code, y["ex_date"]))
    need = sorted(set(need))
    if need:
        print("抓不复权除权日开盘价 %d 条（送转分红折算）..." % len(need), flush=True)

        def _ex_open(item):
            code, exd = item
            try:
                return code, exd, sm.fetch_raw_ex_open(code, exd)
            except Exception:  # noqa: BLE001
                return code, exd, None

        with ThreadPoolExecutor(max_workers=cw) as ex:
            for code, exd, op in ex.map(_ex_open, need):
                if op:
                    raw_opens.setdefault(code, {})[exd] = op
        got = sum(len(v) for v in raw_opens.values())
        print("   成功 %d/%d" % (got, len(need)), flush=True)

    # 组装快照
    # 近1年高管增持/减持（角标用；buy 记录也用于首页“高管增持”排名表）
    try:
        ib_start = (datetime.date.today() - datetime.timedelta(days=365)).isoformat()
        insider_buys = sm.fetch_insider_changes(ib_start, 1)
        insider_sells = sm.fetch_insider_changes(ib_start, -1)
        buy_codes = {r.get("code") for r in insider_buys if r.get("code")}
        sell_codes = {r.get("code") for r in insider_sells if r.get("code")}
        print("   高管增持 %d 条 / 减持 %d 条" %
              (len(insider_buys), len(insider_sells)), flush=True)
    except Exception as exc:  # noqa: BLE001
        insider_buys, insider_sells = [], []
        buy_codes, sell_codes = set(), set()
        print("   高管增减持抓取失败: %s" % exc, flush=True)

    items = []
    for s in stocks:
        code = s["code"]
        v = val.get(code) or {}
        close = v.get("CLOSE_PRICE")
        dy = bonus.get(code, 0.0) / close * 100.0 if close else None
        mv = (v.get("TOTAL_MARKET_CAP") or 0) / 1e8
        items.append({
            "code": code,
            "name": name_map.get(code) or v.get("SECURITY_NAME_ABBR") or code,
            "close": close,
            "pct": v.get("CHANGE_RATE"),
            "dy": round(dy, 2) if dy is not None else None,
            "pe": v.get("PE_TTM"),
            "pb": v.get("PB_MRQ"),
            "mv": round(mv, 1) if mv else None,
            "roe": roe_map.get(code),
            "roe_date": roe_date,
            "industry": v.get("BOARD_NAME"),
            "tags": s.get("tags", []),
            "ins_up": code in buy_codes,
            "ins_down": code in sell_codes,
        })
    # 派生字段：涨跌幅/国企/股息率均值/预期收益率/标签重写
    enrich_items(items, klines, company_dir, cfg, raw_opens)

    items.sort(key=lambda x: -(x["dy"] if x["dy"] is not None else -1))

    # 板块市值排名
    groups = {}
    for it in items:
        bn = it["industry"]
        if not bn:
            continue
        g = groups.setdefault(bn, {
            "mv": 0.0, "count": 0,
            "dy_s": 0.0, "n_dy": 0, "pe_s": 0.0, "n_pe": 0,
            "roe_s": 0.0, "n_roe": 0})
        g["mv"] += (it["mv"] or 0) * 1e8
        g["count"] += 1
        if it["dy"] is not None:
            g["dy_s"] += it["dy"]; g["n_dy"] += 1
        if it["pe"] is not None:
            g["pe_s"] += it["pe"]; g["n_pe"] += 1
        if it["roe"] is not None:
            g["roe_s"] += it["roe"]; g["n_roe"] += 1
    board_rows = [{
        "name": k,
        "mv_b": round(g["mv"] / 1e8, 1),
        "count": g["count"],
        "avg_dy": round(g["dy_s"] / g["n_dy"], 2) if g["n_dy"] else None,
        "avg_pe": round(g["pe_s"] / g["n_pe"], 2) if g["n_pe"] else None,
        "avg_roe": round(g["roe_s"] / g["n_roe"], 2) if g["n_roe"] else None,
    } for k, g in groups.items()]
    board_rows.sort(key=lambda r: -r["mv_b"])
    pct = {}
    try:
        bl = sm.fetch_industry_boards()
        pct = {d.get("f14"): d.get("f3") for d in bl if d.get("f14")}
    except Exception as exc:  # noqa: BLE001
        print("   板块涨跌幅获取失败(不影响主数据): %s" % exc)
    if not pct:
        # 兜底：用成分股涨跌幅按市值加权近似板块涨跌幅
        weighted, wsum = {}, {}
        for it in items:
            bn = it["industry"]
            if not bn or it["pct"] is None or not it["mv"]:
                continue
            weighted[bn] = weighted.get(bn, 0.0) + it["pct"] * it["mv"]
            wsum[bn] = wsum.get(bn, 0.0) + it["mv"]
        pct = {k: round(v / wsum[k], 2) for k, v in weighted.items()}
    for r in board_rows:
        r["pct"] = pct.get(r["name"])

    # 写文件
    now = datetime.datetime.now().isoformat(timespec="seconds")
    snap = {"date": trade_date, "updated_at": now,
            "total": len(items), "items": items}
    with open(os.path.join(data_root, "snapshot.json"), "w",
              encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)
    with open(os.path.join(data_root, "snapshot_%s.json" % trade_date),
              "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)

    for c, bars in klines.items():
        path = os.path.join(kline_dir, c + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"code": c, "name": name_map.get(c, ""),
                       "date": kline_target, "updated": now, "bars": bars},
                      f, ensure_ascii=False)
    for c, rbars in raw_klines.items():
        pathr = os.path.join(raw_dir, c + ".json")
        with open(pathr, "w", encoding="utf-8") as f:
            json.dump({"code": c, "name": name_map.get(c, ""),
                       "date": kline_target, "updated": now, "bars": rbars},
                      f, ensure_ascii=False)

    for it in items:
        path = os.path.join(hist_dir, it["code"] + ".json")
        hist = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                hist = json.load(f)
        if not (hist and hist[-1].get("date") == trade_date):
            hist.append({
                "date": trade_date, "close": it["close"], "dy": it["dy"],
                "pe": it["pe"], "pb": it["pb"], "mv": it["mv"],
                "roe": it["roe"]})
        hist = hist[-cfg.get("history_keep", 600):]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False)

    # 阈值提醒（跌破/涨破 -> 微信）
    try:
        from pipeline import alerts  # noqa: PLC0415
        rules = alerts.load_rules(os.path.join(meta_dir, "alerts.json"))
        events = alerts.check_crossings(rules, items, hist_dir)
        if events:
            lines = [
                "**%s(%s)** %s %.2f%%：当前 %s%%（前值 %s%%）" % (
                    e["name"], e["code"], e["kind"], e["threshold"],
                    ("%.2f" % e["cur"]), ("%.2f" % e["prev"]))
                for e in events]
            title = "红利提醒：%d 条阈值变动" % len(events)
            desp = "\n\n".join(lines) + "\n\n数据日期：%s" % trade_date
            key = os.environ.get("SERVERCHAN_KEY", "").strip()
            webhook = os.environ.get("WECOM_WEBHOOK", "").strip()
            if key:
                resp = alerts.send_serverchan(key, title, desp)
                print("   已推送 Server酱: %s" % str(resp)[:100], flush=True)
            elif webhook:
                resp = alerts.send_wecom(webhook, title, desp)
                print("   已推送 企业微信: %s" % str(resp)[:100], flush=True)
            else:
                print("   有提醒但未配置推送渠道"
                      "（仓库 Secrets 里设置 SERVERCHAN_KEY 或 WECOM_WEBHOOK）", flush=True)
        else:
            print("   无阈值提醒", flush=True)
    except Exception as exc:  # noqa: BLE001
        print("   提醒模块异常: %s" % exc, flush=True)

    # 除权除息明细（每周刷新一次，K线图 S 标志用）
    try:
        refresh_file = os.path.join(meta_dir, "dividends_refresh.json")
        need_div = True
        if os.path.exists(refresh_file):
            with open(refresh_file, encoding="utf-8") as f:
                dr = json.load(f)
            try:
                last = datetime.date.fromisoformat(dr.get("date", "2000-01-01"))
                if (datetime.date.today() - last).days < 6:
                    need_div = False
            except ValueError:
                pass
        if need_div:
            y0 = datetime.date.today().year - 4
            div_details = sm.fetch_dividend_details(
                "%d-01-01" % y0, datetime.date.today().isoformat())
            div_dir = os.path.join(data_root, "dividends")
            os.makedirs(div_dir, exist_ok=True)
            for code, recs in div_details.items():
                with open(os.path.join(div_dir, code + ".json"), "w",
                          encoding="utf-8") as f:
                    json.dump(recs, f, ensure_ascii=False)
            with open(refresh_file, "w", encoding="utf-8") as f:
                json.dump({"date": datetime.date.today().isoformat()}, f)
            print("   除权除息明细已刷新: %d 只股票" % len(div_details), flush=True)
        else:
            print("   除权除息明细在刷新周期内，沿用缓存", flush=True)
    except Exception as exc:  # noqa: BLE001
        print("   除权除息明细刷新失败: %s" % exc, flush=True)

    # 高管增持排名（近一年、总列表内、最近的在最前）
    try:
        wl = set(codes)
        by_code = {}
        for r in insider_buys:
            if r.get("code") not in wl or r["code"] in by_code:
                continue
            by_code[r["code"]] = r
        insider_items = [by_code[c] for c in by_code]
        insider_items.sort(key=lambda x: x.get("date") or "", reverse=True)
        with open(os.path.join(meta_dir, "insider_buys.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"date": trade_date, "updated_at": now,
                       "items": insider_items}, f, ensure_ascii=False)
        print("   高管增持股票: %d 只" % len(insider_items), flush=True)
    except Exception as exc:  # noqa: BLE001
        print("   高管增持抓取失败: %s" % exc, flush=True)

    # 基金“当天涨跌幅”轻量刷新（只更新 funds.json 的 nav_date/chg_today）
    try:
        funds_path = os.path.join(data_root, "funds.json")
        if os.path.exists(funds_path):
            with open(funds_path, encoding="utf-8") as f:
                fnd = json.load(f)
            fund_items = fnd.get("items") or []
            if fund_items:
                fcodes = [x.get("code") for x in fund_items if x.get("code")]
                nav_map = {}
                with ThreadPoolExecutor(max_workers=8) as ex:
                    futs = {ex.submit(sm.fetch_fund_nav, c): c for c in fcodes}
                    for fut in as_completed(futs):
                        c = futs[fut]
                        try:
                            nav_map[c] = fut.result()
                        except Exception:  # noqa: BLE001
                            nav_map[c] = None
                changed = 0
                for it in fund_items:
                    nv = nav_map.get(it.get("code"))
                    if nv and nv.get("date"):
                        if it.get("nav_date") != nv["date"] or it.get("chg_today") != nv.get("chg"):
                            it["nav_date"] = nv["date"]
                            it["chg_today"] = nv.get("chg")
                            changed += 1
                if changed:
                    fnd["updated_at"] = now
                    with open(funds_path, "w", encoding="utf-8") as f:
                        json.dump(fnd, f, ensure_ascii=False)
                print("   基金当天涨跌幅已刷新: %d/%d 只更新" % (changed, len(fund_items)),
                      flush=True)
            else:
                print("   funds.json 为空，跳过基金涨跌幅刷新", flush=True)
        else:
            print("   未找到 funds.json（尚未构建基金页），跳过", flush=True)
    except Exception as exc:  # noqa: BLE001
        print("   基金涨跌幅刷新失败: %s" % exc, flush=True)

    with open(os.path.join(meta_dir, "boards.json"), "w",
              encoding="utf-8") as f:
        json.dump({"date": trade_date, "boards": board_rows}, f,
                  ensure_ascii=False)
    with open(latest_file, "w", encoding="utf-8") as f:
        json.dump({"date": trade_date, "updated_at": now,
                   "kline_date": actual_kline,
                   "roe_date": roe_date, "total": len(items),
                   "snapshot": "snapshot.json"}, f, ensure_ascii=False)
    print("完成: %s · %d 只股票 · 板块 %d 个" % (trade_date, len(items),
                                            len(board_rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

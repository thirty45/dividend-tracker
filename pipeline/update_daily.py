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
from pipeline.kline import (valid_bars, read_series, history_complete,
                            fetched_span_complete,
                            coverage_fields as _coverage_fields,
                            merge_bars as _merge_bars, align_adjusted_history)


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


# 首页筛选：现价站上 60 月均、溢价不超过 10%，现价处于近 12 根月线收盘的最低水平，
# 且这 12 根里没有任何一根月末收盘跌破截至该月的滚动 60 月均。
# 价格口径：不复权（bfq）月末收盘，与东财「不复权」月 K 的 MA60 对齐。
MA60_MONTHS = 60
MA60_PREMIUM_MAX = 0.10
YEAR_LOW_MONTHS = 12
# 半分容差：同一分位视为相等，相邻 1 分钱（0.01）不视为最低。
YEAR_LOW_ABS_TOL = 0.005
YEAR_LOW_REL_TOL = 1e-6


def _positive_month_closes(bars):
    """月末收盘价（升序）。非正数或无法解析的月份跳过。"""
    out = []
    for ym, close in _month_end_series(bars):
        try:
            value = float(close)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        out.append((ym, value))
    return out


def _near_year_month_low(close, year_low):
    """现价等于或视为近一年月线最低收盘（含半分/浮点容差）。"""
    if year_low is None or close is None:
        return False
    tol = max(YEAR_LOW_ABS_TOL, abs(year_low) * YEAR_LOW_REL_TOL)
    return close <= year_low + tol


def _year_ma60_breaks(closes, ma_months, year_months):
    """近 year_months 根月线里，月末收盘严格小于截至该月滚动均线的月份。

    只检查已经凑满 ma_months 根的月份。更早的月份没有「当时 MA60」，
    不视为跌破；总根数不足 ma_months 时调用方不会入选。
    close == MA 视为贴平，不记入跌破。
    """
    breaks = []
    start = len(closes) - year_months
    if start < 0:
        start = 0
    for i in range(start, len(closes)):
        if i + 1 < ma_months:
            continue
        window = closes[i + 1 - ma_months:i + 1]
        ma = sum(c for _, c in window) / float(ma_months)
        ym, close = closes[i]
        if ma > 0 and close < ma:
            breaks.append({
                "ym": ym,
                "close": round(close, 4),
                "ma60": round(ma, 4),
            })
    return breaks


def screen_ma60_year_low(bars, ma_months=MA60_MONTHS, year_months=YEAR_LOW_MONTHS,
                         premium_max=MA60_PREMIUM_MAX, exclude_year_break=True):
    """用月末收盘序列筛选。月份不足时返回 None。

    月线收盘沿用 _month_end_series：每个自然月取最后一根日 K 收盘，
    当前月用最新收盘。MA、溢价、近一年最低与近一年跌破使用同一价格序列。
    首页这张表由每日流水线传入不复权日 K（data/kline_raw，price_basis=bfq），
    最新一根不复权收盘与不复权现价对齐。其它仍用前复权的指标不要改走这条序列。
    近一年最低取最近 year_months 根月线收盘的最小值（含当前月，按根数而非
    再切日历窗口；中间缺月时 12 根可能跨过 12 个自然月）。
    近一年未跌破：这 year_months 根中，只要有一根月末收盘 < 截至该月的滚动
    MA（默认 60），即剔除。贴平不算跌破。exclude_year_break=False 时保留旧口径，
    便于对照。
    """
    closes = _positive_month_closes(bars)
    if len(closes) < ma_months or len(closes) < year_months:
        return None
    ma_win = [c for _, c in closes[-ma_months:]]
    year_win = [c for _, c in closes[-year_months:]]
    ma60 = sum(ma_win) / float(ma_months)
    year_low = min(year_win)
    close = closes[-1][1]
    if ma60 <= 0 or year_low <= 0:
        return None
    premium = (close / ma60 - 1.0) * 100.0
    breaks = _year_ma60_breaks(closes, ma_months, year_months)
    above_ma = ma60 < close <= ma60 * (1.0 + premium_max)
    at_year_low = _near_year_month_low(close, year_low)
    matched = above_ma and at_year_low and not (exclude_year_break and breaks)
    return {
        "close": round(close, 4),
        "ma60": round(ma60, 4),
        "premium_pct": round(premium, 2),
        "year_low_month_close": round(year_low, 4),
        "months": len(closes),
        "ma60_breaks": breaks,
        "matched": matched,
    }


def build_ma60_year_low_payload(items, raw_klines, trade_date):
    """从最新快照股票与不复权日 K（raw_klines）写出首页筛选表。

    每日更新在写出 data/meta/ma60_year_low.json 时调用本函数。
    raw_klines 必须是不复权（bfq）日 K，与 data/kline_raw 同一口径；
    不要传入前复权 klines。
    """
    rows = []
    for it in items or []:
        code = it.get("code")
        stats = screen_ma60_year_low((raw_klines or {}).get(code) or [])
        if not stats or not stats["matched"]:
            continue
        dy = it.get("dy")
        try:
            dy = round(float(dy), 2) if dy is not None else None
        except (TypeError, ValueError):
            dy = None
        rows.append({
            "code": code,
            "name": it.get("name") or code,
            "close": stats["close"],
            "ma60": stats["ma60"],
            "premium_pct": stats["premium_pct"],
            "year_low_month_close": stats["year_low_month_close"],
            "dy": dy,
            "months": stats["months"],
        })
    rows.sort(key=lambda r: (
        r["premium_pct"] if r["premium_pct"] is not None else float("inf"),
        -(r["dy"] if r["dy"] is not None else -1e9),
        r.get("code") or "",
    ))
    return {
        "date": trade_date,
        "params": {
            "ma_months": MA60_MONTHS,
            "premium_max": MA60_PREMIUM_MAX,
            "year_months": YEAR_LOW_MONTHS,
            "year_low_abs_tol": YEAR_LOW_ABS_TOL,
            "year_low_rel_tol": YEAR_LOW_REL_TOL,
            "month_close": "month_end",
            "price_basis": "bfq",
            # 近一年任意一根月末收盘 < 截至该月的滚动 MA60 则剔除；贴平不算。
            "exclude_year_close_below_ma": True,
        },
        "items": rows,
    }


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


def _boll_below_days(bars, period=20, k=2.0):
    """从最新一根K线向前数，连续收盘价跌破布林带下轨（20日 ± 2σ）的天数。
    当日未跌破返回 0；数据不足 20 根返回 0。"""
    if not bars or len(bars) < period:
        return 0
    lowers = _boll_lower(bars, period, k)
    days = 0
    for i in range(len(bars) - 1, -1, -1):
        lo = lowers[i]
        if lo is None or bars[i]["c"] >= lo:
            break
        days += 1
    return days


def _dma_white_series(bars, short=5, long=89):
    """DMA 白线（DIF = MA(C,short) - MA(C,long)）的有效历史序列。"""
    if not bars or len(bars) < long:
        return []
    closes = []
    for b in bars:
        try:
            value = float(b.get("c"))
        except (TypeError, ValueError):
            value = None
        closes.append(value)
    # 最新长均线窗口不完整时，不能把更早的有效值误当作当前白线。
    if any(value is None for value in closes[-long:]):
        return []
    out = []
    for i in range(long - 1, len(closes)):
        short_win = closes[i - short + 1:i + 1]
        long_win = closes[i - long + 1:i + 1]
        if any(v is None for v in short_win) or any(v is None for v in long_win):
            continue
        out.append(sum(short_win) / short - sum(long_win) / long)
    return out


def _quantile(values, q):
    """线性插值分位数；values 为空时返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _empirical_percentile(values, current):
    """当前值在历史序列中的经验分位（0-100，重复值计入）。"""
    if not values or current is None:
        return None
    return sum(value <= current for value in values) / len(values) * 100


def enrich_items(items, klines, company_dir, cfg, raw_opens=None, raw_klines=None):
    """为每只股票计算派生字段并写回 item（就地修改）。

    派生：近1月/近3月涨跌幅、每股收益、每股分红、连续分红年数、是否国有、
    近5年平均市净率、近5年平均股息率、预期收益率①/②；并重写 tags。
    raw_opens: {code: {ex_date: 不复权开盘价}} —— 配送股折算现金分红用
    （前复权K线会把除权日价格按后续分红调整，必须用不复权价）。
    """
    today = datetime.date.today()
    raw_opens = raw_opens or {}
    raw_klines = raw_klines or {}
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
        # 连续跌破布林带下轨天数（20日 ± 2σ，首页排名框用）
        it["boll_days"] = _boll_below_days(bars)
        # DMA(5,89,36) 白线：历史最低30%按 DIF 历史序列的30%分位判定。
        dma_values = _dma_white_series(bars, 5, 89)
        if len(dma_values) >= 30:
            dma_p30 = _quantile(dma_values, 0.30)
            dma_white = dma_values[-1]
            it["dma_white"] = round(dma_white, 6)
            it["dma_white_p30"] = round(dma_p30, 6)
            it["dma_white_percentile"] = round(
                _empirical_percentile(dma_values, dma_white), 1)
            it["dma_samples"] = len(dma_values)
            it["dma_low30"] = dma_white <= dma_p30
        else:
            it["dma_white"] = None
            it["dma_white_p30"] = None
            it["dma_white_percentile"] = None
            it["dma_samples"] = len(dma_values)
            it["dma_low30"] = False

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
        # 年度股息率口径：每股现金分红 ÷ 该年度「不复权」年末收盘价。
        # 前复权价会把历史股价按后续分红回溯压低，导致股息率虚高（如中石化2021年
        # 前复权价2.73→股息率17% vs 真实不复权价4.23→11%）。因此统一用不复权价，
        # 这也是投资者当年实际成本价、口径更贴近“当年真实股息率”。
        # 数据源：优先不复权K线(data/kline_raw)；早年(东财K线仅到2019/2021)用新浪
        # 年线补齐（已验证新浪年线年末收盘与东财不复权逐项一致）。
        year_close_raw = {}
        for b in (raw_klines.get(code) or []):
            d = b.get("d") or b.get("date") or ""
            if len(d) >= 4:
                year_close_raw[d[:4]] = b.get("c")  # 同年度保留最后（年末收盘）
        # 早年/缺失年份：用新浪年线补齐（仅当该年不复权K线没有收盘价）
        need_sina = False
        for y in years:
            yr = str(y.get("year"))
            if y.get("per_share") is not None and yr not in year_close_raw:
                need_sina = True
                break
        if need_sina:
            try:
                sina_yearly = sm.fetch_sina_yearly_close(code)
            except Exception:  # noqa: BLE001
                sina_yearly = {}
            for kk, vv in sina_yearly.items():
                year_close_raw.setdefault(kk, vv)
        # 除权日开盘价（未复权）：
        #   1) 优先从未复权K线(数据/raw klines)里取当天的开盘价；
        #   2) 缺失时回退到单独抓取的 raw_opens（送转折算用）。
        # 配送股现金等价 = 除权日开盘价 × 每股配送股数（送股+转增）。
        div_changed = False
        raw = raw_opens.get(code) or {}
        rk_map = {b["d"]: b for b in (raw_klines.get(code) or [])}
        for y in years:
            exd = y.get("ex_date")
            if not exd:
                # 尚未除权实施（如当年新预案）：年度股息率口径无意义，置空
                if y.get("yield_annual") is not None:
                    y["yield_annual"] = None
                    div_changed = True
                continue
            b = rk_map.get(exd)
            ex_open = b.get("o") if b else None
            if ex_open is None:
                ex_open = raw.get(exd)
            y["ex_open"] = ex_open
            ps = y.get("per_share")
            c_end = year_close_raw.get(str(y.get("year")))
            yn = (round(ps / c_end * 100.0, 2)
                  if ps is not None and c_end else None)
            if y.get("yield_annual") != yn:
                y["yield_annual"] = yn
                div_changed = True
            extra = 0.0
            if ex_open:
                extra = ex_open * ((y.get("send_ratio") or 0) + (y.get("trans_ratio") or 0))
            y["per_share_real"] = round((ps or 0) + extra, 4) if ps is not None else None
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

        # 连续2年及以上股息率 < 1% 判定（最近两个完整年度：当年-1、当年-2；
        # 年度股息率=每股现金分红/不复权年末收盘价；两年均<1%即视为连续2年以上，
        # 某年缺收盘价则视为不满足）
        dy_vals = []
        for yr in (today.year - 1, today.year - 2):
            c = year_close_raw.get(str(yr))
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


def _shift_days(d, days):
    """日期(YYYY-MM-DD 或 YYYYMMDD)平移 N 天 -> YYYYMMDD；异常返回 None。"""
    s = str(d or "").replace("-", "")
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        dt = datetime.datetime.strptime(s, "%Y%m%d").date()
    except ValueError:
        return None
    return (dt + datetime.timedelta(days=days)).strftime("%Y%m%d")


def _has_complete_coverage(path, target_date):
    payload = read_series(path)
    return bool(history_complete(payload) and payload["bars"][-1]["d"] >= target_date)

def _all_kline_coverage(codes, kline_dir, raw_dir, target_date):
    """检查当前有效股票池的前复权/不复权文件是否都完成覆盖。"""
    for code in codes:
        if not _has_complete_coverage(os.path.join(kline_dir, code + ".json"),
                                      target_date):
            return False
        if not _has_complete_coverage(os.path.join(raw_dir, code + ".json"),
                                      target_date):
            return False
    return bool(codes)


def _pair_bars_valid(qbars, rbars, target_date):
    """Validate each price series independently, including dates and OHLCV."""
    return valid_bars(qbars, target_date) and valid_bars(rbars, target_date)


def refresh_kline_series(code, old, target_date, cfg, raw=False, force=False):
    """Return a candidate without modifying files; failure retains that mode's cache."""
    old = old if isinstance(old, dict) and valid_bars(old.get("bars")) else {}
    previous = old.get("bars", [])
    complete = history_complete(old)
    if not force and complete and previous[-1]["d"] >= target_date:
        return dict(payload=old, changed=False, error=None, full=False)
    need_full = force or not complete
    full_date = old.get("full_date") or ""
    interval = cfg.get("kline_full_days", 0)
    if not need_full and interval > 0:
        try:
            need_full = (datetime.date.today() - datetime.date.fromisoformat(full_date)).days >= interval
        except ValueError:
            pass

    span_complete = True

    def fetch_from(begin):
        nonlocal span_complete
        name, bars = ("", sm.fetch_kline_raw(code, begin, "20500101", target_date)) if raw else sm.fetch_kline(code, begin, "20500101", target_date)
        if not valid_bars(bars, target_date):
            raise RuntimeError("K线为空、损坏或未覆盖目标日期 %s" % target_date)
        span_complete = fetched_span_complete(bars)
        return name, bars

    try:
        begin = cfg.get("kline_start", "19900101") if need_full else _shift_days(previous[-1]["d"], -15)
        name, bars = fetch_from(begin)
        if not need_full:
            try:
                aligned = previous if raw else align_adjusted_history(previous, bars)
            except ValueError:
                name, bars = fetch_from(cfg.get("kline_start", "19900101"))
                need_full = True
            else:
                bars = _merge_bars(aligned, bars, cfg.get("history_keep", 1400))
        if need_full and raw and previous and not span_complete:
            # Unadjusted prices can be merged; an early-window gap must not drop the tail.
            bars = _merge_bars(previous, bars, cfg.get("history_keep", 0))
        elif need_full and previous and not span_complete and len(bars) < len(previous):
            raise RuntimeError("早期历史未补齐，保留更长的本地K线")
        elif need_full:
            bars = _merge_bars([], bars, cfg.get("history_keep", 1400))
        if not valid_bars(bars, target_date):
            raise RuntimeError("合并后的K线校验失败")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        history_ok = span_complete if need_full else True
        payload = dict(code=code, name=name or old.get("name", ""), date=bars[-1]["d"],
                       updated=now, full_date=now[:10] if need_full and history_ok else ("" if need_full else full_date),
                       bars=bars, history_complete=history_ok)
        payload.update(_coverage_fields(bars, target_date))
        return dict(payload=payload, changed=True, error=None, full=need_full)
    except Exception as exc:
        return dict(payload=old, changed=False, error=str(exc), full=need_full)

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
    exclude_prefixes = tuple(str(p) for p in cfg.get("exclude_code_prefixes", [])
                             if str(p))
    if exclude_prefixes:
        before = len(stocks)
        stocks = [s for s in stocks
                  if not str(s.get("code") or "").startswith(exclude_prefixes)]
        print("按代码前缀 %s 防御性剔除 %d 只" %
              (", ".join(exclude_prefixes), before - len(stocks)))
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
    kline_target = probe_date or trade_date
    print("估值交易日=%s 探针K线日=%s 已存(date=%s, kline=%s)"
          % (trade_date, probe_date, stored.get("date"), stored.get("kline_date")))
    if not args.force and not snapshot_stale and not kline_stale:
        if _all_kline_coverage(codes, kline_dir, raw_dir, kline_target):
            print("数据已是最新且K线覆盖标记完整(%s)，跳过" % trade_date)
            return 0
        print("日期一致但K线覆盖标记缺失/过期，安排完整回填")
    if kline_stale and not snapshot_stale:
        print("K线已有新交易日(%s)，刷新K线；估值数据尚未更新则快照沿用 %s"
              % (probe_date, trade_date))
    print("股票池: %d 只（交易日 %s）" % (len(codes), trade_date))

    print("1/5 抓取全市场估值快照 ...")
    _, valuation = sm.fetch_valuation(trade_date)
    val = {v["SECURITY_CODE"]: v for v in valuation if v.get("SECURITY_CODE")}
    print("   估值记录 %d 条" % len(val))
    if not val:
        raise RuntimeError("估值接口返回空数据，保留原快照，禁止覆盖为空行情")

    print("2/5 抓取最新报告期 ROE ...")
    roe_date, roe_map = sm.fetch_roe()
    print("   报告期 %s，%d 条" % (roe_date, len(roe_map)))

    print("3/5 抓取近12个月已实施分红 ...")
    end = datetime.date.today()
    start = end - datetime.timedelta(days=366)
    bonus = sm.fetch_sharebonus(start.isoformat(), end.isoformat())
    print("   %d 只股票有分红记录" % len(bonus))

    print("4/5 抓取K线(并发) ...", flush=True)
    klines, raw_klines = {}, {}
    todo = []
    for code in codes:
        adjusted = read_series(os.path.join(kline_dir, code + ".json"))
        raw = read_series(os.path.join(raw_dir, code + ".json"))
        current = lambda payload: history_complete(payload) and payload["bars"][-1]["d"] >= kline_target
        if not args.force and current(adjusted) and current(raw):
            klines[code], raw_klines[code] = adjusted["bars"], raw["bars"]
        else:
            todo.append((code, adjusted, raw))
    print("   待更新K线 %d 只 · 已最新 %d 只；两套序列独立更新" %
          (len(todo), len(codes) - len(todo)), flush=True)

    def _fetch_pair(item):
        code, adjusted, raw = item
        return code, refresh_kline_series(code, adjusted, kline_target, cfg, force=args.force), refresh_kline_series(code, raw, kline_target, cfg, raw=True, force=args.force)

    failed_codes = set()
    with ThreadPoolExecutor(max_workers=cfg.get("workers", 6)) as ex:
        futures = {ex.submit(_fetch_pair, item): item[0] for item in todo}
        for done, future in enumerate(as_completed(futures), 1):
            code, adjusted, raw = future.result()
            for mode, result, folder, output in (("前复权", adjusted, kline_dir, klines),
                                                 ("不复权", raw, raw_dir, raw_klines)):
                payload = result["payload"]
                output[code] = payload.get("bars", [])
                if result["error"]:
                    failed_codes.add(code)
                    print("   K线失败 %s %s（保留此序列原文件）: %s" %
                          (code, mode, result["error"]), flush=True)
                if result["changed"]:
                    if payload.get("name"):
                        name_map[code] = payload["name"]
                    else:
                        payload["name"] = name_map.get(code, "")
                    with open(os.path.join(folder, code + ".json"), "w", encoding="utf-8") as f:
                        json.dump(payload, f, ensure_ascii=False, allow_nan=False)
            if done % 100 == 0:
                print("   K线进度 %d/%d" % (done, len(todo)), flush=True)
    usable = sum(bool(klines.get(c)) and bool(raw_klines.get(c)) for c in codes)
    print("   K线可用 %d/%d；本轮失败 %d 只" % (usable, len(codes), len(failed_codes)), flush=True)
    if failed_codes:
        print("   待补抓代码: " + ",".join(sorted(failed_codes)), flush=True)

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
    cw = max(1, min(cfg.get("company_workers", 6), 16))

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

    # 收集需补「不复权」除权日开盘价的分红记录：
    #   仅当 ex_open 缺失、未复权K线未覆盖该除权日且未标记抓取失败时才抓取。
    #   限近8年除权日 + 每轮上限，抓取失败打 ex_open_miss 标记，避免每天重复重试
    #   （早年数据源无记录，反复重试只会浪费 2 小时）。
    raw_opens = {}
    need = []
    rk_dates = {}
    ex_cutoff = "%d-01-01" % (datetime.date.today().year - 8)
    ex_max = cfg.get("ex_open_backfill_max", 1500)
    for code in codes:
        rk_dates[code] = {b["d"] for b in (raw_klines.get(code) or [])}
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
            exd = y.get("ex_date")
            if not exd or exd < ex_cutoff:
                continue
            if (y.get("ex_open") is None and not y.get("ex_open_miss")
                    and exd not in rk_dates.get(code, set())):
                need.append((code, exd))
    need = sorted(set(need), key=lambda x: x[1])[-ex_max:]
    miss = set()
    if need:
        print("抓不复权除权日开盘价 %d 条（近8年·本轮上限%d）..."
              % (len(need), ex_max), flush=True)

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
                else:
                    miss.add((code, exd))
        got = sum(len(v) for v in raw_opens.values())
        print("   成功 %d/%d" % (got, len(need)), flush=True)
    # 抓取失败打标记，避免下一轮重复重试
    if miss:
        by_code = {}
        for code, exd in miss:
            by_code.setdefault(code, set()).add(exd)
        for code, exds in by_code.items():
            p = os.path.join(company_dir, code + ".json")
            try:
                with open(p, encoding="utf-8") as f:
                    compd = json.load(f)
            except Exception:  # noqa: BLE001
                continue
            changed = False
            for y in ((compd.get("dividend", {}) or {}).get("years", []) or []):
                if y.get("ex_date") in exds:
                    y["ex_open_miss"] = True
                    changed = True
            if changed:
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(compd, f, ensure_ascii=False)

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
    enrich_items(items, klines, company_dir, cfg, raw_opens, raw_klines)

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
    dma_items = [{"code": it["code"], "name": it.get("name"),
                  "white": it.get("dma_white"),
                  "threshold": it.get("dma_white_p30"),
                  "percentile": it.get("dma_white_percentile"),
                  "samples": it.get("dma_samples")}
                 for it in items if it.get("dma_low30")]
    dma_items.sort(key=lambda it: (
        it.get("percentile") is None,
        it.get("percentile") if it.get("percentile") is not None else float("inf"),
        it.get("code") or ""))
    with open(os.path.join(meta_dir, "dma_low30.json"), "w", encoding="utf-8") as f:
        json.dump({"date": trade_date,
                   "params": {"short": 5, "long": 89, "signal": 36,
                              "percentile": 0.30},
                   "items": dma_items}, f, ensure_ascii=False)
    # 首页 MA60 年低表只用不复权月线；前复权 klines 仍供涨跌幅、DMA 等其它指标。
    ma60_payload = build_ma60_year_low_payload(items, raw_klines, trade_date)
    with open(os.path.join(meta_dir, "ma60_year_low.json"), "w", encoding="utf-8") as f:
        json.dump(ma60_payload, f, ensure_ascii=False)
    print("   近一年未跌破60月均、溢价≤10%且月线最低(不复权): %d 只"
          % len(ma60_payload["items"]),
          flush=True)

    # Each successfully refreshed price series was already persisted above.

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

    # funds.py refreshes quotes and NAV together; do not download every fund twice.

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

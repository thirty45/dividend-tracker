# -*- coding: utf-8 -*-
"""1000 日均线首次触及事件回测（研究用，不上站）。

口径（投资Bot 锁定）：
- 价格与均线：前复权收盘；MA = 最近 1000 个交易日简单算术平均
- 信号：先曾高过均线至少 5%（收盘相对均线溢价 ≥ 5%），再首次收盘
  触及或跌破均线（收盘 ≤ 均线）。次日开盘买入。
- 止盈：2%/5%/10%/15% 四档独立。持有期最高价触达则按目标价出；
  60 个交易日内未触达，按第 60 个交易日收盘出。样本期末仍未满 60 日
  且未触达的，按末日收盘结算，记为未命中（截尾）。
- 上一笔结束后必须重新站上均线（收盘 > 均线），并再次满足溢价 ≥ 5%，
  才能再进，避免在均线附近反复刷单。
- 成本：单边万三（买入、卖出各 0.03%）。
- 池子：当前首页可算股息率的 snapshot/watchlist（幸存者偏差，须标注）。
- 年化：按资金占用做一条组合净值再算，不用每笔平均收益直接换算。
  绝对年化只写入本目录内部 summary.json，不上站、不改 display_policy。

入口: python -m pipeline.backtest.ma1000_touch
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import statistics
import sys
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from pipeline.kline import read_series  # noqa: E402

STRATEGY_NAME = "MA1000_首次触及"
COST_ONE_WAY = 0.0003
MA_DAYS = 1000
ARM_PREMIUM = 0.05
MAX_HOLD_DAYS = 60
TARGETS = (0.02, 0.05, 0.10, 0.15)
SAMPLE_BEG = "2018-01-01"
PRICE_BASIS = "qfq"
OUT_DIR = os.path.join(BASE, "data", "backtest", "MA1000_首次触及")


def clean_bars(bars):
    """保留日期递增、OHLC 可解析且收盘为正的交易日（跳过坏根）。"""
    out = []
    prev = ""
    for b in bars or []:
        d = b.get("d") or b.get("date") or ""
        if not d or d <= prev:
            continue
        try:
            o = float(b.get("o"))
            h = float(b.get("h"))
            low = float(b.get("l"))
            c = float(b.get("c"))
        except (TypeError, ValueError):
            continue
        if c <= 0 or o <= 0 or h <= 0:
            continue
        if h < max(o, c, low):
            h = max(o, c, low, h)
        out.append({"d": d, "o": o, "h": h, "l": low, "c": c})
        prev = d
    return out


def _ma(closes, i, ma_days):
    if i + 1 < ma_days:
        return None
    window = closes[i + 1 - ma_days:i + 1]
    ma = sum(window) / float(ma_days)
    return ma if ma > 0 else None


def simulate_stock(bars, target_pct, ma_days=MA_DAYS, arm_premium=ARM_PREMIUM,
                   max_hold=MAX_HOLD_DAYS, cost=COST_ONE_WAY, beg=None, end=None):
    """单只股票、单一止盈档的全部交易（状态机从有均线之日起跑）。

    早于 beg 的交易仍会模拟以维护再入场状态，但不进入返回列表。
    入场日晚于 end 的不纳入。返回的每笔包含日内收益序列，供组合净值使用。
    """
    bars = clean_bars(bars)
    n = len(bars)
    if n < ma_days + 1:
        return []
    closes = [b["c"] for b in bars]
    trades = []
    state = "seek_arm"
    i = ma_days - 1
    while i < n:
        ma = _ma(closes, i, ma_days)
        if ma is None:
            i += 1
            continue
        close = closes[i]
        if state == "need_above":
            if close > ma:
                state = "seek_arm"
            else:
                i += 1
                continue
        if state == "seek_arm":
            if close / ma - 1.0 >= arm_premium:
                state = "armed"
            elif close <= ma:
                state = "need_above"
            i += 1
            continue
        # armed: 等待首次收盘触及或跌破
        if close > ma:
            i += 1
            continue
        signal_i = i
        entry_i = signal_i + 1
        if entry_i >= n:
            break
        entry_open = bars[entry_i]["o"]
        if entry_open <= 0:
            state = "need_above"
            i = entry_i
            continue
        target_px = entry_open * (1.0 + target_pct)
        last_hold = min(entry_i + max_hold - 1, n - 1)
        exit_i = last_hold
        hit = False
        exit_px = bars[last_hold]["c"]
        for j in range(entry_i, last_hold + 1):
            if bars[j]["h"] >= target_px:
                exit_i = j
                hit = True
                exit_px = target_px
                break
        hold_days = exit_i - entry_i + 1
        censored = (not hit) and hold_days < max_hold
        daily = _trade_daily_returns(bars, entry_i, exit_i, entry_open, exit_px, hit, cost)
        ret = (exit_px * (1.0 - cost)) / (entry_open * (1.0 + cost)) - 1.0
        entry_date = bars[entry_i]["d"]
        include = True
        if beg and entry_date < beg:
            include = False
        if end and entry_date > end:
            include = False
        if include:
            trades.append({
                "signal_date": bars[signal_i]["d"],
                "entry_date": entry_date,
                "exit_date": bars[exit_i]["d"],
                "entry_px": round(entry_open, 6),
                "exit_px": round(exit_px, 6),
                "target_px": round(target_px, 6),
                "hit": hit,
                "censored": censored,
                "hold_days": hold_days,
                "ret": ret,
                "daily": daily,
            })
        state = "need_above"
        i = exit_i
    return trades


def _trade_daily_returns(bars, entry_i, exit_i, entry_open, exit_px, hit, cost):
    """入场日至出场日（含）的逐日收益率，买卖日计入单边成本。"""
    shares = 1.0 / (entry_open * (1.0 + cost))
    daily = {}
    prev_value = 1.0
    for j in range(entry_i, exit_i + 1):
        if j == exit_i:
            value = shares * exit_px * (1.0 - cost)
        else:
            value = shares * bars[j]["c"]
        daily[bars[j]["d"]] = value / prev_value - 1.0
        prev_value = value
    return daily


def _stats(trades):
    n = len(trades)
    if not n:
        return {
            "n": 0, "hits": 0, "misses": 0, "hit_rate": None,
            "avg_hold_days_hit": None, "avg_hold_days_miss": None,
            "avg_ret_all": None, "avg_ret_hit": None, "avg_ret_miss": None,
        }
    hits = [t for t in trades if t["hit"]]
    misses = [t for t in trades if not t["hit"]]
    return {
        "n": n,
        "hits": len(hits),
        "misses": len(misses),
        "hit_rate": len(hits) / n,
        "avg_hold_days_hit": (sum(t["hold_days"] for t in hits) / len(hits)) if hits else None,
        "avg_hold_days_miss": (sum(t["hold_days"] for t in misses) / len(misses)) if misses else None,
        "avg_ret_all": sum(t["ret"] for t in trades) / n,
        "avg_ret_hit": (sum(t["ret"] for t in hits) / len(hits)) if hits else None,
        "avg_ret_miss": (sum(t["ret"] for t in misses) / len(misses)) if misses else None,
    }


def equity_curve(trades, calendar):
    """等权资金占用净值。

    规则：
    - 信号日次日开盘入场；当日有持仓时，对各持仓等权（每日再平衡到等权）；
    - 当日无持仓则为现金，收益为 0；
    - 年化分母包含空仓交易日，以反映资金占用与空转时间。
    - 不用「每笔平均收益 × 年化周转」直接换算。
    """
    by_date = defaultdict(list)
    for t in trades:
        for d, r in t["daily"].items():
            by_date[d].append(r)
    nav = 1.0
    series = []
    for d in calendar:
        rs = by_date.get(d)
        r = statistics.fmean(rs) if rs else 0.0
        nav *= (1.0 + r)
        series.append({"date": d, "ret": r, "nav": nav, "n_pos": len(rs) if rs else 0})
    return series


def annualize(nav, n_trading_days, calendar_days=None):
    """按完整样本期（含空仓日）年化。n_trading_days < 1 或 nav<=0 时返回 None。"""
    out = {}
    if n_trading_days and n_trading_days > 0 and nav and nav > 0:
        out["ann_252"] = nav ** (252.0 / n_trading_days) - 1.0
    else:
        out["ann_252"] = None
    if calendar_days and calendar_days > 0 and nav and nav > 0:
        out["ann_365"] = nav ** (365.25 / calendar_days) - 1.0
    else:
        out["ann_365"] = None
    return out


def build_calendar(klines, beg, end):
    dates = set()
    for bars in (klines or {}).values():
        for b in clean_bars(bars):
            d = b["d"]
            if beg and d < beg:
                continue
            if end and d > end:
                continue
            dates.add(d)
    return sorted(dates)


def run_universe(items, klines, targets=TARGETS, ma_days=MA_DAYS,
                 arm_premium=ARM_PREMIUM, max_hold=MAX_HOLD_DAYS,
                 cost=COST_ONE_WAY, beg=SAMPLE_BEG, end=None):
    """对池内全部标的、四档止盈独立回测。"""
    pool = []
    for it in items or []:
        code = it.get("code")
        dy = it.get("dy")
        try:
            dy = float(dy) if dy is not None else None
        except (TypeError, ValueError):
            dy = None
        if not code or dy is None:
            continue
        pool.append({"code": code, "name": it.get("name") or code, "dy": dy})

    if end is None:
        end = ""
        for bars in (klines or {}).values():
            cb = clean_bars(bars)
            if cb:
                end = max(end, cb[-1]["d"])
    calendar = build_calendar(klines, beg, end)
    by_target = {}
    all_trades = {}
    for tgt in targets:
        trades = []
        for it in pool:
            code = it["code"]
            stock_trades = simulate_stock(
                (klines or {}).get(code) or [], tgt,
                ma_days=ma_days, arm_premium=arm_premium,
                max_hold=max_hold, cost=cost, beg=beg, end=end)
            for t in stock_trades:
                row = dict(t)
                row["code"] = code
                row["name"] = it["name"]
                row["dy"] = it["dy"]
                trades.append(row)
        trades.sort(key=lambda t: (t["entry_date"], t["code"]))
        stats = _stats(trades)
        series = equity_curve(trades, calendar)
        nav = series[-1]["nav"] if series else 1.0
        cal_days = None
        if calendar:
            d0 = datetime.date.fromisoformat(calendar[0])
            d1 = datetime.date.fromisoformat(calendar[-1])
            cal_days = (d1 - d0).days
        ann = annualize(nav, len(calendar), cal_days)
        by_target[tgt] = {
            "target_pct": tgt,
            "stats": stats,
            "nav": nav,
            "ann_252": ann["ann_252"],
            "ann_365": ann["ann_365"],
            "n_trading_days": len(calendar),
            "calendar_days": cal_days,
            "start": calendar[0] if calendar else None,
            "end": calendar[-1] if calendar else None,
        }
        all_trades[tgt] = trades
        by_target[tgt]["equity"] = series
    return {
        "pool": pool,
        "beg": beg,
        "end": end,
        "calendar": calendar,
        "by_target": by_target,
        "trades": all_trades,
    }


def _round_or_none(x, nd=6):
    if x is None:
        return None
    return round(float(x), nd)


def to_summary(result):
    pool = result["pool"]
    by_target = result["by_target"]
    tiers = []
    for tgt in sorted(by_target):
        row = by_target[tgt]
        st = row["stats"]
        tiers.append({
            "target_pct": tgt,
            "n": st["n"],
            "hits": st["hits"],
            "misses": st["misses"],
            "hit_rate": _round_or_none(st["hit_rate"], 6),
            "avg_hold_days_hit": _round_or_none(st["avg_hold_days_hit"], 4),
            "avg_hold_days_miss": _round_or_none(st["avg_hold_days_miss"], 4),
            "avg_ret_all": _round_or_none(st["avg_ret_all"], 6),
            "avg_ret_hit": _round_or_none(st["avg_ret_hit"], 6),
            "avg_ret_miss": _round_or_none(st["avg_ret_miss"], 6),
            "nav": _round_or_none(row["nav"], 6),
            "ann_252": _round_or_none(row["ann_252"], 6),
            "ann_365": _round_or_none(row["ann_365"], 6),
        })
    return {
        "strategy": STRATEGY_NAME,
        "deployable": False,
        "public_display": False,
        "display_policy_untouched": True,
        "price_basis": PRICE_BASIS,
        "ma_days": MA_DAYS,
        "arm_premium": ARM_PREMIUM,
        "max_hold_days": MAX_HOLD_DAYS,
        "cost_one_way": COST_ONE_WAY,
        "sample": {
            "beg": result["beg"],
            "end": result["end"],
            "n_trading_days": len(result["calendar"]),
            "start": result["calendar"][0] if result["calendar"] else None,
            "end_traded": result["calendar"][-1] if result["calendar"] else None,
        },
        "universe": {
            "source": "snapshot/watchlist with non-null dy",
            "survivorship": "current_watchlist_applied_historically",
            "survivorship_note": "股票池为当前首页可算股息率的标的，回溯到历史；已退市或当前不再入池的高息股不会出现。存在幸存者偏差，结果不可直接外推。",
            "size": len(pool),
        },
        "entry_exit": {
            "signal": "先曾收盘相对 MA1000 溢价≥5%，再首次收盘≤均线",
            "entry": "信号日次日开盘买入（不用信号日收盘）",
            "take_profit": "持有期最高价触及目标价则按目标价出，同日可完成",
            "time_stop": "60 个交易日内未触达，按第 60 个交易日收盘出；样本期末不足 60 日则按末日收盘结算，记未命中",
            "reentry": "上一笔结束后须重新收盘>均线，并再次溢价≥5% 后，才接受下一次触及",
            "targets": list(TARGETS),
        },
        "equity_rule": {
            "weighting": "equal_weight_active_positions_daily_rebalance",
            "idle": "cash_return_zero",
            "annualization": "nav_end ** (252 / N_trading_days) - 1，N 为样本期全部交易日（含空仓日）。对照口径 ann_365 用 365.25 / 日历天数。禁止用每笔平均收益直接换算年化。",
            "cost": "买入按开盘价计单边万三，卖出按成交价（目标价或收盘价）计单边万三。未计滑点、印花税与冲击。",
        },
        "tiers": tiers,
        "summary_note": "ann_252 / ann_365 / nav 仅供内部复核，不上站、不接到 #/backtest，不改 display_policy。",
    }


def write_artifacts(result, out_dir=OUT_DIR):
    os.makedirs(out_dir, exist_ok=True)
    summary = to_summary(result)
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    # 精简净值，供内部图用；不上站
    slim = {}
    for tgt, row in result["by_target"].items():
        key = "%.2f" % tgt
        slim[key] = [{"date": p["date"], "nav": round(p["nav"], 6), "n_pos": p["n_pos"]}
                     for p in row.get("equity") or []]
    with open(os.path.join(out_dir, "equity.json"), "w", encoding="utf-8") as f:
        json.dump({"beg": result["beg"], "end": result["end"], "series": slim},
                  f, ensure_ascii=False)
    # 交易明细（不含逐日收益，避免文件过大）
    trades_out = {}
    for tgt, trades in result["trades"].items():
        trades_out["%.2f" % tgt] = [{k: v for k, v in t.items() if k != "daily"}
                                    for t in trades]
    with open(os.path.join(out_dir, "trades.json"), "w", encoding="utf-8") as f:
        json.dump(trades_out, f, ensure_ascii=False)
    write_chart(summary, slim, os.path.join(out_dir, "chart.html"))
    return summary


def write_chart(summary, slim, path):
    tiers = summary.get("tiers") or []
    rows = []
    for t in tiers:
        rows.append(
            "<tr><td>{:.0f}%</td><td>{}</td><td>{}</td><td>{}</td>"
            "<td>{:.2%}</td><td>{}</td><td>{}</td><td>{:.2%}</td>"
            "<td>{:.4f}</td><td>{:.2%}</td></tr>".format(
                t["target_pct"] * 100,
                t["n"], t["hits"], t["misses"],
                t["hit_rate"] or 0,
                "%.2f" % t["avg_hold_days_hit"] if t["avg_hold_days_hit"] is not None else "—",
                "%.2f" % t["avg_hold_days_miss"] if t["avg_hold_days_miss"] is not None else "—",
                t["avg_ret_all"] or 0,
                t["nav"] or 1,
                t["ann_252"] or 0,
            )
        )
    # 简易 SVG 净值（四档）
    colors = {"0.02": "#1f77b4", "0.05": "#ff7f0e", "0.10": "#2ca02c", "0.15": "#d62728"}
    width, height, pad = 760, 260, 36
    all_pts = [p for series in slim.values() for p in series]
    if all_pts:
        vmax = max(p["nav"] for p in all_pts)
        vmin = min(p["nav"] for p in all_pts)
        if vmax <= vmin:
            vmax = vmin + 1e-6
        n = max(len(next(iter(slim.values()))), 1)
        def xy(i, nav, n_i):
            x = pad + (width - 2 * pad) * (i / max(n_i - 1, 1))
            y = height - pad - (height - 2 * pad) * ((nav - vmin) / (vmax - vmin))
            return x, y
        paths = []
        for key, series in slim.items():
            if not series:
                continue
            pts = []
            n_i = len(series)
            for i, p in enumerate(series):
                x, y = xy(i, p["nav"], n_i)
                pts.append(("%s%.1f,%.1f" % ("M" if i == 0 else "L", x, y)))
            paths.append('<path d="%s" fill="none" stroke="%s" stroke-width="1.6"/>'
                         % (" ".join(pts), colors.get(key, "#333")))
        svg = ('<svg viewBox="0 0 %d %d" width="100%%" height="260" '
               'xmlns="http://www.w3.org/2000/svg">%s</svg>' % (width, height, "".join(paths)))
    else:
        svg = "<p>无净值序列</p>"
    samp = summary.get("sample") or {}
    html = f"""<!DOCTYPE html>
<meta charset="utf-8">
<title>MA1000 首次触及（研究内部，不上站）</title>
<style>
body {{ font-family: sans-serif; margin: 24px; color: #222; }}
.warn {{ background: #fff3cd; padding: 10px 12px; border-radius: 6px; }}
table {{ border-collapse: collapse; margin-top: 16px; }}
th, td {{ border: 1px solid #ddd; padding: 6px 8px; font-size: 13px; }}
th {{ background: #f6f6f6; }}
.note {{ color: #555; font-size: 13px; line-height: 1.5; max-width: 880px; }}
</style>
<h1>MA1000 首次触及 · 事件回测（内部）</h1>
<p class="warn">deployable=false。绝对年化仅供内部复核，不上站、不接到 #/backtest、不改 display_policy。
股票池为当前可算股息率标的的历史回溯，存在幸存者偏差。</p>
<p class="note">样本期 {samp.get("beg")} ～ {samp.get("end_traded") or samp.get("end")}
（交易日 {samp.get("n_trading_days")} 天）。前复权收盘；MA=1000 交易日简单均线；
先曾溢价≥5% 再首次收盘≤均线，次日开盘买；止盈按持有期最高价、60 日收盘止损；
结束后须重新站上均线并再达 5% 溢价才再进；单边万三。
组合：当日持仓等权（每日再平衡），空仓收益 0，年化 = 净值^(252/N)-1，N 含空仓日。</p>
<table>
<thead><tr><th>止盈</th><th>样本数</th><th>命中</th><th>未命中</th><th>命中胜率</th>
<th>命中持有天数</th><th>未命中持有天数</th><th>含未达标平均收益</th>
<th>净值</th><th>年化(252，内部)</th></tr></thead>
<tbody>
{''.join(rows)}
</tbody>
</table>
<h2>资金占用净值（内部）</h2>
{svg}
<p class="note">蓝 2% / 橙 5% / 绿 10% / 红 15%。</p>
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def load_pool_and_klines(data_root=None):
    data_root = data_root or os.path.join(BASE, "data")
    snap = json.load(open(os.path.join(data_root, "snapshot.json"), encoding="utf-8"))
    items = snap.get("items") or []
    kline_dir = os.path.join(data_root, "kline")
    klines = {}
    for it in items:
        dy = it.get("dy")
        if dy is None:
            continue
        code = it.get("code")
        payload = read_series(os.path.join(kline_dir, code + ".json"))
        if payload and payload.get("bars"):
            klines[code] = payload["bars"]
    return items, klines, snap.get("date")


def main(argv=None):
    ap = argparse.ArgumentParser(description="MA1000 首次触及事件回测（研究，不上站）")
    ap.add_argument("--beg", default=SAMPLE_BEG)
    ap.add_argument("--end", default="")
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args(argv)
    items, klines, snap_date = load_pool_and_klines()
    end = args.end or None
    print("pool dy-not-null with kline: %d / snapshot date %s" % (len(klines), snap_date),
          flush=True)
    result = run_universe(items, klines, beg=args.beg, end=end)
    summary = write_artifacts(result, args.out)
    print("wrote %s" % args.out, flush=True)
    for t in summary["tiers"]:
        print("  止盈 %.0f%%  n=%d  hit=%.2f%%  avg_ret=%.2f%%  nav=%.4f  ann252=%.2f%%" % (
            t["target_pct"] * 100, t["n"],
            (t["hit_rate"] or 0) * 100,
            (t["avg_ret_all"] or 0) * 100,
            t["nav"] or 1,
            (t["ann_252"] or 0) * 100), flush=True)


if __name__ == "__main__":
    main()

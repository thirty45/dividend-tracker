# -*- coding: utf-8 -*-
"""写出回测 JSON 摘要、equity.csv、chart.html。"""
from __future__ import annotations

import csv
import json
import os
from typing import Dict, List, Optional, Sequence

from pipeline.backtest import COST_ONE_WAY, STRATEGY_NAME
from pipeline.backtest.metrics import start_sensitivity, summarize


def write_equity_csv(path: str, dates: Sequence[str], equity: Sequence[float],
                     n_holdings: Sequence[int], turnover: Sequence[float],
                     bench: Optional[Dict[str, Sequence[float]]] = None) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    extra = list((bench or {}).keys())
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "equity", "n_holdings", "turnover"] +
                   ["bench_%s" % k for k in extra])
        for i, d in enumerate(dates):
            row = [d, "%.8f" % equity[i], n_holdings[i], "%.8f" % turnover[i]]
            for k in extra:
                series = bench[k]
                v = series[i] if i < len(series) else ""
                row.append("%.8f" % v if isinstance(v, (int, float)) and v == v else "")
            w.writerow(row)


def write_summary_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def write_chart_html(path: str, dates: Sequence[str], equity: Sequence[float],
                     benches: Dict[str, Sequence[float]], title: str) -> None:
    """纯 HTML + 内联 SVG 折线（无外网依赖）。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    series = [("策略", list(equity))]
    for name, vals in benches.items():
        series.append((name, list(vals)))

    # 归一化画布
    width, height = 960, 420
    pad_l, pad_r, pad_t, pad_b = 56, 24, 36, 40
    usable_w = width - pad_l - pad_r
    usable_h = height - pad_t - pad_b

    all_vals = [v for _, s in series for v in s if isinstance(v, float) and v == v]
    if not all_vals or len(dates) < 2:
        ymin, ymax = 0.8, 1.2
    else:
        ymin, ymax = min(all_vals), max(all_vals)
        if ymax <= ymin:
            ymax = ymin + 1e-6
        pad = (ymax - ymin) * 0.05
        ymin, ymax = ymin - pad, ymax + pad

    def xy(i, v):
        x = pad_l + usable_w * i / max(len(dates) - 1, 1)
        y = pad_t + usable_h * (1.0 - (v - ymin) / (ymax - ymin))
        return x, y

    colors = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd"]
    paths = []
    legends = []
    for si, (name, vals) in enumerate(series):
        pts = []
        for i, v in enumerate(vals):
            if not isinstance(v, float) or v != v:
                continue
            x, y = xy(i, v)
            pts.append("%.1f,%.1f" % (x, y))
        if pts:
            paths.append(
                '<polyline fill="none" stroke="%s" stroke-width="2" points="%s"/>'
                % (colors[si % len(colors)], " ".join(pts)))
            legends.append(
                '<span style="color:%s;margin-right:16px">● %s</span>'
                % (colors[si % len(colors)], name))

    # y ticks
    y_ticks = []
    for k in range(5):
        v = ymin + (ymax - ymin) * k / 4.0
        x0 = pad_l
        y = pad_t + usable_h * (1.0 - k / 4.0)
        y_ticks.append(
            '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#eee"/>'
            '<text x="%d" y="%.1f" font-size="11" fill="#666" text-anchor="end">%.2f</text>'
            % (pad_l, y, width - pad_r, y, pad_l - 6, y + 4, v))

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<title>%s</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:24px;color:#222}
h1{font-size:20px;margin:0 0 8px}
.meta{color:#666;font-size:13px;margin-bottom:12px}
.legend{margin:8px 0 16px;font-size:13px}
</style></head><body>
<h1>%s</h1>
<div class="meta">成本：单边万三（%.4f%%），滑点 0；价格口径 bfq；幸存者偏差：当前 watchlist 回测历史</div>
<div class="legend">%s</div>
<svg width="%d" height="%d" style="border:1px solid #eee;background:#fafafa">
%s
%s
<text x="%d" y="%d" font-size="11" fill="#666">%s</text>
<text x="%d" y="%d" font-size="11" fill="#666" text-anchor="end">%s</text>
</svg>
</body></html>
""" % (
        title, title, COST_ONE_WAY * 100,
        "".join(legends), width, height,
        "".join(y_ticks), "".join(paths),
        pad_l, height - 12, dates[0] if dates else "",
        width - pad_r, height - 12, dates[-1] if dates else "",
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def build_report_payload(result, bench_series: Dict[str, List[float]],
                         bench_sources: Dict[str, str],
                         extra_meta: Optional[dict] = None) -> dict:
    def _filled(series):
        """把前导 None 之后的序列抽出，并与策略净值同步再定基。"""
        if not series:
            return None, None, None
        start_i = 0
        while start_i < len(series) and series[start_i] is None:
            start_i += 1
        if start_i >= len(series):
            return None, None, None
        dates = list(result.dates[start_i:])
        eq = [v / result.equity[start_i] for v in result.equity[start_i:]]
        # series 已相对自身首次非空定基；再切到同一 start_i
        b0 = series[start_i]
        bench = [v / b0 for v in series[start_i:]]
        return dates, eq, bench

    primary = bench_series.get("000922")
    # 000922 应全覆盖；若有 None 则对齐
    if primary and any(v is None for v in primary):
        d2, e2, b2 = _filled(primary)
        summary = summarize(
            d2 or result.dates, e2 or result.equity, result.turnover, result.n_holdings,
            bench_equity=b2)
    else:
        summary = summarize(
            result.dates, result.equity, result.turnover, result.n_holdings,
            bench_equity=primary)
    # 起点敏感性：全期 / 次年起 / 次两年 / 次三年
    if result.dates:
        y0 = int(result.dates[0][:4])
        starts = [
            ("全期", result.dates[0]),
            ("次年起", "%d-01-01" % (y0 + 1)),
            ("次两年", "%d-01-01" % (y0 + 2)),
            ("次三年", "%d-01-01" % (y0 + 3)),
        ]
    else:
        starts = []
    sens = start_sensitivity(result.dates, result.equity, starts)
    bench_summaries = {}
    from pipeline.backtest.metrics import excess_return, annualized_return
    for code, series in bench_series.items():
        d2, e2, b2 = _filled(series)
        if not d2 or not b2 or not e2:
            bench_summaries[code] = {"ok": False, "reason": "no_overlap"}
            continue
        bs = summarize(d2, b2, [0.0] * len(b2), [0] * len(b2))
        bs["ok"] = True
        bs["overlap_start"] = d2[0]
        bs["strategy_on_overlap"] = summarize(
            d2, e2, [0.0] * len(e2), [0] * len(e2))
        bs["strategy_excess_total"] = excess_return(e2, b2)
        bs["strategy_excess_ann"] = (
            bs["strategy_on_overlap"]["ann_return"] - bs["ann_return"])
        bench_summaries[code] = bs

    def _slim_rb(rb, keep=8):
        selected = list(rb.get("selected") or [])
        weights = rb.get("weights") or {}
        prices = rb.get("prices") or {}
        fill = rb.get("fill") or {}
        head = selected[:keep]
        return {
            "date": rb.get("date"),
            "signal_date": rb.get("signal_date"),
            "n": rb.get("n"),
            "selected_head": head,
            "selected_truncated": max(len(selected) - keep, 0),
            "weights_head": {c: weights.get(c) for c in head},
            "prices_head": {c: prices.get(c) for c in head},
            "fill_head": {c: fill.get(c) for c in head},
            "nav_before": rb.get("nav_before"),
            "turnover_notional": rb.get("turnover_notional"),
        }

    samples = result.rebalances[:2] + result.rebalances[-2:]
    # 去重（短回测可能首尾重叠）
    seen, slim = set(), []
    for rb in samples:
        key = rb.get("date")
        if key in seen:
            continue
        seen.add(key)
        slim.append(_slim_rb(rb))

    payload = {
        "strategy": STRATEGY_NAME,
        "meta": {**(result.meta or {}), **(extra_meta or {})},
        "cost": {"one_way": COST_ONE_WAY, "slippage": 0.0,
                 "note": "单边万三，滑点 0"},
        "summary": summary,
        "start_sensitivity": sens,
        "benchmarks": {
            "sources": bench_sources,
            "summaries": bench_summaries,
        },
        "rebalance_count": len(result.rebalances),
        "sample_rebalances": slim,
        "artifacts": {
            "equity_csv": "equity.csv",
            "chart_html": "chart.html",
            "rebalances_json": "rebalances.json",
            "note": "完整调仓明细见 rebalances.json（默认不入库）",
        },
    }
    return payload


def write_all(out_dir: str, result, bench_series: Dict[str, List[float]],
              bench_sources: Dict[str, str],
              extra_meta: Optional[dict] = None) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    payload = build_report_payload(result, bench_series, bench_sources, extra_meta)
    write_summary_json(os.path.join(out_dir, "summary.json"), payload)
    write_equity_csv(
        os.path.join(out_dir, "equity.csv"),
        result.dates, result.equity, result.n_holdings, result.turnover,
        bench=bench_series)
    write_chart_html(
        os.path.join(out_dir, "chart.html"),
        result.dates, result.equity, bench_series,
        title="%s vs 基准" % STRATEGY_NAME)
    # 调仓明细（便于验收核验）
    with open(os.path.join(out_dir, "rebalances.json"), "w", encoding="utf-8") as f:
        json.dump(result.rebalances, f, ensure_ascii=False, indent=2)
    return payload

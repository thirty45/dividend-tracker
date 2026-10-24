# -*- coding: utf-8 -*-
"""写出回测 JSON 摘要、equity.csv、chart.html。"""
from __future__ import annotations

import csv
import json
import os
from typing import Dict, List, Optional, Sequence

from pipeline.backtest import COST_ONE_WAY, STRATEGY_NAME
from pipeline.backtest.metrics import (
    annualized_return,
    excess_return,
    start_sensitivity,
    summarize,
    yearly_attribution,
)

DISPLAY_POLICY = {
    "deployable": False,
    "absolute_ann_return": "forbidden_for_public",
    "excess_vs_000922": "forbidden_for_public",
    "allowed": [
        "max_drawdown",
        "avg_holdings",
        "rebalance_count",
        "fill_stats",
        "bias_list",
        "yearly_attribution_internal",
        "start_sensitivity_with_excess_internal",
    ],
    "reason": (
        "幸存者偏差未修 + 主基准仍为价格指数(000922)，"
        "全收益官方码 H00922 行情不可用；禁止对外展示绝对年化与相对价指超额"
    ),
}


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
                     benches: Dict[str, Sequence[float]], title: str,
                     display_policy: Optional[dict] = None) -> None:
    """纯 HTML + 内联 SVG 折线（无外网依赖）。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    policy = display_policy or DISPLAY_POLICY
    series = [("策略", list(equity))]
    for name, vals in benches.items():
        series.append((name, list(vals)))

    width, height = 960, 420
    pad_l, pad_r, pad_t, pad_b = 56, 24, 36, 40
    usable_w = width - pad_l - pad_r
    usable_h = height - pad_t - pad_b

    all_vals = [v for _, s in series for v in s
                if isinstance(v, (int, float)) and v == v]
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
    paths, legends = [], []
    for si, (name, vals) in enumerate(series):
        pts = []
        for i, v in enumerate(vals):
            if not isinstance(v, (int, float)) or v != v:
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

    y_ticks = []
    for k in range(5):
        v = ymin + (ymax - ymin) * k / 4.0
        y = pad_t + usable_h * (1.0 - k / 4.0)
        y_ticks.append(
            '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#eee"/>'
            '<text x="%d" y="%.1f" font-size="11" fill="#666" text-anchor="end">%.2f</text>'
            % (pad_l, y, width - pad_r, y, pad_l - 6, y + 4, v))

    warn = (
        '<div class="warn"><b>display_policy</b>：deployable=%s；'
        'absolute_ann_return / excess_vs_000922 = <code>%s</code>。'
        '主基准 000922 为价格指数；全收益 H00922 暂无行情。'
        '图仅供内部研究，禁止把绝对年化或相对价指超额对外展示。</div>'
        % (policy.get("deployable"), policy.get("absolute_ann_return"))
    )

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<title>%s</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:24px;color:#222}
h1{font-size:20px;margin:0 0 8px}
.meta{color:#666;font-size:13px;margin-bottom:8px}
.warn{background:#fff3cd;border:1px solid #ffecb5;padding:10px 12px;font-size:13px;margin:8px 0 16px;line-height:1.5}
.legend{margin:8px 0 16px;font-size:13px}
code{background:#f5f5f5;padding:1px 4px}
</style></head><body>
<h1>%s</h1>
%s
<div class="meta">成本：单边万三（%.4f%%），滑点 0；价格口径 bfq；
幸存者偏差：当前 watchlist 回测历史；pool_sensitivity=untested</div>
<div class="legend">%s</div>
<svg width="%d" height="%d" style="border:1px solid #eee;background:#fafafa">
%s
%s
<text x="%d" y="%d" font-size="11" fill="#666">%s</text>
<text x="%d" y="%d" font-size="11" fill="#666" text-anchor="end">%s</text>
</svg>
</body></html>
""" % (
        title, title, warn, COST_ONE_WAY * 100,
        "".join(legends), width, height,
        "".join(y_ticks), "".join(paths),
        pad_l, height - 12, dates[0] if dates else "",
        width - pad_r, height - 12, dates[-1] if dates else "",
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def build_report_payload(result, bench_series: Dict[str, List[float]],
                         bench_meta: Dict[str, dict],
                         extra_meta: Optional[dict] = None) -> dict:
    def _filled(series):
        if not series:
            return None, None, None
        start_i = 0
        while start_i < len(series) and series[start_i] is None:
            start_i += 1
        if start_i >= len(series):
            return None, None, None
        dates = list(result.dates[start_i:])
        eq = [v / result.equity[start_i] for v in result.equity[start_i:]]
        b0 = series[start_i]
        bench = [v / b0 for v in series[start_i:]]
        return dates, eq, bench

    primary_key = "000922"
    # 若有可用全收益序列，优先作对比键（仍可能 excess_public=false 若未启用）
    if "H00922" in bench_series and any(
            v is not None for v in bench_series["H00922"]):
        primary_key = "H00922"

    primary = bench_series.get(primary_key) or bench_series.get("000922")
    if primary and any(v is None for v in primary):
        d2, e2, b2 = _filled(primary)
        summary = summarize(
            d2 or result.dates, e2 or result.equity,
            result.turnover[-len(d2):] if d2 else result.turnover,
            result.n_holdings[-len(d2):] if d2 else result.n_holdings,
            bench_equity=b2)
    else:
        summary = summarize(
            result.dates, result.equity, result.turnover, result.n_holdings,
            bench_equity=primary)

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

    # 主价指基准敏感性
    sens_price = start_sensitivity(
        result.dates, result.equity, starts,
        bench=bench_series.get("000922"), bench_key="000922")
    sens = {"primary_price_000922": sens_price}
    if "H00922" in bench_series:
        sens["total_return_H00922"] = start_sensitivity(
            result.dates, result.equity, starts,
            bench=bench_series.get("H00922"), bench_key="H00922")
    # 扁平一份给旧读者：用主价指
    sens_flat = dict(sens_price)

    yearly = yearly_attribution(
        result.dates, result.equity,
        bench=bench_series.get("000922"), bench_key="000922")
    yearly_tr = None
    if "H00922" in bench_series:
        yearly_tr = yearly_attribution(
            result.dates, result.equity,
            bench=bench_series.get("H00922"), bench_key="H00922")

    bench_summaries = {}
    for code, series in bench_series.items():
        d2, e2, b2 = _filled(series)
        meta = bench_meta.get(code) or {}
        if not d2 or not b2 or not e2:
            bench_summaries[code] = {
                "ok": False, "reason": "no_overlap", **meta}
            continue
        bs = summarize(d2, b2, [0.0] * len(b2), [0] * len(b2))
        bs["ok"] = True
        bs["overlap_start"] = d2[0]
        bs["strategy_on_overlap"] = summarize(
            d2, e2, [0.0] * len(e2), [0] * len(e2))
        bs["strategy_excess_total"] = excess_return(e2, b2)
        bs["strategy_excess_ann"] = (
            bs["strategy_on_overlap"]["ann_return"] - bs["ann_return"])
        bs.update({k: meta.get(k) for k in (
            "source", "benchmark_type", "role", "notes", "excess_public")})
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
            "fees": rb.get("fees"),
            "trades_n": len(rb.get("trades") or []),
        }

    samples = result.rebalances[:2] + result.rebalances[-2:]
    seen, slim = set(), []
    for rb in samples:
        key = rb.get("date")
        if key in seen:
            continue
        seen.add(key)
        slim.append(_slim_rb(rb))

    fills = dict(getattr(result, "fill_counts", {}) or {})
    fill_total = sum(fills.values()) or 1
    fill_stats = {
        "counts": fills,
        "share": {k: fills.get(k, 0) / fill_total for k in (
            "open", "close", "ffill", "missing")},
    }

    primary_meta = bench_meta.get("000922") or {}
    tr_meta = bench_meta.get("H00922") or {}
    has_tr = bool(tr_meta.get("benchmark_type") == "total_return"
                  and bench_series.get("H00922"))

    bias_list = [
        "survivorship: current_watchlist_applied_historically",
        "pool_sensitivity: untested",
        "primary_benchmark_000922: price_index (not total return)",
        "H00922 total-return feed unavailable in this environment"
        if not has_tr else "H00922 total-return available",
        "missing_kline codes excluded from universe",
    ]

    payload = {
        "strategy": STRATEGY_NAME,
        "display_policy": dict(DISPLAY_POLICY),
        "deployable": False,
        "pool_sensitivity": "untested",
        "meta": {**(result.meta or {}), **(extra_meta or {})},
        "cost": {"one_way": COST_ONE_WAY, "slippage": 0.0,
                 "note": "单边万三，滑点 0"},
        "benchmark_primary": {
            "code": "000922",
            "benchmark_type": primary_meta.get("benchmark_type") or "price_index",
            "excess_public": False,
            "source": primary_meta.get("source"),
            "notes": primary_meta.get("notes") or [],
            "total_return_code": "H00922",
            "total_return_available": has_tr,
            "total_return_meta": tr_meta,
        },
        "summary": summary,
        "start_sensitivity": sens_flat,
        "start_sensitivity_by_benchmark": sens,
        "yearly_attribution": yearly,
        "yearly_attribution_tr": yearly_tr,
        "fill_stats": fill_stats,
        "missing_kline_codes": list(
            (result.meta or {}).get("missing_kline_codes")
            or (extra_meta or {}).get("missing_kline_codes")
            or []),
        "bias_list": bias_list,
        "benchmarks": {
            "meta": bench_meta,
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
    # 内部数字保留在 summary，但加闸门注释
    payload["summary_note"] = (
        "summary.ann_return / excess_ann 仅供内部；"
        "display_policy 禁止对外展示"
    )
    return payload


def write_all(out_dir: str, result, bench_series: Dict[str, List[float]],
              bench_meta: Dict[str, dict],
              extra_meta: Optional[dict] = None) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    payload = build_report_payload(result, bench_series, bench_meta, extra_meta)
    write_summary_json(os.path.join(out_dir, "summary.json"), payload)
    write_equity_csv(
        os.path.join(out_dir, "equity.csv"),
        result.dates, result.equity, result.n_holdings, result.turnover,
        bench=bench_series)
    write_chart_html(
        os.path.join(out_dir, "chart.html"),
        result.dates, result.equity, bench_series,
        title="%s vs 基准（研究用，不可部署）" % STRATEGY_NAME,
        display_policy=payload["display_policy"])
    with open(os.path.join(out_dir, "rebalances.json"), "w", encoding="utf-8") as f:
        json.dump(result.rebalances, f, ensure_ascii=False, indent=2)
    return payload

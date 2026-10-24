# -*- coding: utf-8 -*-
"""入口：python -m pipeline.backtest [--limit N] [--beg 2018-01-01]"""
from __future__ import annotations

import argparse
import datetime
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from pipeline.backtest import STRATEGY_NAME
from pipeline.backtest.data_loader import (
    download_kline_raw,
    load_bars,
    load_dividends,
    load_or_fetch_benchmark,
    load_watchlist_codes,
    try_fill_missing_klines,
)
from pipeline.backtest.ma60_equal import benchmark_equity, run_backtest
from pipeline.backtest.report import write_all


def main(argv=None):
    p = argparse.ArgumentParser(description="红利池_MA60_等权 回测")
    p.add_argument("--beg", default="2018-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--limit", type=int, default=0,
                   help="仅用 watchlist 前 N 只（调试）；0=全量")
    p.add_argument("--skip-download", action="store_true")
    p.add_argument("--skip-fill-missing", action="store_true",
                   help="不尝试东财补 Pages 缺 K")
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--out", default=None,
                   help="输出目录，默认 data/backtest/红利池_MA60_等权")
    args = p.parse_args(argv)

    end = args.end or datetime.date.today().isoformat()
    out_dir = args.out or os.path.join(
        BASE, "data", "backtest", STRATEGY_NAME)

    codes = load_watchlist_codes()
    if args.limit and args.limit > 0:
        codes = codes[: args.limit]
        print("限制标的 %d 只" % len(codes))
    else:
        print("watchlist %d 只" % len(codes))

    if not args.skip_download:
        ok, fail = download_kline_raw(
            codes, workers=args.workers,
            progress=lambda m: print(m, flush=True))
        print("kline_raw ok=%d fail=%d" % (ok, fail))

    missing_codes = [
        c for c in codes
        if not os.path.isfile(os.path.join(BASE, "data", "kline_raw", "%s.json" % c))
        or os.path.getsize(os.path.join(BASE, "data", "kline_raw", "%s.json" % c)) < 50
    ]
    filled, still_missing, fill_note = [], list(missing_codes), ""
    if missing_codes and not args.skip_fill_missing:
        print("缺 K %d 只，尝试东财补..." % len(missing_codes), flush=True)
        filled, still_missing, fill_note = try_fill_missing_klines(
            missing_codes, progress=lambda m: print(m, flush=True))
        print("补上 %d，仍缺 %d（%s）" % (len(filled), len(still_missing), fill_note))

    bars_map = {}
    dividends_map = {}
    for code in codes:
        bars = load_bars(code)
        if not bars:
            continue
        bars_map[code] = bars
        dividends_map[code] = load_dividends(code)
    print("有K线 %d / 仍缺 %d" % (len(bars_map), len(still_missing)))

    print("跑回测 %s .. %s ..." % (args.beg, end), flush=True)
    result = run_backtest(
        bars_map, dividends_map, beg=args.beg, end=end, codes=list(bars_map.keys()))
    if not result.dates:
        print("无交易日，中止", result.meta)
        return 2

    bench_series = {}
    bench_meta = {}
    for bcode in ("H00922", "000922", "H30269", "H20269"):
        bbars, meta = load_or_fetch_benchmark(bcode, args.beg, end)
        print("基准 %s: %d 根 type=%s src=%s" % (
            bcode, len(bbars), meta.get("benchmark_type"), meta.get("source")))
        bench_meta[bcode] = meta
        if bbars:
            bench_series[bcode] = benchmark_equity(bbars, result.dates)

    coverage = len(bars_map) / float(len(codes) or 1)
    payload = write_all(
        out_dir, result, bench_series, bench_meta,
        extra_meta={
            "universe_size": len(bars_map),
            "watchlist_size": len(codes),
            "watchlist_limit": args.limit or None,
            "missing_kline": len(still_missing),
            "missing_kline_codes": still_missing,
            "missing_kline_filled": filled,
            "missing_kline_note": fill_note or "Pages 404；未启用补源",
            "kline_coverage": coverage,
            "pool_sensitivity": "untested",
            "note_survivorship": "使用当前 watchlist 回测历史，存在幸存者偏差",
            "note_no_history_constituents": (
                "无历史成分数据，不做池敏感性对照；deployable=false"
            ),
        })
    s = payload["summary"]
    print("完成 → %s" % out_dir)
    print("display_policy.deployable=%s absolute_ann=%s" % (
        payload["display_policy"]["deployable"],
        payload["display_policy"]["absolute_ann_return"]))
    print("过程指标 MDD=%.2f%% 持仓均值=%.1f 调仓=%d（年化/超额禁止对外）" % (
        (s.get("max_drawdown") or 0) * 100,
        s.get("avg_holdings") or 0,
        payload.get("rebalance_count") or 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

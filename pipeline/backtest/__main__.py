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

    bars_map = {}
    dividends_map = {}
    missing = 0
    for code in codes:
        bars = load_bars(code)
        if not bars:
            missing += 1
            continue
        bars_map[code] = bars
        dividends_map[code] = load_dividends(code)
    print("有K线 %d / 缺 %d" % (len(bars_map), missing))

    print("跑回测 %s .. %s ..." % (args.beg, end), flush=True)
    result = run_backtest(
        bars_map, dividends_map, beg=args.beg, end=end, codes=list(bars_map.keys()))
    if not result.dates:
        print("无交易日，中止", result.meta)
        return 2

    bench_series = {}
    bench_sources = {}
    for bcode in ("000922", "H30269"):
        bbars, src = load_or_fetch_benchmark(bcode, args.beg, end)
        print("基准 %s: %d 根 (%s)" % (bcode, len(bbars), src))
        if bbars:
            bench_series[bcode] = benchmark_equity(bbars, result.dates)
            bench_sources[bcode] = src
        else:
            bench_sources[bcode] = src or "unavailable"

    payload = write_all(
        out_dir, result, bench_series, bench_sources,
        extra_meta={
            "universe_size": len(bars_map),
            "watchlist_limit": args.limit or None,
            "missing_kline": missing,
            "note_survivorship": "使用当前 watchlist 回测历史，存在幸存者偏差",
        })
    s = payload["summary"]
    print("完成 → %s" % out_dir)
    print("全期年化=%.2f%% 最大回撤=%.2f%% 持仓均值=%.1f" % (
        (s.get("ann_return") or 0) * 100,
        (s.get("max_drawdown") or 0) * 100,
        s.get("avg_holdings") or 0))
    if s.get("excess_ann") is not None:
        print("相对000922年化超额=%.2f%%" % (s["excess_ann"] * 100))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""回测评价指标：年化、最大回撤、换手、超额、起点敏感性。"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


TRADING_DAYS_PER_YEAR = 252


def max_drawdown(equity: Sequence[float]) -> float:
    """最大回撤（正数比例，如 0.25 = 25%）。"""
    if not equity:
        return 0.0
    peak = equity[0]
    mdd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        if peak > 0:
            dd = 1.0 - v / peak
            if dd > mdd:
                mdd = dd
    return mdd


def annualized_return(equity: Sequence[float], n_days: Optional[int] = None) -> float:
    """按交易日数折算年化收益。equity[0] 为起点净值。"""
    if not equity or equity[0] <= 0:
        return 0.0
    days = n_days if n_days is not None else max(len(equity) - 1, 0)
    if days <= 0:
        return 0.0
    total = equity[-1] / equity[0]
    if total <= 0:
        return -1.0
    return total ** (TRADING_DAYS_PER_YEAR / float(days)) - 1.0


def excess_return(strategy: Sequence[float], benchmark: Sequence[float]) -> float:
    """全期总超额 = 策略总收益 - 基准总收益（非年化）。"""
    if not strategy or not benchmark or strategy[0] <= 0 or benchmark[0] <= 0:
        return 0.0
    return strategy[-1] / strategy[0] - benchmark[-1] / benchmark[0]


def align_series(dates: Sequence[str], values: Dict[str, float],
                 fill: Optional[float] = None) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    last = fill
    for d in dates:
        if d in values:
            last = values[d]
        out.append(last)
    return out


def slice_from(dates: Sequence[str], equity: Sequence[float],
               start_date: str) -> Tuple[List[str], List[float]]:
    for i, d in enumerate(dates):
        if d >= start_date:
            if equity[i] <= 0:
                continue
            base = equity[i]
            return list(dates[i:]), [v / base for v in equity[i:]]
    return [], []


def start_sensitivity(dates: Sequence[str], equity: Sequence[float],
                      starts: Sequence[Tuple[str, str]]) -> Dict[str, dict]:
    """starts: [(label, YYYY-MM-DD), ...]"""
    out = {}
    for label, start in starts:
        d2, e2 = slice_from(dates, equity, start)
        if len(e2) < 2:
            out[label] = {"start": start, "ok": False}
            continue
        out[label] = {
            "start": start,
            "ok": True,
            "end": d2[-1],
            "days": len(e2) - 1,
            "total_return": e2[-1] / e2[0] - 1.0,
            "ann_return": annualized_return(e2),
            "max_drawdown": max_drawdown(e2),
            "final_nav": e2[-1],
        }
    return out


def summarize(dates: Sequence[str], equity: Sequence[float],
              daily_turnover: Sequence[float],
              n_holdings: Sequence[int],
              bench_equity: Optional[Sequence[float]] = None) -> dict:
    ann = annualized_return(equity)
    mdd = max_drawdown(equity)
    avg_turn = (sum(daily_turnover) / len(daily_turnover)) if daily_turnover else 0.0
    # 年化换手近似：日均换手 * 252（单边合计买+卖 / 2 已在引擎记为单边）
    ann_turn = avg_turn * TRADING_DAYS_PER_YEAR
    avg_n = (sum(n_holdings) / len(n_holdings)) if n_holdings else 0.0
    out = {
        "start": dates[0] if dates else None,
        "end": dates[-1] if dates else None,
        "days": max(len(dates) - 1, 0),
        "final_nav": equity[-1] if equity else None,
        "total_return": (equity[-1] / equity[0] - 1.0) if equity and equity[0] else None,
        "ann_return": ann,
        "max_drawdown": mdd,
        "avg_daily_turnover": avg_turn,
        "ann_turnover": ann_turn,
        "avg_holdings": avg_n,
    }
    if bench_equity and equity and bench_equity[0] and equity[0]:
        out["bench_total_return"] = bench_equity[-1] / bench_equity[0] - 1.0
        out["bench_ann_return"] = annualized_return(bench_equity)
        out["bench_max_drawdown"] = max_drawdown(bench_equity)
        out["excess_total"] = excess_return(equity, bench_equity)
        out["excess_ann"] = ann - out["bench_ann_return"]
    return out

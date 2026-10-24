# -*- coding: utf-8 -*-
"""回测评价指标：年化、最大回撤、换手、超额、起点敏感性、年度归因。"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple


TRADING_DAYS_PER_YEAR = 252


def max_drawdown(equity: Sequence[float]) -> float:
    """最大回撤（正数比例，如 0.25 = 25%）。"""
    if not equity:
        return 0.0
    peak = equity[0]
    mdd = 0.0
    for v in equity:
        if v is None or not (v == v):
            continue
        if v > peak:
            peak = v
        if peak > 0:
            dd = 1.0 - v / peak
            if dd > mdd:
                mdd = dd
    return mdd


def annualized_return(equity: Sequence[float], n_days: Optional[int] = None) -> float:
    """按交易日数折算年化收益。equity[0] 为起点净值。"""
    if not equity or equity[0] is None or equity[0] <= 0:
        return 0.0
    days = n_days if n_days is not None else max(len(equity) - 1, 0)
    if days <= 0:
        return 0.0
    if equity[-1] is None or equity[-1] <= 0:
        return -1.0
    total = equity[-1] / equity[0]
    return total ** (TRADING_DAYS_PER_YEAR / float(days)) - 1.0


def excess_return(strategy: Sequence[float], benchmark: Sequence[float]) -> float:
    """全期总超额 = 策略总收益 - 基准总收益（非年化）。"""
    if (not strategy or not benchmark or strategy[0] is None or benchmark[0] is None
            or strategy[0] <= 0 or benchmark[0] <= 0):
        return 0.0
    return strategy[-1] / strategy[0] - benchmark[-1] / benchmark[0]


def slice_from(dates: Sequence[str], equity: Sequence[float],
               start_date: str) -> Tuple[List[str], List[float]]:
    for i, d in enumerate(dates):
        if d >= start_date:
            if equity[i] is None or equity[i] <= 0:
                continue
            base = equity[i]
            return list(dates[i:]), [v / base for v in equity[i:]]
    return [], []


def slice_pair(dates: Sequence[str], equity: Sequence[float],
               bench: Sequence[float], start_date: str
               ) -> Tuple[List[str], List[float], List[float]]:
    """策略与基准同步定基切片；跳过基准为 None 的前导。"""
    for i, d in enumerate(dates):
        if d < start_date:
            continue
        if equity[i] is None or equity[i] <= 0:
            continue
        if i >= len(bench) or bench[i] is None or bench[i] <= 0:
            continue
        e0, b0 = equity[i], bench[i]
        ds, es, bs = [], [], []
        for j in range(i, len(dates)):
            if equity[j] is None or j >= len(bench) or bench[j] is None:
                continue
            ds.append(dates[j])
            es.append(equity[j] / e0)
            bs.append(bench[j] / b0)
        return ds, es, bs
    return [], [], []


def start_sensitivity(dates: Sequence[str], equity: Sequence[float],
                      starts: Sequence[Tuple[str, str]],
                      bench: Optional[Sequence[float]] = None,
                      bench_key: str = "000922") -> Dict[str, dict]:
    """starts: [(label, YYYY-MM-DD), ...]。若给 bench，每档含 bench_ann / excess_ann。"""
    out = {}
    for label, start in starts:
        if bench is not None:
            d2, e2, b2 = slice_pair(dates, equity, bench, start)
            if len(e2) < 2:
                out[label] = {"start": start, "ok": False}
                continue
            s_ann = annualized_return(e2)
            b_ann = annualized_return(b2)
            out[label] = {
                "start": d2[0],
                "ok": True,
                "end": d2[-1],
                "days": len(e2) - 1,
                "total_return": e2[-1] / e2[0] - 1.0,
                "ann_return": s_ann,
                "max_drawdown": max_drawdown(e2),
                "final_nav": e2[-1],
                "bench_key": bench_key,
                "bench_ann_return": b_ann,
                "excess_ann": s_ann - b_ann,
                "bench_total_return": b2[-1] / b2[0] - 1.0,
                "excess_total": e2[-1] / e2[0] - b2[-1] / b2[0],
            }
        else:
            d2, e2 = slice_from(dates, equity, start)
            if len(e2) < 2:
                out[label] = {"start": start, "ok": False}
                continue
            out[label] = {
                "start": d2[0],
                "ok": True,
                "end": d2[-1],
                "days": len(e2) - 1,
                "total_return": e2[-1] / e2[0] - 1.0,
                "ann_return": e2 and annualized_return(e2),
                "max_drawdown": max_drawdown(e2),
                "final_nav": e2[-1],
            }
    return out


def yearly_attribution(dates: Sequence[str], equity: Sequence[float],
                       bench: Optional[Sequence[float]] = None,
                       bench_key: str = "000922") -> List[dict]:
    """日历年 first→last：strategy / bench / excess。"""
    if not dates or not equity:
        return []
    by_year: Dict[str, List[int]] = {}
    for i, d in enumerate(dates):
        by_year.setdefault(d[:4], []).append(i)
    rows = []
    for year in sorted(by_year.keys()):
        idxs = by_year[year]
        i0, i1 = idxs[0], idxs[-1]
        if equity[i0] is None or equity[i0] <= 0 or equity[i1] is None:
            continue
        s_ret = equity[i1] / equity[i0] - 1.0
        row = {
            "year": year,
            "start": dates[i0],
            "end": dates[i1],
            "strategy": s_ret,
        }
        if bench is not None and i1 < len(bench) and bench[i0] and bench[i1]:
            b_ret = bench[i1] / bench[i0] - 1.0
            row["bench_key"] = bench_key
            row["bench"] = b_ret
            row["excess"] = s_ret - b_ret
        rows.append(row)
    return rows


def summarize(dates: Sequence[str], equity: Sequence[float],
              daily_turnover: Sequence[float],
              n_holdings: Sequence[int],
              bench_equity: Optional[Sequence[float]] = None) -> dict:
    ann = annualized_return(equity)
    mdd = max_drawdown(equity)
    avg_turn = (sum(daily_turnover) / len(daily_turnover)) if daily_turnover else 0.0
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
    if (bench_equity and equity and bench_equity[0] is not None
            and equity[0] is not None and bench_equity[0] and equity[0]):
        out["bench_total_return"] = bench_equity[-1] / bench_equity[0] - 1.0
        out["bench_ann_return"] = annualized_return(bench_equity)
        out["bench_max_drawdown"] = max_drawdown(bench_equity)
        out["excess_total"] = excess_return(equity, bench_equity)
        out["excess_ann"] = ann - out["bench_ann_return"]
    return out


def reconstruct_nav_from_ledger(prev_cash: float, prev_holdings: Dict[str, float],
                                day: dict) -> float:
    """独立复算：昨现金/持仓 + 本日分红/成交/费用 → 今收盘净值。

    day 需含：cash_div, fees, trades[{code,delta_shares,px,fee}],
    holdings_after_corp, close_px。
    """
    cash = float(prev_cash) + float(day.get("cash_div") or 0.0)
    if "holdings_after_corp" in day:
        holdings = dict(day.get("holdings_after_corp") or {})
    else:
        holdings = dict(prev_holdings)
    for t in day.get("trades") or []:
        code = t["code"]
        delta = float(t["delta_shares"])
        px = float(t["px"])
        fee = float(t.get("fee") or 0.0)
        # 买入 delta>0：付出现金；卖出 delta<0：收回现金
        cash -= delta * px
        cash -= fee
        holdings[code] = (holdings.get(code) or 0.0) + delta
        if abs(holdings[code]) <= 1e-12:
            holdings.pop(code, None)
    close_px = day.get("close_px") or {}
    mkt = 0.0
    for code, shares in holdings.items():
        px = close_px.get(code)
        if px is None:
            continue
        mkt += shares * float(px)
    return cash + mkt

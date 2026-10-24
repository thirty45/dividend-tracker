# -*- coding: utf-8 -*-
"""策略：红利池_MA60_等权。

选股：站内 watchlist ∩ 不复权月末收盘 ≥ MA60(不复权)。
MA60 与站内 update_daily._month_end_series + 60 根月末均线一致；
**不**套用首页 year_low / premium / 近一年未跌破 等更严条件。

调仓：自然月末最后一个交易日收盘后重算；下一交易日开盘调仓；
无开盘价则用收盘并标注 fill=close。

持仓等权；停牌日用前收 forward-fill。
现金分红：ex_date 按 cash_per10/10 计入现金。
送转：按 profile 解析比例调股本。
成本：单边万三，滑点 0。
"""
from __future__ import annotations

import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from pipeline.update_daily import MA60_MONTHS, _month_end_series, _positive_month_closes
from pipeline.backtest import COST_ONE_WAY, STRATEGY_NAME
from pipeline.backtest.splits import cash_per_share, event_share_ratio


def bars_asof(bars: Sequence[dict], asof: str) -> List[dict]:
    """截取 asof（含）之前的日 K。"""
    out = []
    for b in bars:
        d = b.get("d") or b.get("date") or ""
        if d and d <= asof:
            out.append(b)
    return out


def month_end_ma60(bars: Sequence[dict], ma_months: int = MA60_MONTHS
                   ) -> Optional[Tuple[float, float, str]]:
    """返回 (月末收盘, MA60, ym)；数据不足返回 None。

    口径与站内 _positive_month_closes / screen 的 MA 窗口一致：
    最近 ma_months 根正数月末收盘的算术平均。
    """
    closes = _positive_month_closes(bars)
    if len(closes) < ma_months:
        return None
    window = closes[-ma_months:]
    ma = sum(c for _, c in window) / float(ma_months)
    ym, close = closes[-1]
    if ma <= 0 or close <= 0:
        return None
    return close, ma, ym


def select_close_ge_ma60(bars: Sequence[dict], asof: str,
                        ma_months: int = MA60_MONTHS) -> Optional[dict]:
    """asof 日（通常月末）收盘后判定：月末收盘 ≥ MA60。"""
    sub = bars_asof(bars, asof)
    stats = month_end_ma60(sub, ma_months=ma_months)
    if not stats:
        return None
    close, ma, ym = stats
    return {
        "ym": ym,
        "close": close,
        "ma60": ma,
        "selected": close >= ma,
        "premium_pct": (close / ma - 1.0) * 100.0,
    }


def last_trade_dates_by_month(calendar: Sequence[str]) -> List[str]:
    """每个自然月在日历中的最后一个交易日。"""
    last: Dict[str, str] = {}
    for d in calendar:
        last[d[:7]] = d
    return [last[ym] for ym in sorted(last.keys())]


def next_trade_day(calendar: Sequence[str], after: str) -> Optional[str]:
    for d in calendar:
        if d > after:
            return d
    return None


def build_calendar(all_bars: Dict[str, Sequence[dict]], beg: str, end: str
                   ) -> List[str]:
    """交易日历：取各标的交易日并集，再与 [beg,end] 求交。

    停牌用 ffill，日历用并集可避免「多数停牌日」被误删；
    实际定价时个股缺失会 ffill。
    """
    days = set()
    for bars in all_bars.values():
        for b in bars:
            d = b.get("d") or ""
            if beg <= d <= end:
                days.add(d)
    return sorted(days)


def index_bars(bars: Sequence[dict]) -> Dict[str, dict]:
    return {b["d"]: b for b in bars if b.get("d")}


def price_on(bar_index: Dict[str, dict], date: str, field: str = "c",
             ffill_state: Optional[dict] = None) -> Tuple[Optional[float], str]:
    """返回 (price, fill_tag)。fill_tag: open|close|ffill|missing。

    调仓优先 field='o'；若无开盘或开盘无效则退回收盘并标 fill=close。
    """
    b = bar_index.get(date)
    if b:
        if field == "o":
            o = b.get("o")
            c = b.get("c")
            try:
                o_f = float(o) if o is not None else None
            except (TypeError, ValueError):
                o_f = None
            if o_f is not None and o_f > 0:
                if ffill_state is not None:
                    ffill_state["px"] = o_f
                return o_f, "open"
            try:
                c_f = float(c) if c is not None else None
            except (TypeError, ValueError):
                c_f = None
            if c_f is not None and c_f > 0:
                if ffill_state is not None:
                    ffill_state["px"] = c_f
                return c_f, "close"
        else:
            try:
                c_f = float(b.get("c"))
            except (TypeError, ValueError):
                c_f = None
            if c_f is not None and c_f > 0:
                if ffill_state is not None:
                    ffill_state["px"] = c_f
                return c_f, "close"
    if ffill_state and ffill_state.get("px"):
        return ffill_state["px"], "ffill"
    return None, "missing"


class BacktestResult:
    def __init__(self):
        self.dates: List[str] = []
        self.equity: List[float] = []
        self.cash: List[float] = []
        self.n_holdings: List[int] = []
        self.turnover: List[float] = []  # 当日单边成交额 / 昨收盘净值
        self.rebalances: List[dict] = []
        self.daily: List[dict] = []
        self.ledger: List[dict] = []  # 独立守恒复算用
        self.fill_counts = {"open": 0, "close": 0, "ffill": 0, "missing": 0}
        self.meta: dict = {}


def run_backtest(
    bars_map: Dict[str, Sequence[dict]],
    dividends_map: Dict[str, Sequence[dict]],
    beg: str = "2018-01-01",
    end: Optional[str] = None,
    initial_cash: float = 1_000_000.0,
    cost_one_way: float = COST_ONE_WAY,
    ma_months: int = MA60_MONTHS,
    codes: Optional[Sequence[str]] = None,
) -> BacktestResult:
    """执行回测。bars_map / dividends_map 以 code 为键。"""
    end = end or datetime.date.today().isoformat()
    codes = list(codes) if codes is not None else sorted(bars_map.keys())
    # 截断并索引
    indexed: Dict[str, Dict[str, dict]] = {}
    ffill: Dict[str, dict] = {}
    for code in codes:
        bars = [b for b in (bars_map.get(code) or [])
                if (b.get("d") or "") <= end]
        indexed[code] = index_bars(bars)
        ffill[code] = {"px": None}

    # 分红索引: code -> {ex_date: [events]}
    div_index: Dict[str, Dict[str, List[dict]]] = {}
    for code in codes:
        by_day: Dict[str, List[dict]] = {}
        for ev in dividends_map.get(code) or []:
            ex = str(ev.get("ex_date") or "")[:10]
            if not ex:
                continue
            by_day.setdefault(ex, []).append(ev)
        div_index[code] = by_day

    # 调度用日历：向前多看一年，以便 beg 月初能吃到上月末信号
    lookback = "%04d-01-01" % (max(int(beg[:4]) - 1, 1990))
    sched_cal = build_calendar(
        {c: bars_map.get(c) or [] for c in codes}, lookback, end)
    calendar = [d for d in sched_cal if d >= beg]
    if not calendar:
        res = BacktestResult()
        res.meta = {"error": "empty_calendar", "beg": beg, "end": end}
        return res

    month_ends = last_trade_dates_by_month(sched_cal)
    # 调仓日 = 月末下一交易日；映射 rebalance_day -> signal_day
    rebalance_on: Dict[str, str] = {}
    for me in month_ends:
        nxt = next_trade_day(sched_cal, me)
        if nxt and beg <= nxt <= end:
            rebalance_on[nxt] = me

    cash = float(initial_cash)
    # holdings: code -> shares (float)
    holdings: Dict[str, float] = {}
    res = BacktestResult()
    res.meta = {
        "strategy": STRATEGY_NAME,
        "beg": beg,
        "end": end,
        "cost_one_way": cost_one_way,
        "slippage": 0.0,
        "price_basis": "bfq",
        "ma_months": ma_months,
        "selection": "watchlist ∩ month_end_close >= MA60(bfq)",
        "survivorship": "current_watchlist_applied_historically",
        "initial_cash": initial_cash,
    }

    prev_nav = initial_cash
    prev_cash = initial_cash
    prev_holdings: Dict[str, float] = {}
    prev_closes: Dict[str, float] = {}

    for di, date in enumerate(calendar):
        day_fees = 0.0
        day_trades: List[dict] = []
        day_turnover_notional = 0.0
        fill_tags: Dict[str, str] = {}

        # 1) 除息/送转（开盘前）——在昨收持仓上执行
        cash_div_total = 0.0
        for code, shares in list(holdings.items()):
            if shares <= 0:
                continue
            for ev in div_index.get(code, {}).get(date, []):
                cps = cash_per_share(ev)
                if cps:
                    cash_div_total += shares * cps
                ratio = event_share_ratio(ev)
                if ratio != 1.0:
                    holdings[code] = shares * ratio
                    shares = holdings[code]
        cash += cash_div_total
        holdings_after_corp = {c: s for c, s in holdings.items() if s > 1e-12}

        # 2) 调仓（开盘）
        if date in rebalance_on:
            signal_day = rebalance_on[date]
            selected = []  # (code, px, tag, stats)
            for code in codes:
                st = select_close_ge_ma60(
                    bars_map.get(code) or [], signal_day, ma_months=ma_months)
                if not (st and st["selected"]):
                    continue
                px, tag = price_on(indexed[code], date, "o", ffill[code])
                res.fill_counts[tag] = res.fill_counts.get(tag, 0) + 1
                if px is not None and px > 0:
                    selected.append((code, px, tag, st))

            pre_value = cash
            for code, shares in holdings.items():
                px, tag = price_on(indexed[code], date, "o", ffill[code])
                if px is None:
                    px, tag = price_on(indexed[code], date, "c", ffill[code])
                if px is not None:
                    pre_value += shares * px

            price_map = {c: p for c, p, _, _ in selected}
            for code, px, tag, _st in selected:
                fill_tags[code] = tag

            target_codes = set(price_map)
            n = len(selected)
            target_shares_map = {}
            if n > 0 and pre_value > 0:
                each = pre_value / float(n)
                for code, px in price_map.items():
                    target_shares_map[code] = each / px

            # 卖出：不在目标，或目标内减仓
            for code in list(holdings.keys()):
                cur = holdings.get(code) or 0.0
                if cur <= 0:
                    holdings.pop(code, None)
                    continue
                tgt = target_shares_map.get(code, 0.0)
                if code in target_codes:
                    sell_shares = max(cur - tgt, 0.0)
                else:
                    sell_shares = cur
                if sell_shares <= 1e-12:
                    continue
                px = price_map.get(code)
                tag = fill_tags.get(code)
                if px is None:
                    px, tag = price_on(indexed[code], date, "o", ffill[code])
                    if px is None:
                        px, tag = price_on(indexed[code], date, "c", ffill[code])
                    if tag:
                        res.fill_counts[tag] = res.fill_counts.get(tag, 0) + 1
                if px is None:
                    continue  # 停牌无法卖，保留
                notional = sell_shares * px
                fee = notional * cost_one_way
                cash += notional - fee
                day_fees += fee
                day_turnover_notional += notional
                day_trades.append({
                    "code": code, "delta_shares": -sell_shares,
                    "px": px, "fee": fee, "fill": tag,
                })
                left = cur - sell_shares
                if left <= 1e-10:
                    holdings.pop(code, None)
                else:
                    holdings[code] = left
                fill_tags[code] = tag

            # 买入 / 加仓
            buy_list = []
            for code, tgt in target_shares_map.items():
                cur = holdings.get(code) or 0.0
                delta = tgt - cur
                if delta > 1e-12:
                    buy_list.append((code, delta, price_map[code], fill_tags.get(code)))

            need = sum(d * p for _, d, p, _ in buy_list)
            total_need = need * (1.0 + cost_one_way)
            scale = 1.0
            if total_need > cash > 0:
                scale = cash / total_need
            elif cash <= 0:
                scale = 0.0

            for code, delta, px, tag in buy_list:
                d2 = delta * scale
                notional = d2 * px
                fee = notional * cost_one_way
                cash -= notional + fee
                day_fees += fee
                holdings[code] = (holdings.get(code) or 0.0) + d2
                day_turnover_notional += notional
                day_trades.append({
                    "code": code, "delta_shares": d2,
                    "px": px, "fee": fee, "fill": tag,
                })
                fill_tags[code] = tag

            holdings = {c: s for c, s in holdings.items() if s > 1e-10}

            weights = {}
            prices_out = {}
            for code, shares in holdings.items():
                px = price_map.get(code)
                if px is None:
                    continue
                prices_out[code] = px
                weights[code] = (shares * px) / max(pre_value, 1e-12)

            res.rebalances.append({
                "date": date,
                "signal_date": signal_day,
                "n": len(holdings),
                "selected": sorted(holdings.keys()),
                "weights": weights,
                "prices": prices_out,
                "fill": {c: fill_tags.get(c) for c in holdings},
                "nav_before": pre_value,
                "turnover_notional": day_turnover_notional,
                "fees": day_fees,
                "trades": day_trades,
            })

        # 3) 收盘估值
        mkt = 0.0
        n_hold = 0
        close_px: Dict[str, float] = {}
        for code, shares in holdings.items():
            px, tag = price_on(indexed[code], date, "c", ffill[code])
            if tag == "ffill":
                res.fill_counts["ffill"] = res.fill_counts.get("ffill", 0) + 1
            if px is None:
                continue
            close_px[code] = px
            mkt += shares * px
            n_hold += 1
        nav = cash + mkt
        if prev_nav > 0:
            day_ret = nav / prev_nav - 1.0
        else:
            day_ret = 0.0
        turn_ratio = (day_turnover_notional / prev_nav) if prev_nav > 0 else 0.0

        ledger_day = {
            "date": date,
            "nav": nav,
            "nav_prev": prev_nav,
            "cash": cash,
            "cash_prev": prev_cash,
            "cash_div": cash_div_total,
            "fees": day_fees,
            "trades": day_trades,
            "holdings_after_corp": dict(holdings_after_corp),
            "holdings_eod": dict(holdings),
            "close_px": dict(close_px),
            "prev_closes": dict(prev_closes),
            "rebalance": date in rebalance_on,
        }
        res.ledger.append(ledger_day)

        res.dates.append(date)
        res.equity.append(nav / initial_cash)
        res.cash.append(cash)
        res.n_holdings.append(n_hold)
        res.turnover.append(turn_ratio)
        res.daily.append({
            "date": date,
            "nav": nav,
            "equity": nav / initial_cash,
            "cash": cash,
            "mkt": mkt,
            "n": n_hold,
            "day_ret": day_ret,
            "turnover": turn_ratio,
            "rebalance": date in rebalance_on,
            "cash_div": cash_div_total,
            "fees": day_fees,
        })
        prev_nav = nav
        prev_cash = cash
        prev_holdings = dict(holdings)
        prev_closes = dict(close_px)

    res.meta["fill_stats"] = dict(res.fill_counts)
    return res




def benchmark_equity(bars: Sequence[dict], dates: Sequence[str]) -> List[float]:
    """按策略交易日对齐基准净值（起点=1），缺失 ffill。

    基准在策略起点之前尚无行情时，对应位置为 None（调用方做起点对齐）。
    """
    idx = index_bars(bars)
    out: List[Optional[float]] = []
    base = None
    last = None
    for d in dates:
        b = idx.get(d)
        px = None
        if b:
            try:
                px = float(b.get("c"))
            except (TypeError, ValueError):
                px = None
        if px and px > 0:
            last = px
        if last is None:
            out.append(None)
            continue
        if base is None:
            base = last
        out.append(last / base)
    return out  # type: ignore[return-value]

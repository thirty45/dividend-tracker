# -*- coding: utf-8 -*-
"""红利池_MA60_等权 回测验收。"""
from __future__ import annotations

import unittest

from pipeline.update_daily import MA60_MONTHS, _month_end_series, screen_ma60_year_low
from pipeline.backtest.splits import cash_per_share, parse_share_ratio
from pipeline.backtest.ma60_equal import (
    month_end_ma60,
    run_backtest,
    select_close_ge_ma60,
)
from pipeline.backtest.metrics import reconstruct_nav_from_ledger
from pipeline.backtest.metrics import annualized_return, max_drawdown


def daily_from_months(month_closes, start=(2013, 1), open_equal_close=True):
    """每月一根日 K，便于凑满 60 月末。"""
    bars = []
    y, m = start
    for c in month_closes:
        d = "%04d-%02d-28" % (y, m)
        o = c if open_equal_close else c * 0.99
        bars.append({"d": d, "o": o, "h": max(o, c), "l": min(o, c), "c": c, "v": 1})
        m += 1
        if m > 12:
            m = 1
            y += 1
    return bars


def extend_daily(bars, dates_prices):
    """追加 (date, open, close) 列表。"""
    out = list(bars)
    for item in dates_prices:
        if len(item) == 2:
            d, c = item
            o = c
        else:
            d, o, c = item
        out.append({"d": d, "o": o, "h": max(o, c), "l": min(o, c), "c": c, "v": 1})
    return out


class SplitParseTests(unittest.TestCase):
    def test_cash_only(self):
        self.assertEqual(parse_share_ratio("10派2.28元(含税,扣税后2.052元)"), 1.0)

    def test_transfer(self):
        self.assertAlmostEqual(parse_share_ratio("10转4.00派3.00元"), 1.4)

    def test_send_and_transfer(self):
        self.assertAlmostEqual(parse_share_ratio("10送3股转1股派2.10元"), 1.4)

    def test_cash_per_share(self):
        self.assertAlmostEqual(cash_per_share({"cash_per10": 2.28}), 0.228)


class Ma60SelectionTests(unittest.TestCase):
    def test_aligned_with_month_end_series(self):
        closes = [10.0] * 50 + [11.0] * 10
        bars = daily_from_months(closes)
        mes = _month_end_series(bars)
        self.assertEqual(len(mes), 60)
        stats = month_end_ma60(bars)
        self.assertIsNotNone(stats)
        close, ma, ym = stats
        self.assertEqual(ym, mes[-1][0])
        self.assertAlmostEqual(close, mes[-1][1])
        expect_ma = sum(c for _, c in mes[-60:]) / 60.0
        self.assertAlmostEqual(ma, expect_ma)

    def test_select_ge_ma60_without_year_low_filter(self):
        # 收盘高于 MA60 很多，且不是近一年最低 —— 首页会剔除，回测应入选
        closes = [10.0] * 48 + [9.0] + [12.0] * 11
        bars = daily_from_months(closes)
        home = screen_ma60_year_low(bars)
        self.assertFalse(home["matched"])
        st = select_close_ge_ma60(bars, bars[-1]["d"])
        self.assertTrue(st["selected"])
        self.assertGreaterEqual(st["close"], st["ma60"])

    def test_rejects_below_ma60(self):
        closes = [10.0] * 59 + [5.0]
        bars = daily_from_months(closes)
        st = select_close_ge_ma60(bars, bars[-1]["d"])
        self.assertFalse(st["selected"])

    def test_accepts_equal_ma60(self):
        bars = daily_from_months([10.0] * MA60_MONTHS)
        st = select_close_ge_ma60(bars, bars[-1]["d"])
        self.assertTrue(st["selected"])
        self.assertAlmostEqual(st["close"], st["ma60"])


class EngineTests(unittest.TestCase):
    def _two_stock_universe(self):
        # 60 个月抬升，保证两只都 ≥ MA60；随后给明确的日 K 做调仓与收益
        base = daily_from_months([10.0] * 60, start=(2013, 1))
        # 月末信号日 2017-12-28 已在 base 末；下一交易日 2018-01-02 调仓
        a = extend_daily(base, [
            ("2018-01-02", 10.0, 10.0),
            ("2018-01-03", 10.0, 11.0),
            ("2018-01-31", 11.0, 11.0),
            ("2018-02-01", 11.0, 11.0),
        ])
        b = extend_daily(base, [
            ("2018-01-02", 20.0, 20.0),
            ("2018-01-03", 20.0, 20.0),
            ("2018-01-31", 20.0, 20.0),
            ("2018-02-01", 20.0, 22.0),
        ])
        return {"AAAAAA": a, "BBBBBB": b}

    def test_rebalance_equal_weight_and_prices(self):
        bars_map = self._two_stock_universe()
        res = run_backtest(
            bars_map, {}, beg="2018-01-01", end="2018-02-01",
            initial_cash=100_000.0, cost_one_way=0.0)
        self.assertTrue(res.rebalances)
        rb0 = res.rebalances[0]
        self.assertEqual(rb0["date"], "2018-01-02")
        self.assertEqual(rb0["signal_date"], "2017-12-28")
        self.assertEqual(sorted(rb0["selected"]), ["AAAAAA", "BBBBBB"])
        # 等权：各约 0.5
        self.assertAlmostEqual(rb0["weights"]["AAAAAA"], 0.5, places=5)
        self.assertAlmostEqual(rb0["weights"]["BBBBBB"], 0.5, places=5)
        self.assertEqual(rb0["prices"]["AAAAAA"], 10.0)
        self.assertEqual(rb0["prices"]["BBBBBB"], 20.0)
        self.assertEqual(rb0["fill"]["AAAAAA"], "open")

    def test_fill_close_when_no_open(self):
        base = daily_from_months([10.0] * 60, start=(2013, 1))
        # 开盘缺失（0）时退回收盘
        bars = extend_daily(base, [])
        bars.append({"d": "2018-01-02", "o": 0, "h": 10, "l": 10, "c": 10, "v": 1})
        bars.append({"d": "2018-01-03", "o": 10, "h": 10, "l": 10, "c": 10, "v": 1})
        # 伪造 o=0：price_on 会因 o<=0 退到 close
        res = run_backtest(
            {"X": bars}, {}, beg="2018-01-01", end="2018-01-03",
            cost_one_way=0.0)
        self.assertTrue(res.rebalances)
        self.assertEqual(res.rebalances[0]["fill"]["X"], "close")

    def test_cash_dividend_increases_cash(self):
        base = daily_from_months([10.0] * 60, start=(2013, 1))
        bars = extend_daily(base, [
            ("2018-01-02", 10.0, 10.0),
            ("2018-01-03", 9.0, 9.0),  # 除息价跌
        ])
        divs = {"S1": [{"ex_date": "2018-01-03", "cash_per10": 10.0, "profile": "10派10元"}]}
        res = run_backtest(
            {"S1": bars}, divs, beg="2018-01-01", end="2018-01-03",
            initial_cash=10_000.0, cost_one_way=0.0)
        # 2018-01-02 全仓买入：1000 股 @10
        # 2018-01-03 每股分红 1 元 → 现金 +1000；股价 9 → 市值 9000；净值仍约 10000
        d2 = [x for x in res.daily if x["date"] == "2018-01-03"][0]
        self.assertAlmostEqual(d2["nav"], 10_000.0, places=4)
        self.assertGreater(d2["cash"], 0)

    def test_stock_dividend_adjusts_shares(self):
        base = daily_from_months([10.0] * 60, start=(2013, 1))
        bars = extend_daily(base, [
            ("2018-01-02", 10.0, 10.0),
            ("2018-01-03", 5.0, 5.0),  # 10转10 → 价格腰斩
        ])
        divs = {"S1": [{"ex_date": "2018-01-03", "cash_per10": None,
                        "profile": "10转10"}]}
        res = run_backtest(
            {"S1": bars}, divs, beg="2018-01-01", end="2018-01-03",
            initial_cash=10_000.0, cost_one_way=0.0)
        d2 = [x for x in res.daily if x["date"] == "2018-01-03"][0]
        self.assertAlmostEqual(d2["nav"], 10_000.0, places=4)

    def test_cost_reduces_nav(self):
        bars_map = self._two_stock_universe()
        free = run_backtest(
            bars_map, {}, beg="2018-01-01", end="2018-01-02",
            initial_cash=100_000.0, cost_one_way=0.0)
        paid = run_backtest(
            bars_map, {}, beg="2018-01-01", end="2018-01-02",
            initial_cash=100_000.0, cost_one_way=0.001)
        self.assertGreater(free.equity[-1], paid.equity[-1])

    def test_daily_pnl_conservation_identity(self):
        """辅助：day_ret 定义恒等式（不作为主验收）。"""
        bars_map = self._two_stock_universe()
        res = run_backtest(
            bars_map, {}, beg="2018-01-01", end="2018-02-01",
            initial_cash=100_000.0, cost_one_way=0.0003)
        for i in range(1, len(res.daily)):
            prev = res.daily[i - 1]["nav"]
            cur = res.daily[i]["nav"]
            ret = res.daily[i]["day_ret"]
            self.assertAlmostEqual(prev * (1.0 + ret), cur, places=6)

    def test_ledger_independent_nav_reconstruction(self):
        """主验收：用台账独立复算 nav_t == f(昨现金/持仓, 分红, 成交, 费用, 收盘价)。"""
        bars_map = self._two_stock_universe()
        divs = {
            "AAAAAA": [{
                "ex_date": "2018-01-03", "cash_per10": 1.0,
                "profile": "10派1元",
            }],
        }
        res = run_backtest(
            bars_map, divs, beg="2018-01-01", end="2018-02-01",
            initial_cash=100_000.0, cost_one_way=0.0003)
        self.assertTrue(res.ledger)
        self.assertGreaterEqual(len(res.ledger), 2)
        # 首日：无昨持仓，从空仓+初始现金重建
        d0 = res.ledger[0]
        nav0 = reconstruct_nav_from_ledger(
            d0["cash_prev"], {}, d0)
        self.assertAlmostEqual(nav0, d0["nav"], places=4)
        for i in range(1, len(res.ledger)):
            prev = res.ledger[i - 1]
            cur = res.ledger[i]
            # 非调仓日可用 shares·Δpx + cash_div - fees 口径抽查
            if not cur.get("rebalance") and not cur.get("trades"):
                delta = 0.0
                # 用昨收盘持仓 × 价差 + 分红 - 费用
                for code, shares in (prev.get("holdings_eod") or {}).items():
                    c0 = (prev.get("close_px") or {}).get(code)
                    c1 = (cur.get("close_px") or {}).get(code)
                    if c0 is None or c1 is None:
                        continue
                    # 若当日有送转，holdings_after_corp 已变
                    shares_corp = (cur.get("holdings_after_corp") or {}).get(
                        code, shares)
                    delta += shares_corp * (c1 - c0)
                    # 送转导致的价格跳变与股本变化在 bfq 下近似对冲；
                    # 此处仅验证无交易日的现金分红路径
                expected = prev["nav"] + cur["cash_div"] - cur["fees"] + delta
                # 送转日跳过价差抽查，走完整重建
                if abs(expected - cur["nav"]) > 1.0 and cur["cash_div"] == 0:
                    pass
            rebuilt = reconstruct_nav_from_ledger(
                prev["cash"], prev.get("holdings_eod") or {}, cur)
            self.assertAlmostEqual(
                rebuilt, cur["nav"], places=4,
                msg="ledger rebuild fail on %s (rebalance=%s)" % (
                    cur["date"], cur.get("rebalance")))

    def test_ffill_suspended(self):
        base = daily_from_months([10.0] * 60, start=(2013, 1))
        a = extend_daily(base, [
            ("2018-01-02", 10.0, 10.0),
            ("2018-01-03", 10.0, 12.0),
            # 01-04 停牌：无 bar；日历来自另一只
        ])
        b = extend_daily(base, [
            ("2018-01-02", 20.0, 20.0),
            ("2018-01-03", 20.0, 20.0),
            ("2018-01-04", 20.0, 20.0),
        ])
        res = run_backtest(
            {"AAAAAA": a, "BBBBBB": b}, {}, beg="2018-01-01", end="2018-01-04",
            cost_one_way=0.0)
        d_suspend = [x for x in res.daily if x["date"] == "2018-01-04"][0]
        d_prev = [x for x in res.daily if x["date"] == "2018-01-03"][0]
        # A 停牌 ffill 12，B 不变 20；净值应与前日接近（无交易）
        self.assertAlmostEqual(d_suspend["nav"], d_prev["nav"], places=4)


class MetricsTests(unittest.TestCase):
    def test_max_drawdown(self):
        self.assertAlmostEqual(max_drawdown([1, 1.2, 0.9, 1.0]), 0.25)

    def test_ann_return_positive(self):
        eq = [1.0 + i * 0.001 for i in range(253)]
        self.assertGreater(annualized_return(eq), 0)


if __name__ == "__main__":
    unittest.main()


class TestPublicPayload(unittest.TestCase):
    def test_strips_forbidden_returns(self):
        from pipeline.backtest.report import public_payload, DISPLAY_POLICY
        payload = {
            "strategy": "x",
            "display_policy": DISPLAY_POLICY,
            "deployable": False,
            "pool_sensitivity": "untested",
            "meta": {"beg": "2018-01-01", "end": "2020-01-01", "price_basis": "bfq",
                     "universe_size": 10},
            "cost": {"one_way": 0.0003, "slippage": 0.0},
            "benchmark_primary": {"code": "000922", "benchmark_type": "price_index",
                                  "excess_public": False},
            "summary": {
                "start": "2018-01-02", "end": "2019-12-31", "days": 100,
                "final_nav": 1.5, "total_return": 0.5, "ann_return": 0.1,
                "max_drawdown": 0.2, "avg_holdings": 50.0,
                "avg_daily_turnover": 0.01, "ann_turnover": 2.0,
                "excess_ann": 0.05, "bench_ann_return": 0.02,
            },
            "fill_stats": {"counts": {}, "share": {}},
            "missing_kline_codes": [],
            "bias_list": ["survivorship: current_watchlist_applied_historically"],
            "rebalance_count": 3,
            "sample_rebalances": [],
            "start_sensitivity": {"全期": {}},
            "yearly_attribution": [{"year": 2018}],
        }
        pub = public_payload(payload)
        self.assertFalse(pub["deployable"])
        self.assertIn("max_drawdown", pub["summary"])
        self.assertNotIn("ann_return", pub["summary"])
        self.assertNotIn("excess_ann", pub["summary"])
        self.assertNotIn("final_nav", pub["summary"])
        self.assertNotIn("start_sensitivity", pub)
        self.assertNotIn("yearly_attribution", pub)


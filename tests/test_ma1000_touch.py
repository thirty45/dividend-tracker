"""MA1000 首次触及事件回测：入场、止盈触达/未触达、再入场与净值年化。"""
import unittest

from pipeline.backtest.ma1000_touch import (
    COST_ONE_WAY,
    annualize,
    equity_curve,
    simulate_stock,
)


def bars_from(rows, start="2018-01-01"):
    """rows: iterable of close, or dicts with o/h/l/c. 日期按日顺延。"""
    import datetime
    day = datetime.date.fromisoformat(start)
    bars = []
    for row in rows:
        if isinstance(row, dict):
            o = row.get("o", row.get("c"))
            h = row.get("h", max(o, row.get("c"), row.get("l", o)))
            low = row.get("l", min(o, row.get("c"), h))
            c = row["c"]
        else:
            o = h = low = c = float(row)
        bars.append({"d": day.isoformat(), "o": o, "h": h, "l": low, "c": c, "v": 1})
        day += datetime.timedelta(days=1)
    return bars


def ramp_to_signal(ma_days=5, arm_close=12.0, touch_close=9.0):
    """前 ma_days 根收 10；再一根收 arm_close 完成武装；再一根收 touch_close 给出信号。"""
    return [10.0] * ma_days + [arm_close, touch_close]


class Ma1000TouchTests(unittest.TestCase):
    def test_no_signal_without_prior_five_percent_premium(self):
        # 一直贴着均线或小幅溢价后跌破，未达 5%，不应入场。
        ma_days = 5
        closes = [10.0] * ma_days + [10.2, 9.9, 10.1, 9.8]
        trades = simulate_stock(bars_from(closes), 0.02, ma_days=ma_days)
        self.assertEqual(trades, [])

    def test_signal_then_buy_next_open_not_signal_close(self):
        ma_days = 5
        # i=0..4: 10；i=5: 12 武装；i=6: 9 信号；i=7 开盘 8.5 买入。
        prefix = ramp_to_signal(ma_days)
        rest = [{"o": 8.5, "h": 8.5, "l": 8.5, "c": 8.5}] + [8.5] * 60
        trades = simulate_stock(bars_from(prefix + rest), 0.50, ma_days=ma_days)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["entry_px"], 8.5)
        self.assertNotEqual(trades[0]["entry_px"], 9.0)
        # 信号日是 touch close=9 那天，入场为次日
        self.assertLess(trades[0]["signal_date"], trades[0]["entry_date"])

    def test_take_profit_hits_on_holding_high_at_target_price(self):
        ma_days = 5
        prefix = ramp_to_signal(ma_days)
        # 入场开盘 10，目标 2%=10.2；当日最高 11，应按 10.2 出，同日完成。
        entry = {"o": 10.0, "h": 11.0, "l": 9.8, "c": 10.5}
        trades = simulate_stock(bars_from(prefix + [entry]), 0.02, ma_days=ma_days)
        self.assertEqual(len(trades), 1)
        self.assertTrue(trades[0]["hit"])
        self.assertEqual(trades[0]["exit_px"], 10.2)
        self.assertEqual(trades[0]["hold_days"], 1)
        cost = COST_ONE_WAY
        expected = (10.2 * (1 - cost)) / (10.0 * (1 + cost)) - 1
        self.assertAlmostEqual(trades[0]["ret"], expected, places=10)

    def test_time_stop_exits_on_day_60_close_when_high_never_reaches(self):
        ma_days = 5
        prefix = ramp_to_signal(ma_days)
        # 入场开盘 10，目标 10.2；之后 60 根最高只到 10.1，第 60 根收 9.5。
        hold = [{"o": 10.0, "h": 10.1, "l": 9.4, "c": 9.7}]
        hold += [{"o": 9.7, "h": 10.1, "l": 9.4, "c": 9.7}] * 58
        hold += [{"o": 9.7, "h": 10.1, "l": 9.4, "c": 9.5}]
        trades = simulate_stock(bars_from(prefix + hold), 0.02, ma_days=ma_days)
        self.assertEqual(len(trades), 1)
        self.assertFalse(trades[0]["hit"])
        self.assertFalse(trades[0]["censored"])
        self.assertEqual(trades[0]["hold_days"], 60)
        self.assertEqual(trades[0]["exit_px"], 9.5)
        cost = COST_ONE_WAY
        expected = (9.5 * (1 - cost)) / (10.0 * (1 + cost)) - 1
        self.assertAlmostEqual(trades[0]["ret"], expected, places=10)

    def test_no_reentry_until_close_back_above_ma_and_rearmed(self):
        ma_days = 5
        prefix = ramp_to_signal(ma_days)
        # 第一笔：入场日即触达 2% 止盈。
        first_entry = {"o": 10.0, "h": 11.0, "l": 9.8, "c": 9.0}  # 收盘仍在均线下方
        # 随后继续在均线下方再创新低，不可立刻再进。
        below = [{"o": 9.0, "h": 9.1, "l": 8.5, "c": 8.8}] * 8
        trades = simulate_stock(bars_from(prefix + [first_entry] + below), 0.02, ma_days=ma_days)
        self.assertEqual(len(trades), 1)
        self.assertTrue(trades[0]["hit"])

    def test_second_entry_after_reclaim_and_five_percent_arm(self):
        ma_days = 5
        # 造两段独立的「武装后触及」。
        # 段1：10*5, 12, 9 信号，次日开盘 10、最高 11 止盈，收盘 9（仍在下方）
        prefix = [10.0] * ma_days + [12.0, 9.0]
        first_entry = {"o": 10.0, "h": 11.0, "l": 9.0, "c": 9.0}
        # 站上并再武装：连续抬高收盘，再跌破。
        # 需要足够的高收盘让溢价≥5%，再给一根低收盘。
        recover = [11.0, 12.0, 12.0, 12.0, 12.0, 12.0]
        touch2 = 8.0
        second_entry = {"o": 10.0, "h": 11.0, "l": 9.0, "c": 10.0}
        rows = prefix + [first_entry] + recover + [touch2, second_entry]
        trades = simulate_stock(bars_from(rows), 0.02, ma_days=ma_days)
        self.assertEqual(len(trades), 2)
        self.assertTrue(all(t["hit"] for t in trades))
        self.assertLess(trades[0]["exit_date"], trades[1]["entry_date"])

    def test_equity_annualization_uses_occupancy_not_per_trade_gross_up(self):
        # 一笔持有 2 天、收益约 2%（忽略成本近似），夹在前后空仓日中间。
        # 若错误地用 (1+avg_ret)^(252/hold)-1 会得到极高年化；
        # 正确口径把空仓日算进 N。
        trades = [{
            "daily": {"2018-01-03": 0.01, "2018-01-04": 0.01},
            "ret": 1.01 * 1.01 - 1,
            "hit": True,
            "hold_days": 2,
        }]
        calendar = ["2018-01-0%d" % d for d in range(1, 6)]  # 5 个交易日，3 天空仓
        series = equity_curve(trades, calendar)
        self.assertEqual(len(series), 5)
        by = {p["date"]: p for p in series}
        self.assertEqual(by["2018-01-01"]["ret"], 0.0)
        self.assertAlmostEqual(by["2018-01-03"]["ret"], 0.01)
        self.assertAlmostEqual(by["2018-01-04"]["ret"], 0.01)
        self.assertEqual(by["2018-01-05"]["ret"], 0.0)
        self.assertAlmostEqual(series[-1]["nav"], 1.01 * 1.01)
        nav = series[-1]["nav"]
        ann = annualize(nav, n_trading_days=5)
        naive = nav ** (252 / 2) - 1  # 错误：只按持有天数
        self.assertLess(ann["ann_252"], naive)
        self.assertAlmostEqual(ann["ann_252"], nav ** (252 / 5) - 1)


if __name__ == "__main__":
    unittest.main()

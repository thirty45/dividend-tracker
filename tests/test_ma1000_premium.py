"""1000 日均溢价 0–5% 筛选（前复权口径由调用方传入日 K）。

筛选本身不区分复权：传入哪套日 K，收盘、MA1000 和溢价就用哪套。
每日流水线传入前复权日 K（qfq，data/kline），payload 的 price_basis 为 "qfq"。
"""
import unittest

from pipeline.update_daily import (
    MA1000_DAYS,
    build_ma1000_premium_payload,
    screen_ma1000_premium,
)


def day_bars(closes, start="2018-01-01"):
    """每个交易日一根收盘，日期按自然日顺延（仅用于构造根数，不代表日历均线）。"""
    import datetime
    bars = []
    day = datetime.date.fromisoformat(start)
    for close in closes:
        iso = day.isoformat()
        bars.append({"d": iso, "o": close, "h": close, "l": close,
                     "c": close, "v": 1})
        day += datetime.timedelta(days=1)
    return bars


def flat(n, price=10.0):
    return [price] * n


class Ma1000PremiumTests(unittest.TestCase):
    def test_needs_one_thousand_day_closes(self):
        self.assertIsNone(screen_ma1000_premium(day_bars(flat(MA1000_DAYS - 1))))

    def test_accepts_premium_exactly_five_percent(self):
        # 前 999 根为 10，最后一根 C 使 C = 1.05 × MA1000。
        # C = 1.05 * (999*10 + C) / 1000
        # 1000 C = 1.05 * 9990 + 1.05 C
        # 1000 C - 1.05 C = 10489.5
        # C = 10489.5 / 998.95
        close = 10489.5 / 998.95
        stats = screen_ma1000_premium(day_bars(flat(999) + [close]))
        self.assertIsNotNone(stats)
        self.assertTrue(stats["matched"])
        self.assertAlmostEqual(stats["premium_pct"], 5.0, places=2)

    def test_accepts_premium_exactly_zero(self):
        stats = screen_ma1000_premium(day_bars(flat(MA1000_DAYS, 10.0)))
        self.assertIsNotNone(stats)
        self.assertTrue(stats["matched"])
        self.assertEqual(stats["premium_pct"], 0)
        self.assertEqual(stats["close"], 10.0)
        self.assertEqual(stats["ma1000"], 10.0)

    def test_rejects_negative_premium(self):
        stats = screen_ma1000_premium(day_bars(flat(999, 10.0) + [9.9]))
        self.assertIsNotNone(stats)
        self.assertFalse(stats["matched"])
        self.assertLess(stats["premium_pct"], 0)

    def test_rejects_premium_above_five_percent(self):
        stats = screen_ma1000_premium(day_bars(flat(999, 10.0) + [11.0]))
        self.assertIsNotNone(stats)
        self.assertFalse(stats["matched"])
        self.assertGreater(stats["premium_pct"], 5)

    def test_missing_latest_close_returns_none(self):
        bars = day_bars(flat(MA1000_DAYS))
        bars[-1]["c"] = None
        self.assertIsNone(screen_ma1000_premium(bars))

    def test_payload_keeps_hits_only_and_sorts_by_yield(self):
        exact0 = day_bars(flat(MA1000_DAYS, 10.0))
        close5 = 10489.5 / 998.95
        exact5 = day_bars(flat(999) + [close5])
        below = day_bars(flat(999, 10.0) + [9.9])
        short = day_bars(flat(500))
        items = [
            {"code": "000002", "name": "乙", "dy": 3.1},
            {"code": "000001", "name": "甲", "dy": 8.8},
            {"code": "000003", "name": "丙", "dy": 9.9},  # 负溢价，不入
            {"code": "000004", "name": "丁", "dy": None},  # 无股息率，不入
            {"code": "000005", "name": "戊", "dy": 12.0},  # 不足 1000 根，不入
        ]
        payload = build_ma1000_premium_payload(items, {
            "000002": exact0,
            "000001": exact5,
            "000003": below,
            "000004": exact0,
            "000005": short,
        }, "2026-11-09")
        self.assertEqual(payload["date"], "2026-11-09")
        self.assertEqual(payload["params"]["ma_days"], 1000)
        self.assertEqual(payload["params"]["premium_min_pct"], 0)
        self.assertEqual(payload["params"]["premium_max_pct"], 5)
        self.assertEqual(payload["params"]["price_basis"], "qfq")
        codes = [row["code"] for row in payload["items"]]
        self.assertEqual(codes, ["000001", "000002"])
        self.assertEqual(payload["items"][0]["dy"], 8.8)
        self.assertGreaterEqual(payload["items"][0]["dy"], payload["items"][1]["dy"])
        self.assertTrue(all(0 <= row["premium_pct"] <= 5 for row in payload["items"]))


if __name__ == "__main__":
    unittest.main()

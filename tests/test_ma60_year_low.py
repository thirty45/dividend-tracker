"""60月均溢价、近一年未跌破与近一年月线最低筛选。

筛选本身不区分复权：传入哪套日 K，月末收盘、MA60、溢价和跌破就用哪套。
每日流水线传入不复权日 K（bfq，data/kline_raw），payload 的 price_basis 为 "bfq"。
"""
import unittest

from pipeline.update_daily import (
    MA60_MONTHS,
    build_ma60_year_low_payload,
    screen_ma60_year_low,
)


def month_bars(closes, start=(2018, 1)):
    """每个自然月一根收盘，便于直接构造月末序列。"""
    bars = []
    year, month = start
    for close in closes:
        day = "%04d-%02d-28" % (year, month)
        bars.append({"d": day, "o": close, "h": close, "l": close,
                     "c": close, "v": 1})
        month += 1
        if month > 12:
            month = 1
            year += 1
    return bars


class Ma60YearLowTests(unittest.TestCase):
    def test_needs_sixty_month_closes(self):
        self.assertIsNone(screen_ma60_year_low(month_bars([10.0] * (MA60_MONTHS - 1))))

    def test_match_when_above_ma_within_premium_and_at_year_low(self):
        closes = [10.0] * 48 + [10.5] * 11 + [10.4]
        stats = screen_ma60_year_low(month_bars(closes))
        self.assertIsNotNone(stats)
        self.assertTrue(stats["matched"])
        self.assertEqual(stats["close"], 10.4)
        self.assertEqual(stats["year_low_month_close"], 10.4)
        self.assertGreater(stats["close"], stats["ma60"])
        self.assertLessEqual(stats["premium_pct"], 10)

    def test_rejects_premium_above_ten_percent(self):
        closes = [10.0] * 48 + [20.0] * 12
        stats = screen_ma60_year_low(month_bars(closes))
        self.assertFalse(stats["matched"])
        self.assertGreater(stats["premium_pct"], 10)

    def test_accepts_premium_exactly_ten_percent(self):
        # 前 48 根为 10，近 12 根同为 C，使 C = 1.10 × MA60。
        level = 0.88 / 0.78 * 10
        closes = [10.0] * 48 + [level] * 12
        stats = screen_ma60_year_low(month_bars(closes))
        self.assertTrue(stats["matched"])
        self.assertAlmostEqual(stats["premium_pct"], 10.0, places=2)

    def test_rejects_close_equal_to_ma(self):
        stats = screen_ma60_year_low(month_bars([10.0] * MA60_MONTHS))
        self.assertFalse(stats["matched"])
        self.assertEqual(stats["premium_pct"], 0)

    def test_rejects_when_not_year_month_low(self):
        closes = [10.0] * 59 + [10.5]
        stats = screen_ma60_year_low(month_bars(closes))
        self.assertFalse(stats["matched"])
        self.assertEqual(stats["year_low_month_close"], 10.0)
        self.assertEqual(stats["close"], 10.5)

    def test_half_cent_tolerance_counts_as_year_low(self):
        closes = [10.0] * 59 + [10.004]
        stats = screen_ma60_year_low(month_bars(closes))
        self.assertTrue(stats["matched"])

    def test_one_cent_above_year_low_is_rejected(self):
        closes = [10.0] * 59 + [10.01]
        stats = screen_ma60_year_low(month_bars(closes))
        self.assertFalse(stats["matched"])

    def test_current_month_uses_latest_close_not_intramonth_low(self):
        bars = month_bars([10.0] * 59)
        bars.append({"d": "2022-12-02", "o": 8, "h": 8, "l": 8, "c": 8, "v": 1})
        bars.append({"d": "2022-12-28", "o": 10.4, "h": 10.4, "l": 10.4, "c": 10.4, "v": 1})
        stats = screen_ma60_year_low(bars)
        self.assertEqual(stats["close"], 10.4)
        self.assertEqual(stats["year_low_month_close"], 10.0)
        self.assertFalse(stats["matched"])

    def test_payload_keeps_matches_and_sorts_by_premium_then_yield(self):
        low = month_bars([10.0] * 48 + [10.2] * 12)
        higher = month_bars([10.0] * 48 + [10.8] * 12)
        flat = month_bars([10.0] * MA60_MONTHS)
        items = [
            {"code": "000002", "name": "乙", "dy": 1.5},
            {"code": "000001", "name": "甲", "dy": 6.2},
            {"code": "000003", "name": "丙", "dy": 9},
        ]
        payload = build_ma60_year_low_payload(items, {
            "000002": higher,
            "000001": low,
            "000003": flat,
        }, "2026-10-16")
        self.assertEqual(payload["date"], "2026-10-16")
        self.assertEqual(payload["params"]["ma_months"], 60)
        self.assertEqual(payload["params"]["year_months"], 12)
        self.assertEqual(payload["params"]["month_close"], "month_end")
        self.assertEqual(payload["params"]["price_basis"], "bfq")
        self.assertTrue(payload["params"]["exclude_year_close_below_ma"])
        codes = [row["code"] for row in payload["items"]]
        self.assertEqual(codes, ["000001", "000002"])
        self.assertEqual(payload["items"][0]["dy"], 6.2)
        self.assertLess(payload["items"][0]["premium_pct"],
                        payload["items"][1]["premium_pct"])

    def test_rejects_when_any_year_month_closed_below_its_ma60(self):
        # 早段高价抬高当时 MA；近 12 根从 12 收到 9.1。首月 12 < 当时 MA60 12.1，
        # 但现价仍高于当前 MA、溢价约 8%、且是这 12 根的最低。旧口径入选，新口径剔除。
        closes = [30.0] * 12 + [8.0] * 48 + [
            12, 11.5, 11, 10.5, 10.2, 10, 9.8, 9.6, 9.4, 9.3, 9.2, 9.1]
        bars = month_bars(closes)
        stats = screen_ma60_year_low(bars)
        self.assertFalse(stats["matched"])
        self.assertGreaterEqual(len(stats["ma60_breaks"]), 1)
        self.assertLess(stats["ma60_breaks"][0]["close"], stats["ma60_breaks"][0]["ma60"])
        old = screen_ma60_year_low(bars, exclude_year_break=False)
        self.assertTrue(old["matched"])
        self.assertAlmostEqual(old["premium_pct"], 7.99, places=2)

    def test_keeps_when_every_year_month_stays_above_ma60(self):
        closes = [10.0] * 68 + [10.2] * 11 + [10.15]
        stats = screen_ma60_year_low(month_bars(closes))
        self.assertTrue(stats["matched"])
        self.assertEqual(stats["ma60_breaks"], [])
        self.assertEqual(stats["close"], 10.15)
        self.assertLessEqual(stats["premium_pct"], 10)

    def test_equal_close_to_past_ma60_is_not_a_break(self):
        # 近 12 根的第一根收盘恰好等于当时 MA60；其后 11 根收在 10.6，
        # 现价仍是近一年最低且站上当前 MA。贴平不剔除。
        hist = [12.5] * 11 + [10.2] * 48
        touch = sum(hist) / 59.0
        closes = hist + [touch] + [10.6] * 11
        bars = month_bars(closes)
        stats = screen_ma60_year_low(bars)
        self.assertTrue(stats["matched"])
        self.assertEqual(stats["ma60_breaks"], [])
        dipped = closes[:]
        dipped[59] = touch - 0.01
        broken = screen_ma60_year_low(month_bars(dipped))
        self.assertFalse(broken["matched"])
        self.assertTrue(any(row["close"] < row["ma60"] for row in broken["ma60_breaks"]))


if __name__ == "__main__":
    unittest.main()

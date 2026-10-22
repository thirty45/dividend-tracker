"""备用K线源回退链、stale 判定/补抓、港股快照日期不前移、可转债备用源。全部离线 mock。"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from pipeline import cbonds, hk, status_report
from pipeline import sources_market as sm
from pipeline.net import FetchError, SourceGuard
import pipeline.repair_kline as repair_kline


def bar(day, px=1.0):
    return {"d": day, "o": px, "c": px, "h": px, "l": px, "v": 1.0}


SINA_ROWS = [
    {"day": "2026-10-08", "open": "10.0", "high": "10.5", "low": "9.8", "close": "10.2", "volume": "123400"},
    {"day": "2026-10-09", "open": "10.2", "high": "10.6", "low": "10.0", "close": "10.4", "volume": "100000"},
    {"day": "2026-10-12", "open": "9.4", "high": "9.6", "low": "9.2", "close": "9.5", "volume": "200000"},
]
SINA_QFQ_JS = ('var sz000899qfq={"total":2,"data":[{"d":"2026-10-12", "f":"1.0000000000"},'
               '{"d":"2000-01-04", "f":"1.1000000000"}]}\n/* sig */')


def fake_network(url, params=None, **kwargs):
    """腾讯一律 501（模拟 runner），新浪正常返回。"""
    if "gtimg" in url:
        raise FetchError("请求失败(1次): %s -> <HTTPError 501: 'Not Implemented'>" % url, code=501)
    if "getKLineData" in url:
        return SINA_ROWS
    raise AssertionError("unexpected url " + url)


class GuardMixin:
    def setUp(self):
        self.patches = [
            mock.patch.object(sm, "TENCENT_GUARD", SourceGuard("t", max_fail=3)),
            mock.patch.object(sm, "SINA_GUARD", SourceGuard("s", max_fail=3)),
            mock.patch.object(sm, "_em_fail_streak", 2),  # 东财已熔断
            mock.patch.dict(sm._sina_factor_cache, clear=True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()


class AshareFallbackTests(GuardMixin, unittest.TestCase):
    def test_tencent_501_falls_back_to_sina_raw(self):
        with mock.patch("pipeline.sources_market.fetch_json", fake_network), redirect_stdout(io.StringIO()):
            bars = sm.fetch_kline_raw("000899", "20261001", "20500101", "2026-10-12")
        self.assertEqual([b["d"] for b in bars], ["2026-10-08", "2026-10-09", "2026-10-12"])
        self.assertAlmostEqual(bars[0]["v"], 1234.0)  # 新浪成交量(股) -> 手
        self.assertEqual(getattr(bars, "source", ""), "sina")

    def test_sina_qfq_applies_factor(self):
        with mock.patch("pipeline.sources_market.fetch_json", fake_network), \
             mock.patch("pipeline.sources_market.fetch", return_value=SINA_QFQ_JS), \
             redirect_stdout(io.StringIO()):
            _, bars = sm.fetch_kline("000899", "20261001", "20500101", "2026-10-12")
        self.assertAlmostEqual(bars[0]["c"], round(10.2 / 1.1, 3))
        self.assertAlmostEqual(bars[-1]["c"], 9.5)  # 除权日起因子 1

    def test_tencent_breaker_stops_requests(self):
        calls = []

        def counting(url, params=None, **kwargs):
            calls.append(url)
            return fake_network(url, params, **kwargs)

        with mock.patch("pipeline.sources_market.fetch_json", counting), redirect_stdout(io.StringIO()) as out:
            for _ in range(6):
                sm.fetch_kline_raw("000899", "20261001", "20500101", "2026-10-12")
        tencent = [u for u in calls if "gtimg" in u]
        # 熔断阈值 3：前 3 只每只 2 次（窗口 + 近期补抓），之后不再请求腾讯
        self.assertEqual(len(tencent), 6)
        self.assertTrue(sm.TENCENT_GUARD.blocked())
        self.assertIn("::warning", out.getvalue())

    def test_both_sources_fail_keeps_unavailable_flag(self):
        def all_down(url, params=None, **kwargs):
            raise FetchError("down", code=501)

        with mock.patch("pipeline.sources_market.fetch_json", all_down), redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError) as ctx:
                sm.fetch_kline_raw("000899", "20261001", "20500101", "2026-10-12")
        self.assertTrue(ctx.exception.unavailable)
        self.assertIn("新浪", str(ctx.exception))

    def test_recent_falls_back_to_sina(self):
        with mock.patch("pipeline.sources_market.fetch_json", fake_network), redirect_stdout(io.StringIO()):
            bars = sm.fetch_kline_recent("000899", qfq=False)
        self.assertFalse(bars.history_complete)
        self.assertEqual(bars[-1]["d"], "2026-10-12")


def series(days, complete=True):
    bars = [bar(d) for d in days]
    return {"code": "000899", "name": "t", "bars": bars, "history_complete": complete,
            "coverage_complete": complete, "coverage_start": bars[0]["d"], "coverage_end": bars[-1]["d"]}


class RepairStaleTests(unittest.TestCase):
    def _root(self, tmp, end_day):
        root = Path(tmp)
        (root / "snapshot.json").write_text(json.dumps({
            "date": "2026-10-12", "items": [{"code": "000899", "name": "t"}]}), encoding="utf-8")
        for folder in ("kline", "kline_raw"):
            (root / folder).mkdir()
            (root / folder / "000899.json").write_text(
                json.dumps(series(["2026-10-08", end_day])), encoding="utf-8")
        return root

    def _main(self, root, *extra):
        argv = ["repair_kline.py", "--data-root", str(root)] + list(extra)
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), redirect_stdout(out):
            code = repair_kline.main()
        return code, out.getvalue()

    def test_audit_marks_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, "2026-10-09")
            report = repair_kline.audit(root, {"000899": "t"}, "2026-10-12")
        self.assertEqual(report["stale"], ["000899:qfq", "000899:raw"])
        self.assertEqual(report["missing"], [])

    def test_stale_over_threshold_warns_and_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, "2026-10-09")
            code, out = self._main(root)
        self.assertEqual(code, 1)  # 100% 陈旧 > 50%
        self.assertIn("::warning title=K线陈旧", out)
        self.assertIn("::error title=K线陈旧超限", out)

    def test_repair_refreshes_stale_and_keeps_history(self):
        new = [bar("2026-10-09"), bar("2026-10-12")]
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, "2026-10-09")
            with mock.patch.object(repair_kline.sm, "fetch_kline", return_value=("", new)), \
                 mock.patch.object(repair_kline.sm, "fetch_kline_raw", return_value=new):
                code, out = self._main(root, "--repair")
            raw = json.loads((root / "kline_raw" / "000899.json").read_text(encoding="utf-8"))
            cov = json.loads((root / "meta" / "kline_coverage.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual([b["d"] for b in raw["bars"]], ["2026-10-08", "2026-10-09", "2026-10-12"])
        self.assertTrue(raw["history_complete"])
        self.assertTrue(raw["coverage_complete"])
        self.assertEqual(cov["stale"], [])
        self.assertEqual(cov["stale_ratio"], 0)
        self.assertNotIn("::warning", out)

    def test_failed_stale_refresh_does_not_touch_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, "2026-10-09")
            before = (root / "kline" / "000899.json").read_text(encoding="utf-8")
            with mock.patch.object(repair_kline.sm, "fetch_kline", side_effect=RuntimeError("501")), \
                 mock.patch.object(repair_kline.sm, "fetch_kline_raw", side_effect=RuntimeError("501")):
                code, _ = self._main(root, "--repair")
            self.assertEqual(code, 1)
            self.assertEqual((root / "kline" / "000899.json").read_text(encoding="utf-8"), before)


YAHOO = {"chart": {"result": [{
    "meta": {"gmtoffset": 28800},
    "timestamp": [1791855000, 1791941400, 1792027800],  # 2026-10-13/14/15 09:30 HKT
    "indicators": {
        "quote": [{"open": [10.0, 11.0, None], "close": [10.0, 11.0, None],
                   "high": [10.5, 11.5, None], "low": [9.5, 10.5, None], "volume": [100, 200, None]}],
        "adjclose": [{"adjclose": [9.0, 11.0, None]}],
    }}]}}


EM_PAYLOAD = {"data": {"name": "腾讯控股", "klines": [
    "2026-10-13,10.000,10.000,10.500,9.500,100",
    "2026-10-14,11.000,11.000,11.500,10.500,200",
]}}


class HkTests(unittest.TestCase):
    def setUp(self):
        self.patches = [mock.patch.object(hk, "TENCENT_GUARD", SourceGuard("t", max_fail=3)),
                        mock.patch.object(hk, "EM_GUARD", SourceGuard("e", max_fail=3, min_interval=0)),
                        mock.patch.object(hk, "YAHOO_GUARD", SourceGuard("y", max_fail=3))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_yahoo_parse_and_qfq(self):
        seen = []

        def fake(url, params=None, **kw):
            seen.append(url)
            return YAHOO

        with mock.patch.object(hk, "fetch_json", fake):
            bars = hk.fetch_hk_kline_yahoo("00700", "20261001", qfq=True)
        self.assertTrue(seen[0].endswith("/0700.HK"))
        self.assertEqual([b["d"] for b in bars], ["2026-10-13", "2026-10-14"])  # 空值行跳过
        self.assertAlmostEqual(bars[0]["c"], 9.0)
        self.assertAlmostEqual(bars[0]["h"], 9.45)

    def test_em_parse(self):
        with mock.patch.object(hk, "fetch_json", return_value=EM_PAYLOAD):
            bars = hk.fetch_hk_kline_em("00700", "20261001", qfq=True)
        self.assertEqual([b["d"] for b in bars], ["2026-10-13", "2026-10-14"])
        self.assertAlmostEqual(bars[0]["c"], 10.0)
        self.assertAlmostEqual(bars[1]["v"], 200)

    def test_tencent_501_falls_back_to_em(self):
        def fake(url, params=None, **kw):
            if "gtimg" in url:
                raise FetchError("<HTTPError 501: 'Not Implemented'>", code=501)
            if "push2his" in url or "eastmoney" in url:
                return EM_PAYLOAD
            raise AssertionError("unexpected url %s" % url)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(hk, "BASE", tmp), \
             mock.patch.object(hk, "fetch_json", fake), mock.patch.object(hk.time, "sleep"):
            bars = hk.fetch_hk_kline("00700", "20261001", qfq=False)
        self.assertEqual(bars[-1]["d"], "2026-10-14")
        self.assertEqual(hk.TENCENT_GUARD.fail_count, 1)
        self.assertEqual(hk.EM_GUARD.ok_count, 1)
        self.assertEqual(hk.YAHOO_GUARD.ok_count, 0)

    def test_tencent_and_em_fail_falls_back_to_yahoo(self):
        def fake(url, params=None, **kw):
            if "gtimg" in url or "push2his" in url or "eastmoney" in url:
                raise FetchError("<HTTPError 501: 'Not Implemented'>", code=501)
            return YAHOO

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(hk, "BASE", tmp), \
             mock.patch.object(hk, "fetch_json", fake), mock.patch.object(hk.time, "sleep"):
            bars = hk.fetch_hk_kline("00700", "20261001", qfq=False)
        self.assertEqual(bars[-1]["d"], "2026-10-14")
        self.assertEqual(hk.TENCENT_GUARD.fail_count, 1)
        self.assertEqual(hk.EM_GUARD.fail_count, 1)
        self.assertEqual(hk.YAHOO_GUARD.ok_count, 1)

    def test_soft_empty_does_not_trip_yahoo(self):
        def fake(url, params=None, **kw):
            if "gtimg" in url or "push2his" in url or "eastmoney" in url:
                raise FetchError("<HTTPError 501: 'Not Implemented'>", code=501)
            return {"chart": {"result": [{"meta": {}, "timestamp": [], "indicators": {"quote": [{}], "adjclose": [{}]}}]}}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(hk, "BASE", tmp), \
             mock.patch.object(hk, "fetch_json", fake), mock.patch.object(hk.time, "sleep"):
            with self.assertRaises(RuntimeError):
                hk.fetch_hk_kline("00700", "20261001", qfq=False)
        self.assertEqual(hk.YAHOO_GUARD.fail_count, 0)
        self.assertFalse(hk.YAHOO_GUARD.tripped)

    def test_await_turn_waits_for_probe(self):
        g = SourceGuard("t", max_fail=1, min_interval=0, cooldown=0.2)
        g.fail(RuntimeError("x"))
        self.assertTrue(g.tripped)
        self.assertTrue(g.blocked())  # within cooldown
        self.assertTrue(g.await_turn(max_wait=1.0))  # waits then gets probe slot

    def _write_snapshot(self, tmp, snap):
        d = Path(tmp) / "data" / "hk"
        d.mkdir(parents=True)
        (d / "snapshot.json").write_text(json.dumps(snap), encoding="utf-8")
        return d / "snapshot.json"

    def test_cached_components_keep_real_asof(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(hk, "BASE", tmp):
            self._write_snapshot(tmp, {"date": "2026-10-19", "components_asof": "2026-10-13",
                                       "items": [{"code": "700", "name": "腾讯控股"}]})
            rows, asof = hk._load_cached_components()
        self.assertEqual(asof, "2026-10-13")
        self.assertIn("00700", rows)

    def test_all_failed_does_not_advance_snapshot(self):
        snap = {"date": "2026-10-13", "components_source": "snapshot-cache",
                "components_asof": "2026-10-13", "items": [{"code": "00700", "name": "腾讯控股"}]}

        def fake_components():
            hk.COMPONENTS_SOURCE = "snapshot-cache"
            hk.COMPONENTS_ASOF = "2026-10-13"
            return {"00700": {"f14": "腾讯控股", "f26": "20040616"}}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(hk, "BASE", tmp), \
             mock.patch.object(hk, "fetch_components", fake_components), \
             mock.patch.object(hk, "fetch_hk_kline", side_effect=RuntimeError("501")), \
             mock.patch.object(hk, "fetch_financial", side_effect=AssertionError("不应继续")), \
             redirect_stdout(io.StringIO()) as out:
            path = self._write_snapshot(tmp, snap)
            code = hk.build()
            after = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(after["date"], "2026-10-13")
        self.assertIn("成功 0，失败 1", out.getvalue())


class CbondsFallbackTests(unittest.TestCase):
    def test_datacenter_rows_map_to_push2_fields(self):
        payload = {"result": {"data": [
            {"SECURITY_CODE": "127119", "SECURITY_NAME_ABBR": "中鼎转03", "CONVERT_STOCK_CODE": "000887",
             "SECURITY_SHORT_NAME": "中鼎股份", "TRANSFER_PRICE": 18.2, "TRANSFER_VALUE": 95.05,
             "CURRENT_BOND_PRICE": "-", "TRANSFER_PREMIUM_RATIO": 5.2,
             "PUBLIC_START_DATE": "2026-10-22 00:00:00", "EXPIRE_DATE": "2032-10-22 00:00:00"},
            {"SECURITY_CODE": "110001", "SECURITY_NAME_ABBR": "已退转债", "PUBLIC_START_DATE": "2020-01-01 00:00:00",
             "DELIST_DATE": "2025-01-01 00:00:00"},
        ]}}
        import datetime
        with mock.patch.object(cbonds, "fetch", return_value=json.dumps(payload)):
            quotes, issue = cbonds.fetch_quotes_dc(datetime.date(2026, 10, 20))
        self.assertEqual(list(quotes), ["127119"])
        q = quotes["127119"]
        self.assertEqual((q["f232"], q["f235"], q["f237"]), ("000887", 18.2, 5.2))
        self.assertIsNone(cbonds.num(q["f2"]))
        self.assertIn("127119", issue)


class StatusReportTests(unittest.TestCase):
    def test_stale_modules_flagged_with_real_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            (data / "meta").mkdir(parents=True)
            (data / "hk").mkdir()
            (data / "snapshot.json").write_text(json.dumps({"date": "2026-10-20", "total": 1}), encoding="utf-8")
            (data / "cbonds.json").write_text(json.dumps({"date": "2026-10-16"}), encoding="utf-8")
            (data / "hk" / "snapshot.json").write_text(json.dumps(
                {"date": "2026-10-13", "components_asof": "2026-10-13", "kline_date": "2026-10-13"}), encoding="utf-8")
            (data / "meta" / "kline_coverage.json").write_text(json.dumps(
                {"target_date": "2026-10-20", "expected_series": 10, "stale": ["a"] * 8}), encoding="utf-8")
            with mock.patch.object(status_report, "BASE", Path(tmp)), \
                 mock.patch.dict(os.environ, {"OUTCOME_CBONDS": "failure"}), \
                 redirect_stdout(io.StringIO()) as out:
                status_report.main()
        text = out.getvalue()
        self.assertIn("| 可转债 | failure | 2026-10-16 ⚠️陈旧", text)
        self.assertIn("| 港股通 | — | 2026-10-13 ⚠️陈旧", text)
        self.assertIn("覆盖报告陈旧 8/10", text)
        self.assertIn("::warning title=A股K线 数据陈旧", text)


if __name__ == "__main__":
    unittest.main()


class SourceGuardCooldownTests(unittest.TestCase):
    """熔断后过了冷却期放行一只试探：成功恢复，失败继续熔断。"""

    def test_probe_after_cooldown(self):
        from pipeline import net
        clock = [100.0]
        with mock.patch.object(net.time, "monotonic", lambda: clock[0]):
            g = SourceGuard("t", max_fail=2, cooldown=30)
            g.fail(); g.fail()
            self.assertTrue(g.blocked())
            clock[0] += 31
            self.assertFalse(g.blocked())   # 放行一只试探
            self.assertTrue(g.blocked())    # 同一周期其余仍熔断
            g.fail()                        # 试探失败，继续熔断
            self.assertTrue(g.blocked())
            clock[0] += 31
            self.assertFalse(g.blocked())
            g.ok()                          # 试探成功，恢复
            self.assertFalse(g.blocked())
            self.assertEqual(g.trips, 1)

    def test_zero_cooldown_never_reopens(self):
        g = SourceGuard("t", max_fail=1, cooldown=0)
        g.fail()
        self.assertTrue(g.blocked())
        self.assertTrue(g.blocked())

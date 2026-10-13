"""Early-window 501s must not drop a usable K-line or block deploy."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from pipeline import sources_market as sm
from pipeline.kline import KlineBars, valid_bars
from pipeline.net import FetchError, fetch
import pipeline.repair_kline as repair_kline
import pipeline.update_daily as update_daily


def bar(day, px=1.0):
    return {"d": day, "o": px, "c": px, "h": px, "l": px, "v": 1.0}


def payload(days, history_complete=True):
    bars = [bar(day) for day in days]
    return {
        "code": "000899",
        "name": "test",
        "bars": bars,
        "history_complete": history_complete,
        "coverage_complete": history_complete,
        "coverage_start": bars[0]["d"],
        "coverage_end": bars[-1]["d"],
    }


class SegmentTests(unittest.TestCase):
    def test_early_501_keeps_later_windows_once(self):
        calls = []

        def fake_fetch(url, params=None, **kwargs):
            param = params["param"]
            calls.append(param)
            sym = param.split(",")[0]
            if "1990-01-01" in param or ",,," in param:
                raise FetchError("HTTPError 501: 'Not Implemented'", code=501)
            return {"code": 0, "data": {sym: {"day": [
                ["1992-06-19", "1", "1.2", "1.3", "0.9", "10"],
                ["1992-06-22", "1.2", "1.1", "1.4", "1.0", "11"],
            ]}}}

        with mock.patch("pipeline.sources_market.fetch_json", fake_fetch):
            bars = sm._fetch_tencent_segments("sz000899", "19900101", "19930101", False)
        self.assertFalse(bars.history_complete)
        self.assertEqual([b["d"] for b in bars], ["1992-06-19", "1992-06-22"])
        self.assertEqual(sum("1990-01-01" in c for c in calls), 1)
        self.assertTrue(any("1992-06-19" in c for c in calls))
        self.assertTrue(bars.gaps)

    def test_every_window_501_raises_without_a_second_walk(self):
        calls = []

        def fake_fetch(url, params=None, **kwargs):
            calls.append(params["param"])
            raise FetchError("no", code=501)

        with mock.patch("pipeline.sources_market.fetch_json", fake_fetch):
            with self.assertRaises(RuntimeError) as ctx:
                sm._fetch_tencent_segments("sz000899", "19900101", "19930101", False)
        self.assertTrue(ctx.exception.unavailable)
        # Two date windows plus one recent count call, each once.
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(set(calls)), 3)

    def test_empty_early_window_stays_complete(self):
        def fake_fetch(url, params=None, **kwargs):
            param = params["param"]
            sym = param.split(",")[0]
            if "1990-01-01" in param or ",,," in param:
                return {"code": 0, "data": {sym: {"day": []}}}
            return {"code": 0, "data": {sym: {"day": [
                ["1992-06-19", "1", "1", "1", "1", "10"],
            ]}}}

        with mock.patch("pipeline.sources_market.fetch_json", fake_fetch):
            bars = sm._fetch_tencent_segments("sz000899", "19900101", "19930101", False)
        self.assertTrue(bars.history_complete)
        self.assertEqual(bars[-1]["d"], "1992-06-19")
        self.assertEqual(bars.gaps, [])

    def test_middle_501_keeps_both_sides(self):
        def fake_fetch(url, params=None, **kwargs):
            param = params["param"]
            sym = param.split(",")[0]
            if "1992-06-19" in param or ",,," in param:
                raise FetchError("HTTPError 501: 'Not Implemented'", code=501)
            if "1990-01-01" in param:
                return {"code": 0, "data": {sym: {"day": [
                    ["1991-01-02", "1", "1", "1", "1", "10"],
                ]}}}
            return {"code": 0, "data": {sym: {"day": [
                ["1994-12-06", "2", "2", "2", "2", "12"],
            ]}}}

        with mock.patch("pipeline.sources_market.fetch_json", fake_fetch):
            bars = sm._fetch_tencent_segments("sz000899", "19900101", "19950101", False)
        self.assertFalse(bars.history_complete)
        self.assertEqual([b["d"] for b in bars], ["1991-01-02", "1994-12-06"])

    def test_recent_501_does_not_drop_complete_segments(self):
        def fake_fetch(url, params=None, **kwargs):
            param = params["param"]
            sym = param.split(",")[0]
            if ",,," in param:
                raise FetchError("HTTPError 501: 'Not Implemented'", code=501)
            return {"code": 0, "data": {sym: {"day": [
                ["1992-06-19", "1", "1", "1", "1", "10"],
            ]}}}

        with mock.patch("pipeline.sources_market.fetch_json", fake_fetch):
            bars = sm._fetch_tencent_segments("sz000899", "19900101", "19930101", False)
        self.assertTrue(bars.history_complete)
        self.assertEqual(len(bars), 1)

    def test_broken_print_is_dropped_and_neighbors_remain(self):
        def fake_fetch(url, params=None, **kwargs):
            param = params["param"]
            sym = param.split(",")[0]
            if ",,," in param:
                return {"code": 0, "data": {sym: {"day": []}}}
            return {"code": 0, "data": {sym: {"day": [
                ["2024-01-02", "1", "1", "1", "1", "10"],
                ["2024-01-03", "6.33", "6.45", "6.47", "6.35", "20"],
                ["2024-01-04", "2", "2", "2", "2", "12"],
            ]}}}

        with mock.patch("pipeline.sources_market.fetch_json", fake_fetch):
            bars = sm._fetch_tencent_segments("sz000899", "20240101", "20240110", False)
        self.assertEqual([b["d"] for b in bars], ["2024-01-02", "2024-01-04"])
        self.assertTrue(valid_bars(bars))

    def test_count_501_falls_back_to_one_date_window(self):
        calls = []

        def fake_fetch(url, params=None, **kwargs):
            calls.append(params["param"])
            if ",,," in params["param"]:
                raise FetchError("HTTPError 501: 'Not Implemented'", code=501)
            sym = params["param"].split(",")[0]
            return {"code": 0, "data": {sym: {"day": [
                ["2026-10-12", "3", "3", "3", "3", "8"],
            ]}}}

        with mock.patch("pipeline.sources_market.fetch_json", fake_fetch):
            bars = sm.fetch_kline_recent("000899", qfq=False)
        self.assertEqual(len(calls), 2)
        self.assertFalse(bars.history_complete)
        self.assertEqual(bars[-1]["d"], "2026-10-12")


class FetchRetryTests(unittest.TestCase):
    def test_http_501_is_not_retried(self):
        calls = []

        def urlopen(req, timeout=0):
            calls.append(req.full_url)
            raise HTTPError(req.full_url, 501, "Not Implemented", hdrs={}, fp=None)

        with mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(FetchError) as ctx:
                fetch("https://example.test/k", retries=3, delay=0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(ctx.exception.code, 501)


class DeployGateTests(unittest.TestCase):
    def _run(self, root):
        argv = ["repair_kline.py", "--data-root", str(root)]
        with mock.patch.object(sys, "argv", argv):
            return repair_kline.main()

    def test_partial_history_deploys_blank_and_broken_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "snapshot.json").write_text(json.dumps({
                "date": "2026-10-12",
                "items": [{"code": "000899", "name": "test"}],
            }), encoding="utf-8")
            good = payload(["2026-10-12"], True)
            partial = payload(["2026-10-12"], False)

            def write(folder, body):
                path = root / folder
                path.mkdir(parents=True, exist_ok=True)
                (path / "000899.json").write_text(json.dumps(body), encoding="utf-8")

            write("kline", good)
            write("kline_raw", partial)
            self.assertEqual(self._run(root), 0)

            write("kline_raw", {})
            self.assertEqual(self._run(root), 1)

            broken = payload(["2026-10-12"], True)
            broken["bars"] = [{"d": "2026-10-12", "o": 5, "c": 1, "h": 1, "l": 4, "v": 1}]
            write("kline_raw", broken)
            self.assertEqual(self._run(root), 1)

            (root / "kline_raw" / "000899.json").unlink()
            self.assertEqual(self._run(root), 1)


class RepairWriteTests(unittest.TestCase):
    def test_partial_raw_is_written_and_hard_failure_is_not_retried(self):
        calls = {"raw": 0, "recent": 0}

        def fake_raw(code, beg, end, target):
            calls["raw"] += 1
            if calls["raw"] == 1:
                return KlineBars([bar("2026-10-10"), bar("2026-10-12")],
                                 history_complete=False, gaps=["1990-01-01~1992-06-18: 501"])
            raise AssertionError("hard gap walk retried")

        def fake_qfq(code, beg, end, target):
            err = RuntimeError("腾讯日K分段失败")
            err.unavailable = True
            raise err

        def fake_recent(code, qfq=True):
            calls["recent"] += 1
            if qfq:
                err = RuntimeError("recent 501")
                err.unavailable = True
                raise err
            raise AssertionError("raw recent used after a usable partial")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(repair_kline.sm, "fetch_kline_raw", fake_raw), \
                 mock.patch.object(repair_kline.sm, "fetch_kline", fake_qfq), \
                 mock.patch.object(repair_kline.sm, "fetch_kline_recent", fake_recent):
                results = repair_kline.repair_one(
                    root, "000899", "test", "2026-10-12", {"kline_start": "19900101"})
            raw = json.loads((root / "kline_raw" / "000899.json").read_text(encoding="utf-8"))
            self.assertFalse(raw["history_complete"])
            self.assertEqual(raw["bars"][-1]["d"], "2026-10-12")
            self.assertIn("1990-01-01~1992-06-18", " ".join(raw["repair_errors"]))
            self.assertEqual(calls["raw"], 1)
            self.assertEqual(calls["recent"], 1)
            self.assertIn("recent", [r["status"] for r in results])
            self.assertIn("failed", [r["status"] for r in results])
            self.assertFalse((root / "kline" / "000899.json").exists())


class RefreshTests(unittest.TestCase):
    def test_partial_raw_merge_keeps_history_flag(self):
        old = payload(["2026-10-08", "2026-10-09"], False)

        def fake_raw(code, beg, end, target):
            return KlineBars([bar("2026-10-12")], history_complete=False,
                             gaps=["1990-01-01~1992-06-18: 501"])

        with mock.patch.object(update_daily.sm, "fetch_kline_raw", fake_raw):
            result = update_daily.refresh_kline_series(
                "000899", old, "2026-10-12",
                {"kline_start": "19900101", "kline_full_days": 0, "history_keep": 0},
                raw=True)
        self.assertTrue(result["changed"])
        self.assertFalse(result["payload"]["history_complete"])
        self.assertEqual([b["d"] for b in result["payload"]["bars"]],
                         ["2026-10-08", "2026-10-09", "2026-10-12"])

    def test_shorter_adjusted_partial_does_not_replace_cache(self):
        old = payload(["2026-10-08", "2026-10-09", "2026-10-12"], False)

        def fake_qfq(code, beg, end, target):
            return "", KlineBars([bar("2026-10-12")], history_complete=False)

        with mock.patch.object(update_daily.sm, "fetch_kline", fake_qfq):
            result = update_daily.refresh_kline_series(
                "000899", old, "2026-10-12",
                {"kline_start": "19900101", "kline_full_days": 0, "history_keep": 0},
                raw=False)
        self.assertFalse(result["changed"])
        self.assertEqual(len(result["payload"]["bars"]), 3)
        self.assertIn("保留更长", result["error"])


if __name__ == "__main__":
    unittest.main()

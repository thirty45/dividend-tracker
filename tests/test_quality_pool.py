# -*- coding: utf-8 -*-
"""优质红利池：权重守恒、强制入池合并。"""
import json
import os
import tempfile
import unittest

from pipeline.build_watchlist import (
    QUALITY_TAG,
    load_quality_pool,
    merge_quality_pool,
)


class QualityPoolTest(unittest.TestCase):
    def test_repo_quality_pool_weights(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "data", "meta", "quality_pool.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        items = data["items"]
        self.assertEqual(data.get("count"), 30)
        self.assertEqual(len(items), 30)
        codes = [it["code"] for it in items]
        self.assertEqual(len(set(codes)), 30)
        self.assertIn("601919", codes)  # 中远海控
        self.assertIn("600566", codes)  # 济川药业
        self.assertIn("300770", codes)  # 新媒股份
        self.assertIn("603565", codes)  # 中谷物流
        self.assertIn("600642", codes)  # 申能股份
        self.assertIn("002267", codes)  # 陕天然气
        self.assertIn("600023", codes)  # 浙能电力
        forbid = {"600760", "000989", "600825", "601866", "601186", "600167"}
        self.assertFalse(set(codes) & forbid)
        wsum = sum(float(it["weight_pct"]) for it in items)
        self.assertAlmostEqual(wsum, 100.0, places=4)

    def test_merge_quality_pool_force_insert(self):
        with tempfile.TemporaryDirectory() as td:
            payload = {
                "name": "优质红利",
                "items": [
                    {"code": "600760", "name": "中航沈飞", "weight_pct": 2.5, "sector": "军工", "source": "t"},
                    {"code": "000651", "name": "格力电器", "weight_pct": 8.0, "sector": "家电", "source": "t"},
                ],
            }
            with open(os.path.join(td, "quality_pool.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            stocks = {
                "000651": {"name": "格力电器", "funds": {"a"}, "max_pct": 1.0, "indices": ["高股息"]},
            }
            codes = merge_quality_pool(stocks, td)
            self.assertEqual(codes, {"600760", "000651"})
            self.assertIn("600760", stocks)
            self.assertEqual(stocks["600760"]["name"], "中航沈飞")
            # existing untouched except still present
            self.assertEqual(stocks["000651"]["max_pct"], 1.0)
            self.assertEqual(QUALITY_TAG, "优质红利")
            loaded = load_quality_pool(td)
            self.assertEqual(len(loaded), 2)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""优质红利命名池：权重守恒与关键代码核验（不依赖大池重建）。"""
import json
import os
import unittest


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
        # 字段约定
        for it in items:
            self.assertIn("code", it)
            self.assertIn("name", it)
            self.assertIn("weight_pct", it)
            self.assertIn("sector", it)
            self.assertIn("source", it)


if __name__ == "__main__":
    unittest.main()

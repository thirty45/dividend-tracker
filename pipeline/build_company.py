# -*- coding: utf-8 -*-
"""一次性重建全部股票的公司基本面数据（控股股东/分红/财务绩效/高管增减持/简介）。

用法:
  python pipeline/build_company.py            # 全量抓取股票池
  python pipeline/build_company.py --limit 10 # 仅前10只(测试)
  python pipeline/build_company.py --force    # 忽略缓存全部重抓

适合在云端 build-company 工作流或本地首次初始化时运行；日常更新交给 update_daily.py。
"""

import argparse
import datetime
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from pipeline import company as comp  # noqa: E402


def load_config():
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def load_watchlist():
    with open(os.path.join(BASE, "data", "meta", "watchlist.json"),
              encoding="utf-8") as f:
        return json.load(f)


def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--workers", type=int, default=0)
    args = ap.parse_args()
    cfg = load_config()

    company_dir = os.path.join(BASE, "data", "company")
    os.makedirs(company_dir, exist_ok=True)

    watch = load_watchlist()
    stocks = watch["stocks"]
    if args.limit:
        stocks = stocks[:args.limit]
    name_map = {s["code"]: s["name"] for s in stocks}
    print("公司基本面：股票池 %d 只" % len(stocks))

    cw = max(1, min(args.workers or cfg.get("company_workers", 6), 8))

    def _one(code):
        try:
            d = comp.fetch_company(code, name_map.get(code, code))
            with open(os.path.join(company_dir, code + ".json"), "w",
                      encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            return code, None
        except Exception as exc:  # noqa: BLE001
            return code, str(exc)

    done = [0]
    ok = [0]
    with ThreadPoolExecutor(max_workers=cw) as ex:
        futs = {ex.submit(_one, s["code"]): s["code"] for s in stocks}
        for fut in as_completed(futs):
            code, err = fut.result()
            done[0] += 1
            if err:
                print("   失败 %s: %s" % (code, err), flush=True)
            else:
                ok[0] += 1
            if done[0] % 100 == 0:
                print("   进度 %d/%d（成功 %d）" %
                      (done[0], len(stocks), ok[0]), flush=True)
    print("完成：成功 %d / 共 %d，写入 %s" %
          (ok[0], len(stocks), company_dir))


if __name__ == "__main__":
    main()

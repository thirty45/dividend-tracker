# -*- coding: utf-8 -*-
"""每日更新脚本：估值/ROE/分红/K线/板块 -> 生成网页数据。

用法: python pipeline/update_daily.py
"""

import argparse
import datetime
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from pipeline import sources_market as sm  # noqa: E402


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
    ap.add_argument("--limit", type=int, default=0, help="只更新前N只(测试用)")
    ap.add_argument("--force", action="store_true", help="忽略幂等检查强制更新")
    args = ap.parse_args()
    cfg = load_config()

    data_root = os.path.join(BASE, "data")
    meta_dir = os.path.join(data_root, "meta")
    kline_dir = os.path.join(data_root, "kline")
    hist_dir = os.path.join(data_root, "history")
    latest_file = os.path.join(meta_dir, "latest.json")
    os.makedirs(meta_dir, exist_ok=True)
    os.makedirs(kline_dir, exist_ok=True)
    os.makedirs(hist_dir, exist_ok=True)

    trade_date = sm.latest_trade_date()
    if not trade_date:
        print("无法获取最新交易日，退出")
        return 1
    if not args.force and os.path.exists(latest_file):
        with open(latest_file, encoding="utf-8") as f:
            prev = json.load(f)
        if prev.get("date") == trade_date:
            kline_files = [f for f in os.listdir(kline_dir)
                           if f.endswith(".json")]
            if kline_files:
                print("数据已是最新(%s)，跳过" % trade_date)
                return 0
            print("快照已是最新但缺少K线文件，重新生成K线数据")

    watch = load_watchlist()
    stocks = watch["stocks"]
    if args.limit:
        stocks = stocks[:args.limit]
    codes = [s["code"] for s in stocks]
    name_map = {s["code"]: s["name"] for s in stocks}
    print("股票池: %d 只（交易日 %s）" % (len(codes), trade_date))

    print("1/5 抓取全市场估值快照 ...")
    _, valuation = sm.fetch_valuation(trade_date)
    val = {v["SECURITY_CODE"]: v for v in valuation if v.get("SECURITY_CODE")}
    print("   估值记录 %d 条" % len(val))

    print("2/5 抓取最新报告期 ROE ...")
    roe_date, roe_map = sm.fetch_roe()
    print("   报告期 %s，%d 条" % (roe_date, len(roe_map)))

    print("3/5 抓取近12个月已实施分红 ...")
    end = datetime.date.today()
    start = end - datetime.timedelta(days=366)
    bonus = sm.fetch_sharebonus(start.isoformat(), end.isoformat())
    print("   %d 只股票有分红记录" % len(bonus))

    print("4/5 抓取K线(并发) ...", flush=True)
    klines = {}
    todo = []
    for c in codes:
        p = os.path.join(kline_dir, c + ".json")
        if not args.force and os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    old = json.load(f)
                if old.get("date") == trade_date and old.get("bars"):
                    klines[c] = old["bars"]
                    continue
            except Exception:  # noqa: BLE001
                pass
        todo.append(c)
    print("   待抓取K线 %d 只（已最新 %d 只）" % (len(todo), len(codes) - len(todo)),
          flush=True)
    done_count = [0]
    with ThreadPoolExecutor(max_workers=cfg.get("workers", 6)) as ex:
        futs = {ex.submit(sm.fetch_kline, c, cfg["kline_start"]): c
                for c in todo}
        now_iso = datetime.datetime.now().isoformat(timespec="seconds")
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                name, bars = fut.result()
                klines[c] = bars
                if name:
                    name_map[c] = name
            except Exception as exc:  # noqa: BLE001
                print("   K线失败 %s: %s" % (c, exc), flush=True)
                klines[c] = []
            # 边抓边写，断点可续
            path = os.path.join(kline_dir, c + ".json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"code": c, "name": name_map.get(c, ""),
                           "date": trade_date, "updated": now_iso,
                           "bars": klines[c]}, f, ensure_ascii=False)
            done_count[0] += 1
            if done_count[0] % 100 == 0:
                print("   K线进度 %d/%d" % (done_count[0], len(todo)), flush=True)
    print("   K线完成 %d/%d" % (len(klines), len(codes)), flush=True)

    # 组装快照
    items = []
    for s in stocks:
        code = s["code"]
        v = val.get(code) or {}
        close = v.get("CLOSE_PRICE")
        dy = bonus.get(code, 0.0) / close * 100.0 if close else None
        mv = (v.get("TOTAL_MARKET_CAP") or 0) / 1e8
        items.append({
            "code": code,
            "name": name_map.get(code) or v.get("SECURITY_NAME_ABBR") or code,
            "close": close,
            "pct": v.get("CHANGE_RATE"),
            "dy": round(dy, 2) if dy is not None else None,
            "pe": v.get("PE_TTM"),
            "pb": v.get("PB_MRQ"),
            "mv": round(mv, 1) if mv else None,
            "roe": roe_map.get(code),
            "roe_date": roe_date,
            "industry": v.get("BOARD_NAME"),
            "tags": s.get("tags", []),
        })
    items.sort(key=lambda x: -(x["dy"] if x["dy"] is not None else -1))

    # 板块市值排名
    groups = {}
    for it in items:
        bn = it["industry"]
        if not bn:
            continue
        g = groups.setdefault(bn, {
            "mv": 0.0, "count": 0,
            "dy_s": 0.0, "n_dy": 0, "pe_s": 0.0, "n_pe": 0,
            "roe_s": 0.0, "n_roe": 0})
        g["mv"] += (it["mv"] or 0) * 1e8
        g["count"] += 1
        if it["dy"] is not None:
            g["dy_s"] += it["dy"]; g["n_dy"] += 1
        if it["pe"] is not None:
            g["pe_s"] += it["pe"]; g["n_pe"] += 1
        if it["roe"] is not None:
            g["roe_s"] += it["roe"]; g["n_roe"] += 1
    board_rows = [{
        "name": k,
        "mv_b": round(g["mv"] / 1e8, 1),
        "count": g["count"],
        "avg_dy": round(g["dy_s"] / g["n_dy"], 2) if g["n_dy"] else None,
        "avg_pe": round(g["pe_s"] / g["n_pe"], 2) if g["n_pe"] else None,
        "avg_roe": round(g["roe_s"] / g["n_roe"], 2) if g["n_roe"] else None,
    } for k, g in groups.items()]
    board_rows.sort(key=lambda r: -r["mv_b"])
    pct = {}
    try:
        bl = sm.fetch_industry_boards()
        pct = {d.get("f14"): d.get("f3") for d in bl if d.get("f14")}
    except Exception as exc:  # noqa: BLE001
        print("   板块涨跌幅获取失败(不影响主数据): %s" % exc)
    if not pct:
        # 兜底：用成分股涨跌幅按市值加权近似板块涨跌幅
        weighted, wsum = {}, {}
        for it in items:
            bn = it["industry"]
            if not bn or it["pct"] is None or not it["mv"]:
                continue
            weighted[bn] = weighted.get(bn, 0.0) + it["pct"] * it["mv"]
            wsum[bn] = wsum.get(bn, 0.0) + it["mv"]
        pct = {k: round(v / wsum[k], 2) for k, v in weighted.items()}
    for r in board_rows:
        r["pct"] = pct.get(r["name"])

    # 写文件
    now = datetime.datetime.now().isoformat(timespec="seconds")
    snap = {"date": trade_date, "updated_at": now,
            "total": len(items), "items": items}
    with open(os.path.join(data_root, "snapshot.json"), "w",
              encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)
    with open(os.path.join(data_root, "snapshot_%s.json" % trade_date),
              "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)

    for c, bars in klines.items():
        path = os.path.join(kline_dir, c + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"code": c, "name": name_map.get(c, ""),
                       "date": trade_date, "updated": now, "bars": bars},
                      f, ensure_ascii=False)

    for it in items:
        path = os.path.join(hist_dir, it["code"] + ".json")
        hist = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                hist = json.load(f)
        if not (hist and hist[-1].get("date") == trade_date):
            hist.append({
                "date": trade_date, "close": it["close"], "dy": it["dy"],
                "pe": it["pe"], "pb": it["pb"], "mv": it["mv"],
                "roe": it["roe"]})
        hist = hist[-cfg.get("history_keep", 600):]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False)

    with open(os.path.join(meta_dir, "boards.json"), "w",
              encoding="utf-8") as f:
        json.dump({"date": trade_date, "boards": board_rows}, f,
                  ensure_ascii=False)
    with open(latest_file, "w", encoding="utf-8") as f:
        json.dump({"date": trade_date, "updated_at": now,
                   "roe_date": roe_date, "total": len(items),
                   "snapshot": "snapshot.json"}, f, ensure_ascii=False)
    print("完成: %s · %d 只股票 · 板块 %d 个" % (trade_date, len(items),
                                            len(board_rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

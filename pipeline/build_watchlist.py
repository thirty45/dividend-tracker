# -*- coding: utf-8 -*-
"""构建自选股票池：红利基金近5年持仓 + 红利指数成分股 + 高股息筛选。

用法: python pipeline/build_watchlist.py
"""

import argparse
import datetime
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from pipeline import sources_funds as sf  # noqa: E402
from pipeline import sources_market as sm  # noqa: E402


def load_config():
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前N只基金(测试用)")
    ap.add_argument("--workers", type=int, default=0, help="基金抓取并发数(默认取config)")
    ap.add_argument("--resume", action="store_true",
                    help="断点续跑：复用已有基金持仓，只抓缺失的基金")
    args = ap.parse_args()
    cfg = load_config()

    data_meta = os.path.join(BASE, "data", "meta")
    os.makedirs(data_meta, exist_ok=True)
    fund_holdings_file = os.path.join(data_meta, "fund_holdings.json")

    print("1/4 拉取基金列表并筛选红利基金 ...")
    funds = sf.select_dividend_funds(sf.load_fund_list())
    if args.limit:
        funds = funds[:args.limit]
    print("   筛选出 %d 只红利基金" % len(funds))

    prev_holdings = {}
    if args.resume and os.path.exists(fund_holdings_file):
        with open(fund_holdings_file, encoding="utf-8") as f:
            prev = json.load(f)
        prev_holdings = {p["code"]: p for p in prev.get("funds", [])}

    print("2/4 抓取近5年基金前十大持仓 ...")
    to_fetch = []
    for code, name, ftype in funds:
        if args.resume:
            p = prev_holdings.get(code)
            if p and p.get("holdings"):
                continue
        to_fetch.append((code, name, ftype))
    print("   需要抓取 %d 只（已有 %d 只）" % (
        len(to_fetch), len(funds) - len(to_fetch)))
    holdings = sf.fetch_many_holdings(
        to_fetch, workers=args.workers or cfg.get("fund_workers", 6),
        min_year=cfg.get("fund_min_year", 2020),
        progress=lambda s: print("   " + s))
    holdings = {**prev_holdings, **holdings}

    stocks = {}
    fund_holdings_out = []
    for code, name, ftype in funds:
        p = holdings.get(code)
        if isinstance(p, dict):
            recs = p.get("holdings") or []
        else:
            recs = p or []
        fund_holdings_out.append({
            "code": code, "name": name, "type": ftype,
            "periods": len({r["end_date"] for r in recs}),
            "holdings": recs,
        })
        for r in recs:
            st = stocks.setdefault(r["code"], {
                "code": r["code"], "name": r["name"],
                "funds": set(), "max_pct": 0.0, "periods": 0})
            st["funds"].add(code)
            st["max_pct"] = max(st["max_pct"], r["pct"])
            st["periods"] += 1
    print("   基金持仓共涉及 %d 只股票" % len(stocks))

    print("3/4 抓取红利指数成分股 ...")
    for idx, tag in cfg["dividend_index_names"].items():
        try:
            rows = sm.fetch_sina_index(idx)
            print("   %s(%s): %d 只" % (tag, idx, len(rows)))
            for c, n in rows:
                st = stocks.setdefault(c, {
                    "code": c, "name": n,
                    "funds": set(), "max_pct": 0.0, "periods": 0})
                st["name"] = n
                st.setdefault("indices", []).append(tag)
        except Exception as exc:  # noqa: BLE001
            print("   %s(%s) 失败: %s" % (tag, idx, exc))

    for bk, tag in cfg["concept_board_names"].items():
        try:
            rows = sm.fetch_board_constituents(bk)
            if rows:
                print("   %s(%s): %d 只" % (tag, bk, len(rows)))
                for c, n in rows:
                    st = stocks.setdefault(c, {
                        "code": c, "name": n,
                        "funds": set(), "max_pct": 0.0, "periods": 0})
                    st["name"] = n
                    st.setdefault("indices", []).append(tag)
        except Exception as exc:  # noqa: BLE001
            print("   %s(%s) 失败: %s" % (tag, bk, exc))

    print("4/4 高股息筛选 ...")
    trade_date, valuation = sm.fetch_valuation()
    end = datetime.date.today()
    start = end - datetime.timedelta(days=366)
    bonus = sm.fetch_sharebonus(start.isoformat(), end.isoformat())
    val = {v["SECURITY_CODE"]: v for v in valuation if v.get("SECURITY_CODE")}
    high = []
    for code, v in val.items():
        close = v.get("CLOSE_PRICE")
        mv = v.get("TOTAL_MARKET_CAP")
        if not close or not mv:
            continue
        dy = bonus.get(code, 0.0) / close * 100.0
        if dy >= cfg["high_yield_min_pct"] and mv >= cfg["high_yield_min_mv"]:
            high.append(code)
    print("   高股息(股息率TTM>=%.1f%%, 市值>=100亿): %d 只"
          % (cfg["high_yield_min_pct"], len(high)))
    for code in high:
        st = stocks.setdefault(code, {
            "code": code,
            "name": val[code].get("SECURITY_NAME_ABBR") or code,
            "funds": set(), "max_pct": 0.0, "periods": 0})
        st.setdefault("indices", []).append("高股息")

    out = []
    for code, st in sorted(stocks.items()):
        tags = []
        if st.get("funds"):
            tags.append("基金持仓")
        for t in (st.get("indices") or []):
            if t not in tags:
                tags.append(t)
        out.append({
            "code": code,
            "name": st["name"],
            "tags": tags,
            "fund_count": len(st["funds"]),
            "max_pct": round(st["max_pct"], 2),
        })

    built_at = datetime.datetime.now().isoformat(timespec="seconds")
    meta = {"built_at": built_at, "count": len(out), "stocks": out}
    with open(os.path.join(data_meta, "watchlist.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    with open(fund_holdings_file, "w", encoding="utf-8") as f:
        json.dump({"built_at": built_at, "valuation_date": trade_date,
                   "funds": fund_holdings_out}, f, ensure_ascii=False)
    print("完成: 股票池共 %d 只 -> %s" % (len(out), os.path.join(data_meta, "watchlist.json")))


if __name__ == "__main__":
    main()

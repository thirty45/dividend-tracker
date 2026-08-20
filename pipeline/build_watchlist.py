# -*- coding: utf-8 -*-
"""构建自选股票池：红利基金近5年持仓 + 红利指数成分股 + 高股息筛选。

用法: python pipeline/build_watchlist.py
"""

import argparse
import datetime
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

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

    print("2/5 抓取近5年基金前十大持仓 ...")
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

    print("3/5 抓取红利指数成分股 ...")
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

    print("4/5 高股息筛选 ...")
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

    print("5/5 剔除分红/退市不合格股票 ...")
    n_dy = cfg.get("dividend_years", 3)
    today = datetime.date.today()
    need_years = set(range(today.year - n_dy + 1, today.year + 1))
    # 分红窗口再往前扩一年，覆盖连续3个完整年度（供“连续3年股息率<1%”判断）
    start3 = "%d-01-01" % (today.year - 3)
    div_map = sm.fetch_dividends_by_year(start3, today.isoformat())
    print("   近%d年有分红记录的股票: %d 只" % (n_dy, len(div_map)))
    removed_no_div, removed_delisted, keep = [], [], {}
    for code, st in sorted(stocks.items()):
        years_with = set((div_map.get(code) or {}).keys())
        if not need_years.issubset(years_with):
            removed_no_div.append(code)
            continue
        if code not in val:
            removed_delisted.append(code)
            continue
        keep[code] = st
    print("   剔除: 近%d年有缺少年份分红 %d 只, 已退市/无行情 %d 只"
          % (n_dy, len(removed_no_div), len(removed_delisted)))
    examples = ["%s(%s)" % (c, (stocks[c].get("name") or c))
                for c in (removed_no_div + removed_delisted)[:15]]
    print("   剔除示例: " + ", ".join(examples))
    stocks = keep

    print("6/6 低股息/亏损质量过滤 ...")
    n_yield = cfg.get("min_yield_years", 3)
    min_pct = cfg.get("min_yield_pct", 1.0)
    yset = [today.year - n_yield + i for i in range(n_yield)]  # 连续N个完整年度

    # 年度每股分红(除权日归属年) 已由 div_map 覆盖；缺年度末收盘价需抓未复权K线
    need_close = [c for c in stocks
                  if any((div_map.get(c) or {}).get(y) for y in yset)]
    print("   为 %d 只股票抓未复权K线（取年度末收盘价算年度股息率）..."
          % len(need_close), flush=True)
    raw_dir = os.path.join(BASE, "data", "kline_raw")
    os.makedirs(raw_dir, exist_ok=True)
    raw_klines = {}

    def _raw_one(c):
        try:
            bars = sm.fetch_kline_raw(c, "20200101", "20500101")
        except Exception:  # noqa: BLE001
            bars = []
        if bars:
            with open(os.path.join(raw_dir, c + ".json"), "w",
                      encoding="utf-8") as f:
                json.dump({"code": c, "name": "",
                           "date": bars[-1]["d"],
                           "updated": datetime.datetime.now().isoformat(
                               timespec="seconds"),
                           "bars": bars}, f, ensure_ascii=False)
        return c, bars

    done_r = [0]
    with ThreadPoolExecutor(max_workers=cfg.get("fund_workers", 6)) as ex:
        futs = {ex.submit(_raw_one, c): c for c in need_close}
        for fut in as_completed(futs):
            c, bars = fut.result()
            raw_klines[c] = bars
            done_r[0] += 1
            if done_r[0] % 200 == 0:
                print("   K线进度 %d/%d" % (done_r[0], len(need_close)),
                      flush=True)

    removed_low = []
    for code in list(stocks):
        per_year = div_map.get(code) or {}
        bars = raw_klines.get(code) or []
        yc = {}
        for b in bars:
            y = b["d"][:4]
            if y in (str(v) for v in yset):
                yc[y] = b["c"]  # 同年度最后出现=年末收盘（bars升序）
        yields = []
        for y in yset:
            d = per_year.get(y)
            c = yc.get(str(y))
            if d is None or not c:
                yields = None
                break
            yields.append(d / c * 100.0)
        if yields is None:
            continue  # 数据不足不误删
        if all(v < min_pct for v in yields):
            removed_low.append((code, stocks[code].get("name") or code))
            del stocks[code]

    # 上一年度亏损（最近一个完整年度归母净利润 < 0）
    loss_year = today.year - 1 if today.month >= 5 else today.year - 2
    try:
        profit_map = sm.fetch_annual_profit(loss_year)
    except Exception as exc:  # noqa: BLE001
        profit_map = {}
        print("   上年度净利润抓取失败: %s" % exc)
    removed_loss = []
    for code in list(stocks):
        p = profit_map.get(code)
        if p is not None and p < 0:
            removed_loss.append((code, stocks[code].get("name") or code))
            del stocks[code]

    print("   剔除: 连续%d年股息率<%.1f%% %d 只, %d年度亏损 %d 只"
          % (n_yield, min_pct, len(removed_low), loss_year, len(removed_loss)))
    examples2 = ["%s(%s)" % (c, n) for c, n in (removed_low + removed_loss)[:15]]
    print("   剔除示例: " + ", ".join(examples2))

    out = []
    for code, st in sorted(stocks.items()):
        tags = []
        # 注：「国有企业」标签改由 update_daily.py 依据公司基本面(is_state_owned)生成；
        # 「基金持仓」标签不再生成（需求：删除该标签）。
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

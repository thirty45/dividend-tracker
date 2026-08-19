# -*- coding: utf-8 -*-
"""红利/宽基指数基金列表与指标抓取。

数据源（均无需登录）：
  - 东方财富 pingzhongdata.js：单位净值历史(Data_netWorthTrend) + 规模季度历史(Data_fluctuationScale)
  - 乐咕乐股 index-basic：跟踪指数的 市盈率/市净率/股息率

产出：
  - data/funds.json          基金列表与全部派生指标
  - data/fund_nav/{code}.json 单只基金净值走势（最近约 1500 个交易日）
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.net import fetch  # noqa: E402
from pipeline import sources_funds as sf  # noqa: E402

# ---------------------------------------------------------------------------
# 指数映射 / 宽基关键词 / 排除词：优先读 config.json，缺失时回退到下列默认值。
# 顺序即优先级（红利类放前面，确保“红利”基金优先命中红利指数）
# ---------------------------------------------------------------------------
INDEX_MAP_DEFAULT = [
    # 红利类
    ["红利低波100", "930955.CSI", "红利低波100"],
    ["红利低波", "h30269.CSI", "红利低波"],
    ["深证红利", "399324.SZ", "深证红利"],
    ["上证红利", "000015.SH", "上证红利"],
    ["中证红利", "000922.SH", "中证红利"],
    ["红利", "000922.SH", "中证红利"],  # 兜底：泛“红利”ETF 归到中证红利
    # 宽基类
    ["沪深300", "000300.SH", "沪深300"],
    ["中证A500", "000510.SH", "中证A500"],
    ["中证A50", "000950.SH", "中证A50"],
    ["中证500", "000905.SH", "中证500"],
    ["中证1000", "000852.SH", "中证1000"],
    ["上证50", "000016.SH", "上证50"],
    ["创业板50", "399673.SZ", "创业板50"],
    ["创业板指", "399006.SZ", "创业板指"],
    ["科创50", "000688.SH", "科创50"],
    ["深证100", "399330.SZ", "深证100"],
    ["上证180", "000010.SH", "上证180"],
    ["中证800", "000906.SH", "中证800"],
    ["国证2000", "399303.SZ", "国证2000"],
    ["MSCI中国", "718711.CSI", "MSCI中国A股"],
]

BROAD_KEYWORDS_DEFAULT = [
    "沪深300", "中证500", "中证1000", "上证50", "创业板指", "创业板50",
    "科创50", "深证100", "上证180", "中证800", "国证2000",
    "中证A500", "中证A50", "MSCI中国",
]

EXCLUDE_NAMES_DEFAULT = (
    "医药", "医疗", "健康", "消费", "食品", "饮料", "白酒", "半导体", "芯片",
    "电子", "新能源", "光伏", "锂电", "电池", "汽车", "智能", "券商", "证券",
    "银行", "保险", "地产", "房地产", "军工", "国防", "航天", "环保", "公用",
    "传媒", "游戏", "计算机", "软件", "通信", "5G", "人工智能", "AI", "科技",
    "钢铁", "煤炭", "有色", "化工", "农业", "养殖", "旅游", "港股通", "恒生",
    "纳斯达克", "标普", "美股", "原油", "黄金", "商品", "债券", "货币",
    "短债", "信用", "量化", "对冲", "养老", "FOF", "QDII",
)


def _load_config():
    """读取项目根目录 config.json；缺失键回退到 *_DEFAULT。"""
    cfg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"
    )
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


_CFG = _load_config()
INDEX_MAP = _CFG.get("fund_index_map") or INDEX_MAP_DEFAULT
BROAD_KEYWORDS = _CFG.get("fund_broad_keywords") or BROAD_KEYWORDS_DEFAULT
EXCLUDE_NAMES = tuple(_CFG.get("fund_exclude_names") or EXCLUDE_NAMES_DEFAULT)

PINGZHONG_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
LEGULEGU_URL = "https://legulegu.com/stockdata/index-basic?indexCode={code}"
DIV_RE = re.compile(r"派现金([\d.]+)元")
NAV_KEEP = 1500  # 净值走势保留最近交易日点数


def detect_index(name):
    """根据基金名称推断跟踪指数（乐咕乐股代码 + 展示名）。"""
    for frag, code, disp in INDEX_MAP:
        if frag in name:
            return code, disp
    return None, None


def select_broad_index_funds(records):
    """从全量基金列表筛选宽基指数基金（排除行业/主题/海外/非权益）。"""
    chosen = {}
    for rec in records:
        code, name, ftype = rec[0], rec[2], rec[3]
        if not any(k in name for k in BROAD_KEYWORDS):
            continue
        if any(e in name for e in EXCLUDE_NAMES):
            continue
        if not sf._ok_type(ftype):
            continue
        b = sf._base_name(name)
        if b not in chosen or sf._class_rank(name) < sf._class_rank(chosen[b][1]):
            chosen[b] = (code, name, ftype)
    return sorted(chosen.values(), key=lambda x: x[0])


def build_universe(limit=None):
    """合并 红利基金 + 宽基指数基金，去重（红利优先）。"""
    records = sf.load_fund_list()
    dividends = sf.select_dividend_funds(records)
    broad = select_broad_index_funds(records)
    by_code = {}
    for code, name, ftype in dividends:
        by_code[code] = (code, name, ftype, "红利")
    for code, name, ftype in broad:
        if code not in by_code:
            by_code[code] = (code, name, ftype, "宽基")
    items = sorted(by_code.values(), key=lambda x: x[0])
    if limit:
        items = items[:limit]
    return items


# ---------------------------------------------------------------------------
# 解析东方财富 pingzhongdata.js
# ---------------------------------------------------------------------------
def parse_pingzhong(code):
    """返回 (nav_list, scale_list)。nav_list: [{x:ms,y:nav,unitMoney:str}]；
    scale_list: [{date:'YYYY-MM-DD', y:float, mom:str}]。"""
    js = fetch(PINGZHONG_URL.format(code=code),
               referer="https://fund.eastmoney.com/", timeout=30, retries=3, delay=1.0)
    m = re.search(r"var Data_netWorthTrend\s*=\s*(\[.*?\]);", js, re.S)
    nav = json.loads(m.group(1)) if m else []
    scale = []
    m2 = re.search(r"var Data_fluctuationScale\s*=\s*(\{.*?\});", js, re.S)
    if m2:
        sc = json.loads(m2.group(1))
        cats = sc.get("categories", [])
        series = sc.get("series", [])
        for d, s in zip(cats, series):
            y = s.get("y")
            if y is None:
                continue
            scale.append({"date": d, "y": float(y), "mom": s.get("mom")})
    return nav, scale


def pct_from_nav(nav, months):
    if not nav:
        return None
    today = datetime.date.today()
    target = today - datetime.timedelta(days=int(months * 30.44))
    tg_ms = int(time.mktime(target.timetuple()) * 1000)
    past = [p for p in nav if p.get("x", 0) <= tg_ms]
    if not past:
        return None
    p0, p1 = past[-1].get("y"), nav[-1].get("y")
    if not p0 or not p1:
        return None
    return round((p1 / p0 - 1) * 100, 2)


def dividend_stats(nav):
    """近12个月分红：次数 + 累计每份分红（元）。"""
    today = datetime.date.today()
    cutoff = int(time.mktime((today - datetime.timedelta(days=365)).timetuple()) * 1000)
    cnt, total = 0, 0.0
    for p in nav:
        um = p.get("unitMoney")
        if not um or p.get("x", 0) < cutoff:
            continue
        mm = DIV_RE.search(str(um))
        if mm:
            cnt += 1
            total += float(mm.group(1))
    return cnt, round(total, 4)


def scale_at(scale, months):
    """返回截至 (今天 - N 月) 之前最近一次披露的规模点，或 None。"""
    if not scale:
        return None
    today = datetime.date.today()
    target = today - datetime.timedelta(days=int(months * 30.44))
    tg = target.strftime("%Y-%m-%d")
    cand = [s for s in scale if s["date"] <= tg]
    return cand[-1] if cand else None


def build_scale_cells(scale_now, scale):
    """构造 当前 + 1/3/6/12月前 规模单元格。"""
    out = {}
    for n in (1, 3, 6, 12):
        cell = scale_at(scale, n) if scale else None
        if not cell or scale_now is None:
            out[n] = None
            continue
        val = cell["y"]
        delta = round(scale_now - val, 2)
        pct = round(delta / val * 100, 2) if val else None
        out[n] = {"date": cell["date"], "value": val,
                  "delta": delta, "pct": pct}
    return out


# ---------------------------------------------------------------------------
# 解析乐咕乐股指数估值
# ---------------------------------------------------------------------------
def parse_index_val(html):
    """从 index-basic 页面提取 市盈率/市净率/股息率(%)。

    数值位于 <meta name="description"> 中，格式如：
      “…最新加权平均市盈率：13.62，加权平均市净率：1.43，加权平均股息率：2.75…”
    优先解析该描述，避免页面其他位置 0.0 噪声。
    """
    m = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', html, re.S)
    desc = m.group(1) if m else ""

    def grab(label):
        mm = re.search(r"%s：([\d.]+)" % label, desc)
        if mm:
            try:
                v = float(mm.group(1))
                if v > 0:
                    return v
            except ValueError:
                pass
        return None

    pe = grab("加权平均市盈率") or grab("市盈率") or grab("PE-TTM") or grab("PE")
    pb = grab("加权平均市净率") or grab("市净率") or grab("PB")
    dy = grab("加权平均股息率") or grab("股息率")
    # 合理性过滤
    pe = pe if (pe and 0 < pe < 300) else None
    pb = pb if (pb and 0 < pb < 50) else None
    dy = dy if (dy and 0 <= dy < 30) else None
    return {"pe": pe, "pb": pb, "dy": dy}


def fetch_index_val(index_code):
    try:
        html = fetch(LEGULEGU_URL.format(code=index_code), timeout=20, retries=2, delay=0.5)
        return parse_index_val(html)
    except Exception:  # noqa: BLE001
        return {"pe": None, "pb": None, "dy": None}


# ---------------------------------------------------------------------------
# 单只基金指标汇总
# ---------------------------------------------------------------------------
def compute_one(item):
    code, name, ftype, ftype_tag = item
    try:
        nav, scale = parse_pingzhong(code)
    except Exception as exc:  # noqa: BLE001
        return {"code": code, "name": name, "type": ftype_tag,
                "error": "净值/规模抓取失败: %s" % exc}
    scale_now = scale[-1]["y"] if scale else None
    idx_code, idx_name = detect_index(name)
    val = fetch_index_val(idx_code) if idx_code else {"pe": None, "pb": None, "dy": None}
    div_cnt, div_sum = dividend_stats(nav)
    last_nav = nav[-1]["y"] if nav else None
    div_ratio = round(div_sum / last_nav * 100, 2) if (div_sum and last_nav) else None
    # 当天涨跌幅：最新净值 vs 上一净值
    nav_date = None
    chg_today = None
    if len(nav) >= 2:
        y0, y1 = nav[-2].get("y"), nav[-1].get("y")
        if y0 and y1:
            chg_today = round((y1 / y0 - 1) * 100, 2)
        nav_date = datetime.datetime.fromtimestamp(
            nav[-1].get("x", 0) / 1000).strftime("%Y-%m-%d")
    rec = {
        "code": code,
        "name": name,
        "type": ftype_tag,
        "index_name": idx_name,
        "nav_date": nav_date,
        "chg_today": chg_today,
        "pct_1m": pct_from_nav(nav, 1),
        "pct_3m": pct_from_nav(nav, 3),
        "pct_6m": pct_from_nav(nav, 6),
        "pct_12m": pct_from_nav(nav, 12),
        "div_count": div_cnt,
        "div_sum": div_sum,
        "div_ratio": div_ratio,
        "pe": val["pe"],
        "pb": val["pb"],
        "dy": val["dy"],
        "scale_now": scale_now,
        "scale_date": scale[-1]["date"] if scale else None,
        "scale_cells": build_scale_cells(scale_now, scale),
    }
    # 净值走势（最近 NAV_KEEP 个交易日）
    if nav:
        rec["_nav"] = [[
            datetime.datetime.fromtimestamp(p["x"] / 1000).strftime("%Y-%m-%d"),
            round(p["y"], 4),
        ] for p in nav[-NAV_KEEP:]]
    return rec


def dedup_broad(items):
    """宽基基金同指数去重：每个指数只保留「当前规模最大」的一只代表基金。

    红利类基金一只不删；无 index_name 的宽基不参与去重。
    规模缺失按 0 处理；规模并列时保留先出现的（按代码序）。
    """
    best = {}  # index_name -> item（规模最大）
    kept = []
    for it in items:
        if it.get("type") != "红利" and it.get("index_name"):
            key = it["index_name"]
            sc = it.get("scale_now") or 0
            cur = best.get(key)
            if cur is None or sc > (cur.get("scale_now") or 0):
                best[key] = it
        else:
            kept.append(it)
    kept.extend(best[k] for k in sorted(best))
    return kept


def build_funds(limit=None, workers=6, data_dir="data", progress=None):
    items = build_universe(limit=limit)
    if progress:
        progress("基金池：%d 只（红利+宽基）" % len(items))
    out_items = []
    nav_dir = os.path.join(data_dir, "fund_nav")
    os.makedirs(nav_dir, exist_ok=True)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(compute_one, it): it for it in items}
        done = 0
        for fut in as_completed(futs):
            rec = fut.result()
            done += 1
            if rec.get("_nav"):
                with open(os.path.join(nav_dir, rec["code"] + ".json"),
                          "w", encoding="utf-8") as f:
                    json.dump({"code": rec["code"], "name": rec["name"],
                               "nav": rec.pop("_nav")}, f, ensure_ascii=False)
            out_items.append(rec)
            if progress and done % 25 == 0:
                progress("基金进度 %d/%d" % (done, len(items)))
    out_items = [it for it in out_items if (it.get("scale_now") or 0) >= 0.5]
    out_items = dedup_broad(out_items)
    out_items.sort(key=lambda x: (x.get("type") != "红利", x["code"]))
    payload = {
        "date": datetime.date.today().isoformat(),
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "total": len(out_items),
        "items": out_items,
    }
    with open(os.path.join(data_dir, "funds.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    if progress:
        progress("已写出 data/funds.json（%d 只）" % len(out_items))
    return payload


def main():
    ap = argparse.ArgumentParser(description="构建红利/宽基指数基金列表与指标")
    ap.add_argument("--limit", type=int, default=None, help="仅处理前 N 只（调试）")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--code", default=None, help="仅抓取单只基金代码（调试）")
    args = ap.parse_args()

    if args.code:
        name = args.code
        try:
            recs = sf.load_fund_list()
            for r in recs:
                if r[0] == args.code:
                    name = r[2]
                    break
        except Exception:  # noqa: BLE001
            pass
        rec = compute_one((args.code, name, "指数型-股票", "调试"))
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return
    build_funds(limit=args.limit, workers=args.workers, data_dir=args.data_dir,
                progress=print)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""红利基金列表与近5年持仓抓取（天天基金/东方财富 F10）。"""

import random
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from .net import fetch

FUND_LIST_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
FUND_HOLD_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
FUND_REFERER = "https://fundf10.eastmoney.com/"

HL = "红利"
EXCLUDE_NAMES = (
    "改革", "混改", "港股", "恒生", "香港", "海外", "美股",
    "纳斯达克", "美元", "日结",
)


def _ok_type(fund_type):
    """只保留指数型-股票（被动/增强指数基金），剔除混合型/股票型等主动选股基金。"""
    if not fund_type:
        return False
    return fund_type.startswith("指数型-股票")


def _base_name(name):
    n = name.replace("联接", "")
    if n.endswith("ETF") or n.endswith("LOF") or n.endswith("QDII"):
        return n
    return re.sub(r"[A-Z]$", "", n)


def _class_rank(name):
    for i, c in enumerate("ABCDEFIY"):
        if name.endswith(c):
            return i
    return 99


def load_fund_list():
    """返回天天基金全量基金列表 [[code, pinyin, name, type, pinyin2], ...]。"""
    text = fetch(FUND_LIST_URL, referer="https://fund.eastmoney.com/",
                 encoding="utf-8-sig", timeout=30, retries=3, delay=1.0)
    arr = json_loads_js_array(text)
    return arr


def json_loads_js_array(text):
    import json
    start = text.index("[")
    end = text.rindex("]") + 1
    return json.loads(text[start:end])


def select_dividend_funds(records):
    """筛选名字含“红利”且非改革/港股/债/货币类基金，并按主份额去重。"""
    chosen = {}
    for rec in records:
        code, name, fund_type = rec[0], rec[2], rec[3]
        if HL not in name:
            continue
        if any(e in name for e in EXCLUDE_NAMES):
            continue
        if not _ok_type(fund_type):
            continue
        b = _base_name(name)
        if b not in chosen or _class_rank(name) < _class_rank(chosen[b][1]):
            chosen[b] = (code, name, fund_type)
    return sorted(chosen.values(), key=lambda x: x[0])


def _parse_holdings(text):
    """解析基金持仓 JS 返回内容 -> (records, available_years)。"""
    years = []
    m = re.search(r"arryear:\[([^\]]*)\]", text)
    if m:
        years = [int(y) for y in m.group(1).split(",") if y.strip().isdigit()]
    cm = re.search(r'content:"(.*)",\s*arryear:', text, re.S)
    content = ""
    if cm:
        content = cm.group(1).replace('\\"', '"').replace("\\'", "'")
    blocks = re.split(r"<div class='box'><div class='boxitem w790'>", content)
    records = []
    for blk in blocks[1:]:
        qm = re.search(r"(\d{4})年(\d)季度", blk)
        if not qm:
            continue
        dm = re.search(r"截止至：<font class='px12'>(\d{4}-\d{2}-\d{2})</font>", blk)
        end_date = dm.group(1) if dm else ""
        row_re = (
            r"<td>(\d+)</td><td><a href='[^']*'>(\d{6})</a></td>"
            r"<td class='tol'><a href='[^']*'>([^<]+)</a></td>.*?"
            r"<td class='tor'>([\d.]+)%</td>"
        )
        for rm in re.finditer(row_re, blk, re.S):
            records.append({
                "quarter": "%sQ%s" % (qm.group(1), qm.group(2)),
                "end_date": end_date,
                "seq": int(rm.group(1)),
                "code": rm.group(2),
                "name": rm.group(3).strip(),
                "pct": float(rm.group(4)),
            })
    return records, years


def fetch_fund_holdings(code, min_year=2020):
    """抓取单只基金各季度前十大持仓。"""
    def one(params):
        return fetch(FUND_HOLD_URL, params=params, referer=FUND_REFERER,
                     timeout=25, retries=6, delay=2.5)

    rt = "0.%06d" % random.randint(1, 999999)
    text = one({"type": "jjcc", "code": code, "topline": "10",
                "year": "", "month": "", "rt": rt})
    records, years = _parse_holdings(text)
    years = [y for y in years if y >= min_year]
    for y in years:
        rt = "0.%06d" % random.randint(1, 999999)
        t = one({"type": "jjcc", "code": code, "topline": "10",
                 "year": str(y), "month": "", "rt": rt})
        recs, _ = _parse_holdings(t)
        records.extend(recs)

    seen = set()
    out = []
    for r in records:
        key = (r["end_date"], r["code"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    out.sort(key=lambda r: (r["end_date"], r["seq"]))
    return out


def fetch_many_holdings(funds, workers=6, min_year=2020, progress=None):
    """并发抓取多只基金持仓。funds: [(code, name, type), ...]"""
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_fund_holdings, code, min_year): code
                for code, _, _ in funds}
        done = 0
        for fut in as_completed(futs):
            code = futs[fut]
            try:
                results[code] = fut.result()
            except Exception as exc:  # noqa: BLE001
                results[code] = []
                if progress:
                    progress("基金 %s 抓取失败: %s" % (code, exc))
            done += 1
            if progress and done % 25 == 0:
                progress("基金进度 %d/%d" % (done, len(funds)))
    return results

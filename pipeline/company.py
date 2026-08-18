# -*- coding: utf-8 -*-
"""公司基本面数据抓取与清洗：控股股东/实控人、分红、财务绩效、高管增减持、公司简介。

所有数据来自东方财富公开接口（emweb F10 PageAjax + datacenter-web），仅用标准库。
输出结构见 fetch_company() 返回的 dict，供 update_daily.py 写入 data/company/<code>.json。
"""

import datetime
import json
import os
import re
import urllib.request

from .net import fetch, fetch_json

EMWEB = "https://emweb.securities.eastmoney.com/PC_HSF10/%s/PageAjax?code=%s"
DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
# 高管增减持(RPTA_WEB_GGMX)需要在东财网页 JS 中硬编码的公开 token，直接复用即可。
GGMX_TOKEN = "28dfeb41d35cc81d84b4664d7c23c49f"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 财务绩效指标分组： (MAINFINADATA列名, 中文名, 展示单位, 数值换算除数)
# unit: 亿=除以1e8；元/%=不换算；人力投入回报率用 人均净利润(AVG_NET_PROFIT, 元/人) 近似。
FIN_GROUPS = {
    "key": [
        ("TOTALOPERATEREVE", "营业收入", "亿", 1e8),
        ("PARENTNETPROFIT", "净利润", "亿", 1e8),
        ("KCFJCXSYJLR", "扣非净利润", "亿", 1e8),
    ],
    "profit": [
        ("EPSJB", "每股收益", "元", 1),
        ("BPS", "每股净资产", "元", 1),
        ("ROEJQ", "净资产收益率", "%", 1),
        ("XSMLL", "销售毛利率", "%", 1),
        ("XSJLL", "销售净利率", "%", 1),
        ("AVG_NET_PROFIT", "人力投入回报率", "元/人", 1),
    ],
    "risk": [
        ("ZCFZL", "资产负债率", "%", 1),
        ("LD", "流动比率", "", 1),
        ("SD", "速动比率", "", 1),
    ],
}


# 国有企业判定关键词（匹配实际控制人/控股股东名称）
STATE_KEYWORDS = ("国资委", "国有", "国务院", "财政部", "国资", "汇金", "证金",
                  "社保基金", "中国烟草", "中央汇金")

# 财务绩效收录起始年（需求：2016 年至今）
FIN_START_YEAR = 2016


def _is_state_owned(name):
    """实际控制人/控股股东名称含国资相关关键词即视为国有。"""
    if not name:
        return False
    return any(k in name for k in STATE_KEYWORDS)


def _num(v, div=1):
    """安全地把值除以 div 转成 float；None/空/异常返回 None。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if div and div != 1:
        f = f / div
    return f


def _emweb(code, ctrl):
    """调用 emweb F10 PageAjax，返回解析后的 dict（失败返回 {}）。"""
    market = "SH" if code.startswith("6") else "SZ"
    url = EMWEB % (ctrl, market + code)
    try:
        text = fetch(url, referer="https://emweb.securities.eastmoney.com/",
                     timeout=25, retries=3, delay=1.0)
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return {}


def _dc(report, flt, columns="ALL", sort="", token=None, page_size=300):
    """调用 datacenter-web 报表接口，返回 data 列表（失败返回 []）。"""
    p = {
        "reportName": report,
        "columns": columns,
        "filter": flt,
        "pageSize": str(page_size),
        "pageNumber": "1",
        "sortColumns": sort,
        "sortTypes": "-1",
        "source": "WEB",
        "client": "WEB",
    }
    if token:
        p["token"] = token
    try:
        j = fetch_json(DC, params=p, referer="https://data.eastmoney.com/",
                       timeout=25, retries=3, delay=1.0)
    except Exception:  # noqa: BLE001
        return []
    res = j.get("result") or {}
    return res.get("data") or []


def fetch_finance(code):
    """主要财务指标：抓取多报告期，清洗为 periods 序列 + 分组展示结构。"""
    rows = _dc("RPT_F10_FINANCE_MAINFINADATA", '(SECURITY_CODE="%s")' % code,
              sort="", page_size=300)
    if not rows:
        return {"periods": [], "groups": {"key": [], "profit": [], "risk": []}}

    # 解析每条报告期为统一结构
    periods = []
    annual_netprofit = {}  # 年份 -> 年度净利润(亿)，用于分红比率
    for r in rows:
        rd = (r.get("REPORT_DATE") or "")[:10]
        if len(rd) < 4:
            continue
        rtype = (r.get("REPORT_TYPE") or "").strip()
        try:
            year = int(rd[:4])
        except ValueError:
            continue
        if rtype in ("年报",):
            g = "annual"
        elif rtype in ("中报", "半年报"):
            g = "half"
        elif rtype in ("一季报",):
            g = "q1"
        elif rtype in ("三季报",):
            g = "q3"
        else:
            g = "other"
        if g == "other":
            continue
        vals = {}
        for col, _, _, div in sum(FIN_GROUPS.values(), []):
            vals[col] = _num(r.get(col), div)
        periods.append({
            "date": rd, "year": year, "type": g, "rtype": rtype,
            "vals": vals,
        })
        if g == "annual":
            npv = vals.get("PARENTNETPROFIT")
            if npv is not None:
                annual_netprofit[year] = npv

    # 同比：按 (type, year) 找上一年同类型
    def find_prev(g, year):
        for p in periods:
            if p["type"] == g and p["year"] == year - 1:
                return p
        return None

    for p in periods:
        p["yoy"] = {}
        for col, _, _, _ in sum(FIN_GROUPS.values(), []):
            v = p["vals"].get(col)
            prev = find_prev(p["type"], p["year"])
            pv = prev["vals"].get(col) if prev else None
            if v is not None and pv not in (None, 0):
                p["yoy"][col] = round((v - pv) / abs(pv) * 100.0, 2)
            else:
                p["yoy"][col] = None

    # 组装分组展示：取各报告类型最新一条（一季报/中报/三季报/年报）
    def latest_of(g):
        cands = [p for p in periods if p["type"] == g]
        if not cands:
            return None
        return max(cands, key=lambda p: p["date"])

    groups = {"key": [], "profit": [], "risk": []}
    for gkey, items in FIN_GROUPS.items():
        for col, name, unit, _ in items:
            rec = {"key": col, "name": name, "unit": unit,
                   "q1": None, "half": None, "q3": None, "annual": None}
            for g in ("q1", "half", "q3", "annual"):
                lp = latest_of(g)
                if lp:
                    rec[g] = {
                        "v": lp["vals"].get(col),
                        "yoy": lp["yoy"].get(col),
                        "date": lp["date"],
                    }
            groups[gkey].append(rec)

    # 保留全部报告期用于画图与历史回溯（2016 至今）
    periods_sorted = sorted(periods, key=lambda p: p["date"], reverse=True)
    periods_out = [
        {k: p.get(k) for k in ("date", "year", "type", "rtype", "vals", "yoy")}
        for p in periods_sorted if p["year"] >= FIN_START_YEAR
    ]
    return {"periods": periods_out, "groups": groups,
            "annual_netprofit": annual_netprofit}


def fetch_holders(code):
    """十大股东 + 实际控制人。"""
    sr = _emweb(code, "ShareholderResearch")
    top10 = []
    for h in (sr.get("sdgd") or [])[:10]:
        top10.append({
            "rank": h.get("HOLDER_RANK"),
            "name": h.get("HOLDER_NAME"),
            "shares": _num(h.get("HOLD_NUM")),
            "ratio": _num(h.get("HOLD_NUM_RATIO")),
            "change": h.get("HOLD_NUM_CHANGE"),    # 文字：不变/增持/减持 或数量
            "change_ratio": _num(h.get("CHANGE_RATIO")),
        })
    controller = None
    sjkzr = sr.get("sjkzr") or []
    if sjkzr:
        controller = sjkzr[0].get("HOLDER_NAME")
    # 控股股东取十大股东中持股比例最高者
    controller_shareholder = top10[0]["name"] if top10 else None
    return {"controller": controller,
            "controller_shareholder": controller_shareholder,
            "is_state_owned": _is_state_owned(controller or controller_shareholder),
            "top10": top10}


def fetch_dividend(code, annual_netprofit):
    """分红数据：按「报告期年度」归属（2025 年 = 2025 年报期宣告的分红，
    而非 2025 年实际派发；派发日归属会把 2024 年报的分红错记到 2025 年）。

    数据源：datacenter-web RPT_SHAREBONUS_DET（含 REPORT_DATE 报告期、
    IMPL_PLAN_PROFILE 方案文本、PRETAX_BONUS_RMB 每10股派息、BONUS_RATIO/IT_RATIO
    每10股送/转股数、EX_DIVIDEND_DATE 除权日）。

    每个报告年度合并一条：每股现金分红=该年度各笔(中期+年度)合计；
    送/转股比例=合计；方案文本=各笔合并；total_div=Σ(每股派息×总股本)。
    配送股现金等价（除权后开盘价×每股配送股数）由 update_daily.py 用 K线补算为
    ex_open / per_share_real。"""
    rows = _dc("RPT_SHAREBONUS_DET", '(SECURITY_CODE="%s")' % code,
               sort="REPORT_DATE", page_size=200)
    by_year = {}
    for r in rows:
        plan = r.get("IMPL_PLAN_PROFILE") or ""
        if not plan or "不分配" in plan or "不派" in plan:
            continue
        rp = (r.get("REPORT_DATE") or "")[:10]
        if len(rp) != 10 or not rp[:4].isdigit():
            continue
        year = int(rp[:4])
        per10 = _num(r.get("PRETAX_BONUS_RMB"))   # 每10股派息(元)
        send10 = _num(r.get("BONUS_RATIO"))       # 每10股送(股)
        trans10 = _num(r.get("IT_RATIO"))         # 每10股转增(股)
        shares = _num(r.get("TOTAL_SHARES"))      # 总股本(股)
        g = by_year.setdefault(year, {
            "per_share": 0.0, "send_ratio": 0.0, "trans_ratio": 0.0,
            "total_div": 0.0, "plans": [], "ex_dates": [], "has_st": False,
        })
        if per10 is not None:
            g["per_share"] += per10 / 10.0
            if shares:
                g["total_div"] += per10 / 10.0 * shares
        if send10:
            g["send_ratio"] += send10 / 10.0
        if trans10:
            g["trans_ratio"] += trans10 / 10.0
        if send10 or trans10:
            g["has_st"] = True
        if plan not in g["plans"]:
            g["plans"].append(plan)
        ex_date = (r.get("EX_DIVIDEND_DATE") or "")[:10]
        if len(ex_date) == 10:
            g["ex_dates"].append(ex_date)
    years = []
    for y in sorted(by_year.keys(), reverse=True):
        g = by_year[y]
        # 除权日：优先取该年度含送/转的那笔；否则取最后实施的一笔
        ex_date = ""
        if g["has_st"]:
            for r in rows:
                rp = (r.get("REPORT_DATE") or "")[:10]
                if rp[:4] == str(y) and ((_num(r.get("BONUS_RATIO")) or 0) or
                                         (_num(r.get("IT_RATIO")) or 0)):
                    ex_date = (r.get("EX_DIVIDEND_DATE") or "")[:10]
                    if len(ex_date) == 10:
                        break
        if not ex_date and g["ex_dates"]:
            ex_date = g["ex_dates"][-1]
        npv = annual_netprofit.get(y)
        ratio = (round(g["total_div"] / 1e8 / npv * 100.0, 2)
                 if npv and g["total_div"] else None)
        years.append({
            "year": y,
            "report_date": "%d-12-31" % y,        # 报告期年度（合并键）
            "total_div": round(g["total_div"] / 1e8, 2),   # 亿
            "ratio": ratio,                       # 分红比率 %
            "per_share": round(g["per_share"], 4) or None,   # 每股现金分红(元)
            "send_ratio": round(g["send_ratio"], 4) or None,  # 每股送股
            "trans_ratio": round(g["trans_ratio"], 4) or None,  # 每股转增
            "plan": "；".join(g["plans"]),
            "ex_date": ex_date or None,           # 除权除息日
            "ex_open": None,                      # 除权后开盘价（K线补算）
            "per_share_real": None,               # 每股真实分红=现金+配送股折现（K线补算）
        })
    years.sort(key=lambda x: x["year"], reverse=True)
    # 近5年平均分红额度：仅统计已实施(总额>0)的年份
    recent5 = [y for y in years if y["total_div"]][:5]
    avg5 = round(sum(y["total_div"] for y in recent5) / len(recent5), 2) if recent5 else None
    return {"avg5": avg5, "years": years}


def fetch_executives(code):
    """高管增减持（向东财富高管持股变动明细）。"""
    rows = _dc("RPTA_WEB_GGMX", '(SCODE="%s")' % code, token=GGMX_TOKEN,
              sort="TDATE", page_size=50)
    out = []
    for r in rows:
        out.append({
            "date": (r.get("TDATE") or "")[:10],
            "name": r.get("GGXM") or r.get("BDR"),
            "title": r.get("ZW"),
            "direction": r.get("BDFX"),         # 增持/减持
            "shares": _num(r.get("CHANNUM")),   # 有正负
            "price": _num(r.get("CJJJ")),       # 成交均价
            "amount": _num(r.get("BDJE")),       # 变动金额(元)
            "reason": r.get("BDYY"),
            "after": _num(r.get("BDHCG")),       # 变动后持股
            "relation": r.get("GX"),
        })
    out.sort(key=lambda x: x["date"] or "", reverse=True)
    return out


def fetch_profile(code):
    """公司基本资料与简介。"""
    cs = _emweb(code, "CompanySurvey")
    jb = (cs.get("jbzl") or [{}])[0]
    return {
        "org_name": jb.get("ORG_NAME"),
        "org_name_en": jb.get("ORG_NAME_EN"),
        "chairman": jb.get("CHAIRMAN"),
        "legal_person": jb.get("LEGAL_PERSON"),
        "president": jb.get("PRESIDENT"),
        "secretary": jb.get("SECRETARY"),
        "industry": jb.get("EM2016") or jb.get("INDUSTRYCSRC1"),
        "listing": jb.get("SECURITY_TYPE"),
        "address": jb.get("ADDRESS"),
        "reg_address": jb.get("REG_ADDRESS"),
        "emp_num": _num(jb.get("EMP_NUM")),
        "business_scope": jb.get("BUSINESS_SCOPE"),
        "profile": (jb.get("ORG_PROFILE") or "").strip(),
        "website": jb.get("ORG_WEB"),
    }


def fetch_company(code, name=""):
    """汇总一只股票的全部公司信息。单模块失败不影响其余。"""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    finance = fetch_finance(code)
    out = {
        "code": code,
        "name": name or code,
        "updated": now,
        "holders": fetch_holders(code),
        "dividend": fetch_dividend(code, finance.get("annual_netprofit", {})),
        "finance": {"periods": finance.get("periods", []),
                    "groups": finance.get("groups", {})},
        "executives": fetch_executives(code),
        "profile": fetch_profile(code),
    }
    return out


if __name__ == "__main__":
    import sys
    c = sys.argv[1] if len(sys.argv) > 1 else "600900"
    print(json.dumps(fetch_company(c, c), ensure_ascii=False, indent=1))

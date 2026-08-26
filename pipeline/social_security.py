# -*- coding: utf-8 -*-
"""社保基金调仓跟踪：从东财F10十大股东/十大流通股东提取社保组合持股变动。

数据源：emweb PC_HSF10 ShareholderResearch（无需登录）
  - sdgd / sdltgd：十大股东 / 十大流通股东
  - END_DATE 报告期、HOLD_NUM_CHANGE 较上期变动数量（股，负数=减持）
  - HOLDER_TYPE="全国社保基金" 或名称含"社保"的组合

产出：data/meta/social_security.json
  {"report_date": "2026-06-30", "updated_at": ISO,
   "items": [{code,name,holder,end_date,hold_num,hold_ratio,
              change_num,change_ratio,direction}]}
  direction: 增持 / 减持（"新进"按增持处理，"不变"忽略）

缓存：报告期不变且 7 天内不重抓（股东数据按季更新，无需每日全量请求）。
"""

import datetime
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from pipeline.net import fetch  # noqa: E402

EMWEB = ("https://emweb.securities.eastmoney.com/PC_HSF10/"
         "ShareholderResearch/PageAjax?code=%s%s")
CACHE_PATH = os.path.join(BASE, "data", "meta", "social_security.json")
CACHE_DAYS = 7


def _market(code):
    return "SH" if code.startswith(("6", "9", "5")) else "SZ"


def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_social_one(code, name):
    """抓单只股票的社保组合持仓变动，返回记录列表（无则空列表）。"""
    out = []
    try:
        t = fetch(EMWEB % (_market(code), code),
                  referer="https://emweb.securities.eastmoney.com/",
                  timeout=25, retries=3, delay=1.0)
        j = json.loads(t)
    except Exception:  # noqa: BLE001
        return out
    seen = set()
    for arr in ("sdgd", "sdltgd"):
        for r in (j.get(arr) or []):
            holder = r.get("HOLDER_NAME") or ""
            htype = r.get("HOLDER_TYPE") or ""
            if not (("社保" in holder) or ("社保" in htype)
                    or ("社会保障" in holder)):
                continue
            end = (r.get("END_DATE") or "")[:10]
            key = (holder, end)
            if key in seen:
                continue
            seen.add(key)
            chg = str(r.get("HOLD_NUM_CHANGE") or "")
            if chg in ("不变", "持平", ""):
                continue
            hold_num = _num(r.get("HOLD_NUM"))
            change_num = None
            if chg == "新进":
                direction = "增持"
                change_num = int(hold_num) if hold_num else None
            elif chg in ("增持", "减持"):
                direction = chg
            else:
                try:
                    change_num = int(float(chg))
                except ValueError:
                    continue
                direction = ("增持" if change_num > 0
                             else "减持" if change_num < 0 else None)
            if direction is None:
                continue
            out.append({
                "code": code,
                "name": name,
                "holder": holder,
                "end_date": end,
                "hold_num": hold_num,
                "hold_ratio": (_num(r.get("HOLD_NUM_RATIO"))
                               or _num(r.get("FREE_HOLDNUM_RATIO"))),
                "change_num": change_num,
                "change_ratio": _num(r.get("CHANGE_RATIO")),
                "direction": direction,
            })
    return out


def _probe_report_date(code):
    """取一只股票当前十大股东的报告期（YYYY-MM-DD），用于判断是否换季。"""
    try:
        t = fetch(EMWEB % (_market(code), code),
                  referer="https://emweb.securities.eastmoney.com/",
                  timeout=25, retries=2, delay=1.0)
        j = json.loads(t)
        for arr in ("sdgd", "sdltgd"):
            rows = j.get(arr) or []
            if rows:
                return (rows[0].get("END_DATE") or "")[:10]
    except Exception:  # noqa: BLE001
        pass
    return ""


def build():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    watch = json.load(open(os.path.join(
        BASE, "data", "meta", "watchlist.json"), encoding="utf-8"))
    codes = [s["code"] for s in watch["stocks"]]
    names = {s["code"]: s["name"] for s in watch["stocks"]}
    print("股票池 %d 只" % len(codes), flush=True)

    cache = {}
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:  # noqa: BLE001
            cache = {}
    now = datetime.datetime.now()
    report_date = _probe_report_date(codes[0]) if codes else ""
    fresh = False
    if report_date and cache.get("report_date") == report_date:
        try:
            upd = datetime.datetime.fromisoformat(
                cache.get("updated_at") or "")
            fresh = (now - upd).total_seconds() < CACHE_DAYS * 86400
        except ValueError:
            fresh = False
    if fresh:
        print("报告期 %s 缓存最新，跳过" % report_date)
        return 0

    print("抓取社保基金持仓变动（报告期 %s）..." % (report_date or "未知"),
          flush=True)
    items = []
    done = [0]

    def _one(c):
        return c, fetch_social_one(c, names.get(c, c))

    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(_one, c): c for c in codes}
        for fut in as_completed(futs):
            try:
                c, recs = fut.result()
            except Exception:  # noqa: BLE001
                c = futs[fut]
                recs = []
            items.extend(recs)
            done[0] += 1
            if done[0] % 200 == 0:
                print("   进度 %d/%d" % (done[0], len(codes)), flush=True)

    # 同一股票同一社保组合去重（十大股东/流通股东可能重复出现），保留变动更显著者
    best = {}
    for it in items:
        key = (it["code"], it["holder"], it["end_date"])
        old = best.get(key)
        if old is None or abs(it["change_num"] or 0) > abs(old["change_num"] or 0):
            best[key] = it
    items = sorted(best.values(), key=lambda x: (
        x["end_date"] or "", x["code"]), reverse=True)

    inc = [x for x in items if x["direction"] == "增持"]
    dec = [x for x in items if x["direction"] == "减持"]
    out = {
        "report_date": report_date,
        "updated_at": now.isoformat(timespec="seconds"),
        "total": {"inc": len(inc), "dec": len(dec)},
        "items": items,
    }
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print("已写 %s | 增持 %d 条 · 减持 %d 条" % (CACHE_PATH, len(inc), len(dec)),
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(build())

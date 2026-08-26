# -*- coding: utf-8 -*-
"""国企红利模块：从红利股票池中筛选实际控制人为 央企 / 地方国企 / 政府部门 的股票。

数据源：本地 data/company/<code>.json 的 holders.controller（实际控制人）——
无需联网，由每日 update_daily 缓存的公司基本面提供。

分类规则（按实控人名称关键词，顺序优先）：
  1) 央企：国务院 / 中央汇金 / 中国烟草总公司
  2) 政府部门：财政部 / 财政厅 / 财政局 等含"财政"
  3) 地方国企：各级国资委 / 国资办 / 国有资本运营公司 等
  社保基金组合等并非国企实控人，排除。

产出：data/meta/soe.json
  {"date": ..., "updated_at": ..., "total": {央企, 地方国企, 政府部门},
   "items": [{code,name,controller,category}]}
"""

import datetime
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

KEYS_CENTRAL = ("国务院", "中央汇金", "中国烟草总公司")
KEYS_GOV = ("财政",)
KEYS_LOCAL = ("国有资产监督管理", "国有资产经营", "国有资产管理", "国有资产投资",
              "国有股权", "国有资本", "国资经营", "国资投资", "国资运营",
              "国资管理", "国资局", "国资办", "国资中心", "国有文化资产")
KEYS_PENSION = ("社保基金", "社会保障基金", "全国社保")


def classify_controller(c):
    """返回 央企/地方国企/政府部门/None。"""
    if not c:
        return None
    if any(k in c for k in KEYS_PENSION):
        return None
    if any(k in c for k in KEYS_CENTRAL):
        return "央企"
    if any(k in c for k in KEYS_GOV):
        return "政府部门"
    if any(k in c for k in KEYS_LOCAL):
        return "地方国企"
    return None


def build():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    watch = json.load(open(os.path.join(
        BASE, "data", "meta", "watchlist.json"), encoding="utf-8"))
    codes = {s["code"] for s in watch["stocks"]}
    cdir = os.path.join(BASE, "data", "company")
    items = []
    for fn in os.listdir(cdir):
        c = fn[:-5]
        if not fn.endswith(".json") or c not in codes:
            continue
        try:
            d = json.load(open(os.path.join(cdir, fn), encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        h = d.get("holders") or {}
        if not h.get("is_state_owned"):
            continue
        ctrl = h.get("controller") or h.get("controller_shareholder") or ""
        cat = classify_controller(ctrl)
        if cat is None:
            continue
        items.append({
            "code": c,
            "name": d.get("name") or c,
            "controller": ctrl,
            "category": cat,
        })
    items.sort(key=lambda x: (x["category"], x["code"]))
    total = {
        "央企": sum(1 for x in items if x["category"] == "央企"),
        "地方国企": sum(1 for x in items if x["category"] == "地方国企"),
        "政府部门": sum(1 for x in items if x["category"] == "政府部门"),
    }
    out = {
        "date": datetime.date.today().isoformat(),
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "total": total,
        "items": items,
    }
    path = os.path.join(BASE, "data", "meta", "soe.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print("已写 %s | 央企 %d · 地方国企 %d · 政府部门 %d"
          % (path, total["央企"], total["地方国企"], total["政府部门"]),
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(build())

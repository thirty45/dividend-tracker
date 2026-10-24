# -*- coding: utf-8 -*-
"""从分红 profile 解析送转比例。

站内 data/dividends 常见写法：
  10派2.28元...
  10转4.00派3.00元...
  10送3股转1股派2.10元...
  10转5.00

股本调整倍数 = (基数 + 送 + 转) / 基数；无送转则返回 1.0。
"""
from __future__ import annotations

import re
from typing import Optional

_SEND = re.compile(r"(\d+(?:\.\d+)?)\s*送\s*(\d+(?:\.\d+)?)\s*股?")
# 「10转4」整段；或送转连写里的「转1股」
_TRANSFER = re.compile(r"(?:(\d+(?:\.\d+)?)\s*)?转(?:增)?\s*(\d+(?:\.\d+)?)\s*股?")


def parse_share_ratio(profile: Optional[str]) -> float:
    """返回除权后股本相对除权前的倍数；无法解析或无送转时为 1.0。"""
    if not profile:
        return 1.0
    text = str(profile).replace(" ", "")
    base, send, transfer = 10.0, 0.0, 0.0
    m = _SEND.search(text)
    if m:
        base = float(m.group(1))
        send = float(m.group(2))
    # 从「送」匹配结束处往后找转，避免「3股转1」把 3 当成基数
    search_from = m.end() if m else 0
    m2 = _TRANSFER.search(text, search_from)
    if not m2 and not m:
        m2 = _TRANSFER.search(text)
    if m2:
        if m2.group(1) and not m:
            base = float(m2.group(1))
        transfer = float(m2.group(2))
    if send == 0 and transfer == 0:
        return 1.0
    if base <= 0:
        return 1.0
    return (base + send + transfer) / base


def cash_per_share(event: dict) -> float:
    """每股现金红利（税前）。优先 cash_per10/10。"""
    v = event.get("cash_per10")
    if v is None:
        return 0.0
    try:
        return float(v) / 10.0
    except (TypeError, ValueError):
        return 0.0


def event_share_ratio(event: dict) -> float:
    return parse_share_ratio(event.get("profile"))

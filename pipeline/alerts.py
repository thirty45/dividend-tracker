# -*- coding: utf-8 -*-
"""股息率阈值提醒：跌破/涨破检测 + 微信推送（Server酱 / 企业微信机器人）。"""

import json
import os
import urllib.parse
import urllib.request


def load_rules(path):
    """读取提醒规则 data/meta/alerts.json -> [{code,name,dy_below,dy_above}]"""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("rules", []) or []


def check_crossings(rules, snapshot_items, history_dir):
    """对比今日与昨日股息率，返回阈值穿越事件列表。

    事件字段: code, name, kind(跌破/涨破), threshold, cur, prev
    首次纳入或昨日无数据时不提醒，避免误报。
    """
    by_code = {it["code"]: it for it in snapshot_items}
    events = []
    for r in rules:
        code = r.get("code")
        if not code:
            continue
        it = by_code.get(code)
        if not it or it.get("dy") is None:
            continue
        cur = it["dy"]
        prev = None
        hpath = os.path.join(history_dir, code + ".json")
        if os.path.exists(hpath):
            try:
                with open(hpath, encoding="utf-8") as f:
                    hist = json.load(f)
                if len(hist) >= 2:
                    prev = hist[-2].get("dy")
            except Exception:  # noqa: BLE001
                prev = None
        if prev is None or cur == prev:
            continue
        name = r.get("name") or it.get("name") or code
        tb = r.get("dy_below")
        if tb is not None and prev >= tb > cur:
            events.append({
                "code": code, "name": name, "kind": "跌破",
                "threshold": tb, "cur": cur, "prev": prev})
        ta = r.get("dy_above")
        if ta is not None and prev <= ta < cur:
            events.append({
                "code": code, "name": name, "kind": "涨破",
                "threshold": ta, "cur": cur, "prev": prev})
    return events


def _post(url, data, headers):
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def send_serverchan(send_key, title, desp):
    """Server酱(方糖)推送。send_key 形如 SCTxxxx。"""
    data = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
    return _post("https://sctapi.ftqq.com/%s.send" % send_key, data, {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0"})


def send_wecom(webhook, title, desp):
    """企业微信群机器人推送（markdown）。"""
    payload = json.dumps({
        "msgtype": "markdown",
        "markdown": {"content": "**%s**\n\n%s" % (title, desp)},
    }, ensure_ascii=False).encode("utf-8")
    return _post(webhook, payload, {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"})

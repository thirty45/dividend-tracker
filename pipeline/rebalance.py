# -*- coding: utf-8 -*-
"""重要事件 / 调仓提醒：定期审视日历 + 已公告调入调出。

产出 data/meta/rebalance.json，供首页表格读取。

跟踪范围（投资Bot 收窄后的名单）：
  000922 中证红利          每年 12 月
  H30269 中证红利低波动    每年 12 月
  000015 上证红利          每年 12 月
  000016 上证50            每年 6 月、12 月
  000300 沪深300           每年 6 月、12 月

低波只收 H30269。930955 的官方名称是「中证红利低波动100」，季度调整，
与 H30269 不是同一指数；本站红利低波主 ETF 512890 跟踪 H30269。

生效日按编制方案取「6 月或 12 月第二个星期五的下一交易日」。
公告原文常写「该星期五收市后生效」，JSON 里 notice_effective 保留这一天。
交易日只跳过周末，不内置交易所休市日历；2026-06-15 与 2026-12-14 均为交易日。

刷新：
  1. 拉取上交所指数信息披露列表，打开标题命中上述指数的公告页。
  2. 尝试中证指数公司公告接口。该接口目前经常返回 500。
  3. 任一来源失败都保留编制方案日历和已核对的调入调出，不把页面刷空。
调入调出只来自已核对的官方附件，或公告正文里能直接解析出的代码+简称。
PDF 附件不在每日任务里解析。
"""

import datetime
import json
import os
import re
import sys
from html import unescape
from urllib.parse import urljoin

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from pipeline.net import FetchError, fetch  # noqa: E402

OUT_PATH = os.path.join(BASE, "data", "meta", "rebalance.json")
SSE_LIST_URL = "https://www.sse.com.cn/market/sseindex/diclosure/"
SSE_ORIGIN = "https://www.sse.com.cn"
CSI_NOTICE_URL = "https://www.csindex.com.cn/csindex-home/announcement/selectNoticeRe"
CSI_NOTICE_BODY = {"page": 1, "rows": 20, "related_topics": [], "typelist": []}

PAST_DAYS = 160
FUTURE_DAYS = 200
SCHEMA = "rebalance.v1"

METH = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/{code}_Index_Methodology_cn.pdf"

LOW_VOL_NOTE = (
    "低波只跟踪 H30269 中证红利低波动（50 只，每年 12 月调整）。"
    "930955 官方名称是中证红利低波动100，季度调整，与 H30269 不是同一指数；"
    "本站红利低波主 ETF 512890 跟踪 H30269，故不并列收录 930955。"
)

# 指数代码 -> 挂靠的场内 ETF。跟踪关系写在 source_url，不把 ETF 拆成单独调样行。
UNIVERSE = [
    {
        "code": "000922",
        "name": "中证红利",
        "group": "红利",
        "months": [12],
        "cadence": "每年12月",
        "methodology_url": METH.format(code="000922"),
        "etfs": [
            {
                "code": "515180",
                "name": "红利ETF易方达",
                "source_url": "https://www.efunds.com.cn/Html5/fund/515180_org.shtml",
            },
            {
                "code": "515080",
                "name": "中证红利ETF招商",
                "source_url": "https://fund.eastmoney.com/515080.html",
            },
        ],
    },
    {
        "code": "H30269",
        "name": "中证红利低波动",
        "group": "红利",
        "months": [12],
        "cadence": "每年12月",
        "methodology_url": METH.format(code="H30269"),
        "etfs": [
            {
                "code": "512890",
                "name": "红利低波ETF华泰柏瑞",
                "source_url": "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2025-10-28/512890_20251028_IP63.pdf",
            },
            {
                "code": "563020",
                "name": "红利低波ETF易方达",
                "source_url": "https://www.efunds.com.cn/Html5/fund/563020_org.shtml",
            },
        ],
    },
    {
        "code": "000015",
        "name": "上证红利",
        "group": "红利",
        "months": [12],
        "cadence": "每年12月",
        "methodology_url": METH.format(code="000015"),
        "etfs": [
            {
                "code": "510880",
                "name": "红利ETF华泰柏瑞",
                "source_url": "https://fund.eastmoney.com/510880.html",
            },
        ],
    },
    {
        "code": "000016",
        "name": "上证50",
        "group": "宽基",
        "months": [6, 12],
        "cadence": "每年6月、12月",
        "methodology_url": METH.format(code="000016"),
        "etfs": [
            {
                "code": "510050",
                "name": "上证50ETF华夏",
                "source_url": "https://fund.eastmoney.com/510050.html",
            },
        ],
    },
    {
        "code": "000300",
        "name": "沪深300",
        "group": "宽基",
        "months": [6, 12],
        "cadence": "每年6月、12月",
        "methodology_url": METH.format(code="000300"),
        "etfs": [
            {
                "code": "510300",
                "name": "沪深300ETF华泰柏瑞",
                "source_url": "https://fund.eastmoney.com/510300.html",
            },
        ],
    },
]

INDEX_ORDER = [item["code"] for item in UNIVERSE]
_BY_CODE = {item["code"]: item for item in UNIVERSE}

# 2026-05-29 已公告、2026-06-12 收市后生效的调入调出。
# 上证50：上交所公告 + 附件 PDF。沪深300：中证指数公司同日附件 PDF。
SSE50_NOTICE = "https://www.sse.com.cn/market/sseindex/diclosure/c/c_20260529_10820183.shtml"
SSE50_PDF = "https://www.sse.com.cn/market/sseindex/diclosure/c/10820183/files/eaebcb5e5ad343ce87ce9a55f8ba3cce.pdf"
CSI300_PDF = (
    "https://oss-ch.csindex.com.cn/notice/20260529155822-"
    "%E9%99%84%E4%BB%B6%EF%BC%9A%E9%83%A8%E5%88%86%E6%8C%87%E6%95%B0%E6%A0%B7%E6%9C%AC%E8%B0%83%E6%95%B4%E5%90%8D%E5%8D%95.pdf"
)

ANNOUNCED = {
    "000016-2026-06": {
        "announce_date": "2026-05-29",
        "source_url": SSE50_NOTICE,
        "source_title": "关于上证50、上证180、上证380等指数定期调整结果的公告",
        "list_url": SSE50_PDF,
        "adds": [
            ["600089", "特变电工"],
            ["600183", "生益科技"],
            ["601600", "中国铝业"],
            ["601688", "华泰证券"],
            ["603986", "兆易创新"],
        ],
        "removes": [
            ["600104", "上汽集团"],
            ["600690", "海尔智家"],
            ["601225", "陕西煤业"],
            ["601816", "京沪高铁"],
            ["601985", "中国核电"],
        ],
        "note": "公告写明 2026-06-12 收市后生效，下一交易日 2026-06-15 起按新样本计算。跟踪 ETF 随指数调整。",
    },
    "000300-2026-06": {
        "announce_date": "2026-05-29",
        "source_url": CSI300_PDF,
        "source_title": "附件：部分指数样本调整名单（沪深300）",
        "list_url": CSI300_PDF,
        "adds": [
            ["000657", "中钨高新"],
            ["000988", "华工科技"],
            ["001280", "中国铀业"],
            ["002202", "金风科技"],
            ["002353", "杰瑞股份"],
            ["002532", "天山铝业"],
            ["002558", "巨人网络"],
            ["002602", "世纪华通"],
            ["002837", "英维克"],
            ["300450", "先导智能"],
            ["301165", "锐捷网络"],
            ["301308", "江波龙"],
            ["600118", "中国卫星"],
            ["600221", "海航控股"],
            ["600549", "厦门钨业"],
            ["601727", "上海电气"],
            ["688072", "拓荆科技"],
            ["688183", "生益电子"],
            ["688521", "芯原股份"],
        ],
        "removes": [
            ["000661", "长春高新"],
            ["000786", "北新建材"],
            ["000876", "新希望"],
            ["000983", "山西焦煤"],
            ["002252", "上海莱士"],
            ["002459", "晶澳科技"],
            ["002601", "龙佰集团"],
            ["300347", "泰格医药"],
            ["300759", "康龙化成"],
            ["300782", "卓胜微"],
            ["300979", "华利集团"],
            ["600161", "天坛生物"],
            ["600377", "宁沪高速"],
            ["601236", "红塔证券"],
            ["601298", "青岛港"],
            ["601808", "中海油服"],
            ["603195", "公牛集团"],
            ["688169", "石头科技"],
            ["688187", "时代电气"],
        ],
        "note": "中证指数公司 2026-05-29 附件。公告口径为 2026-06-12 收市后生效，下一交易日 2026-06-15 起按新样本计算。",
    },
}


def today_bj(now=None):
    clock = now or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    return clock.date()


def second_friday(year, month):
    """该月第二个星期五。"""
    first = datetime.date(year, month, 1)
    offset = (4 - first.weekday()) % 7
    return first + datetime.timedelta(days=offset + 7)


def next_trading_day(day):
    """下一交易日。只跳过周六、周日。"""
    nxt = day + datetime.timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += datetime.timedelta(days=1)
    return nxt


def _iso(day):
    return day.isoformat() if isinstance(day, datetime.date) else day


def _parse_iso(text):
    if not text:
        return None
    return datetime.date.fromisoformat(text)


def status_of(announce_date, effective_date, as_of):
    """待公告 / 已公告 / 已生效。生效日当天视为已生效。"""
    eff = _parse_iso(effective_date)
    if announce_date and eff and eff <= as_of:
        return "已生效"
    if announce_date and eff and eff > as_of:
        return "已公告"
    return "待公告"


def _names(pairs):
    return [{"code": code, "name": name} for code, name in pairs or []]


def scheduled_slots(index, as_of, past_days=PAST_DAYS, future_days=FUTURE_DAYS):
    """窗口内的定期审视。(month, second_friday, effective_date)。"""
    start = as_of - datetime.timedelta(days=past_days)
    end = as_of + datetime.timedelta(days=future_days)
    slots = []
    for year in range(start.year, end.year + 1):
        for month in index["months"]:
            friday = second_friday(year, month)
            effective = next_trading_day(friday)
            if start <= effective <= end:
                slots.append((month, friday, effective))
    return slots


def _base_event(index, month, friday, effective, as_of):
    event_id = "%s-%04d-%02d" % (index["code"], effective.year, month)
    seeded = ANNOUNCED.get(event_id) or {}
    announce = seeded.get("announce_date")
    note = seeded.get("note")
    if not note:
        note = "编制方案：%s调整，实施日为该月第二个星期五的下一交易日。名单公布前为待公告。" % index["cadence"]
    return {
        "id": event_id,
        "index_code": index["code"],
        "index_name": index["name"],
        "group": index["group"],
        "cadence": index["cadence"],
        "etfs": [dict(item) for item in index["etfs"]],
        "event_type": "定期审视",
        "announce_date": announce,
        "effective_date": _iso(effective),
        "notice_effective": _iso(friday),
        "adds": _names(seeded.get("adds")),
        "removes": _names(seeded.get("removes")),
        "source_url": seeded.get("source_url") or index["methodology_url"],
        "source_title": seeded.get("source_title") or ("%s指数编制方案" % index["name"]),
        "list_url": seeded.get("list_url") or "",
        "status": status_of(announce, _iso(effective), as_of),
        "note": note,
    }


def matched_indexes(title):
    """从公告标题识别跟踪指数。红利低波100 / 930955 明确排除。"""
    text = title or ""
    for frag in ("中证红利低波动100", "红利低波动100", "红利低波100", "930955"):
        text = text.replace(frag, "")
    found = []

    def take(code, fragments):
        nonlocal text
        if any(frag in text for frag in fragments):
            found.append(code)
            for frag in fragments:
                text = text.replace(frag, "")

    take("H30269", ["H30269", "中证红利低波动", "红利低波"])
    take("000015", ["000015", "上证红利"])
    take("000922", ["000922", "中证红利"])
    take("000016", ["000016", "上证50"])
    take("000300", ["000300", "沪深300"])
    return found


_LINK_RE = re.compile(r'href="([^"]+)"[^>]*title="([^"]+)"', re.I)
_EFF_RE = re.compile(r"于(20\d{2})年(\d{1,2})月(\d{1,2})日收市后生效")
_DATE_RE = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日")
_PAIR_RE = re.compile(r"([036]\d{5})\s*([^\s\d,，、。；;：:]{2,8})")


def parse_sse_list(html):
    """上交所指数信息披露列表。返回 [{title, href}]。"""
    seen = set()
    out = []
    for href, title in _LINK_RE.findall(html or ""):
        title = unescape(re.sub(r"\s+", " ", title)).strip()
        if not title or title in seen:
            continue
        seen.add(title)
        out.append({"title": title, "href": href})
    return out


def _ymd(parts):
    year, month, day = parts
    return "%04d-%02d-%02d" % (int(year), int(month), int(day))


def _plain(html):
    text = re.sub(r"<script[\s\S]*?</script>", " ", html or "", flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return unescape(re.sub(r"\s+", " ", text))


def parse_change_lists(text):
    """从纯文本里取「调出…调入…」之后的代码+简称。解析不到时两边都空。"""
    if not text or "调出" not in text or "调入" not in text:
        return [], []
    cut = text
    for marker in ("调出名单", "调出"):
        pos = cut.find(marker)
        if pos >= 0:
            cut = cut[pos:]
            break
    # 附件后的备选名单不要混进来。
    for marker in ("备选名单", "上证180", "上证 180", "中证500", "中证 500"):
        pos = cut.find(marker)
        if pos > 0:
            cut = cut[:pos]
            break
    out_pos = cut.find("调出")
    in_pos = cut.find("调入")
    if out_pos < 0 or in_pos < 0 or in_pos < out_pos:
        return [], []
    removes = _unique_pairs(_PAIR_RE.findall(cut[out_pos:in_pos]))
    adds = _unique_pairs(_PAIR_RE.findall(cut[in_pos:]))
    # 两列被压成一行时，代码会全部落到一侧。两侧都有名单才采用，避免盖掉已核对种子。
    if not removes or not adds:
        return [], []
    return adds, removes


def _unique_pairs(pairs):
    seen = set()
    out = []
    for code, name in pairs:
        name = name.strip()
        if code in seen or len(name) < 2:
            continue
        seen.add(code)
        out.append({"code": code, "name": name})
    return out


def parse_sse_notice(html):
    """公告页：标题、落款日、收市后生效日、PDF 链接。正文若含调入调出表则一并解析。"""
    title_m = re.search(r"<title>([^<]+)", html or "", re.I)
    title = ""
    if title_m:
        title = unescape(title_m.group(1)).split("|")[0].strip()
    text = _plain(html)
    dates = [_ymd(parts) for parts in _DATE_RE.findall(text)]
    eff = _EFF_RE.search(text)
    effective_close = _ymd(eff.groups()) if eff else None
    announce = None
    if dates:
        bounded = [day for day in dates if not effective_close or day <= effective_close]
        announce = (bounded or dates)[-1]
    adds, removes = parse_change_lists(text)
    pdfs = []
    for href in re.findall(r'href="([^"]+\.pdf)"', html or "", flags=re.I):
        pdfs.append(href)
    event_type = "临时调整" if "临时调整" in (title or text) else "定期审视"
    return {
        "title": title,
        "announce_date": announce,
        "effective_close": effective_close,
        "pdfs": pdfs,
        "event_type": event_type,
        "adds": adds,
        "removes": removes,
    }


def parse_csi_payload(payload):
    """中证公告接口。success 不为真时抛 FetchError，由上层保留种子。"""
    if not isinstance(payload, dict) or not payload.get("success"):
        msg = ""
        if isinstance(payload, dict):
            msg = payload.get("msg") or ""
        raise FetchError("中证公告接口失败: %s" % (msg or "success=false"))
    rows = payload.get("data")
    if isinstance(rows, dict):
        rows = rows.get("list") or rows.get("rows") or rows.get("records") or []
    if not isinstance(rows, list):
        return []
    notices = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = row.get("title") or row.get("noticeTitle") or ""
        if not title or not matched_indexes(title):
            continue
        url = row.get("url") or row.get("link") or ""
        announce = row.get("announce_date") or row.get("publishDate") or row.get("publish_date")
        if isinstance(announce, str) and len(announce) >= 10:
            announce = announce[:10]
        else:
            announce = None
        notices.append({
            "title": title,
            "url": url,
            "announce_date": announce,
            "effective_close": row.get("effective_close"),
            "pdfs": row.get("pdfs") or [],
            "event_type": "临时调整" if "临时调整" in title else "定期审视",
            "adds": row.get("adds") or [],
            "removes": row.get("removes") or [],
        })
    return notices


def _abs_pdf(page_url, href):
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return urljoin(page_url, href)


def _find_event(events, code, close_day, effective_day):
    for event in events:
        if event["index_code"] != code:
            continue
        if close_day and event.get("notice_effective") == close_day:
            return event
        if effective_day and event.get("effective_date") == effective_day:
            return event
    return None


def _normalize_names(rows):
    out = []
    for row in rows or []:
        if isinstance(row, dict) and row.get("code") and row.get("name"):
            out.append({"code": str(row["code"]), "name": str(row["name"])})
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            out.append({"code": str(row[0]), "name": str(row[1])})
    return out


def apply_notices(events, notices):
    """用公告更新日期和来源。没有解析出名单时，不覆盖已有调入调出。"""
    extra = []
    for notice in notices or []:
        title = notice.get("title") or ""
        codes = matched_indexes(title)
        close_day = notice.get("effective_close")
        effective_day = None
        if close_day:
            try:
                effective_day = _iso(next_trading_day(_parse_iso(close_day)))
            except ValueError:
                effective_day = None
        page_url = notice.get("url") or ""
        parsed_adds = _normalize_names(notice.get("adds"))
        parsed_removes = _normalize_names(notice.get("removes"))
        list_url = ""
        pdfs = notice.get("pdfs") or []
        if pdfs:
            list_url = _abs_pdf(page_url or SSE_ORIGIN + "/", pdfs[0])
        for code in codes:
            hit = _find_event(events, code, close_day, effective_day)
            if hit:
                if notice.get("announce_date"):
                    hit["announce_date"] = notice["announce_date"]
                if page_url:
                    hit["source_url"] = page_url
                if title:
                    hit["source_title"] = title
                if list_url:
                    hit["list_url"] = list_url
                if parsed_adds:
                    hit["adds"] = parsed_adds
                if parsed_removes:
                    hit["removes"] = parsed_removes
                if notice.get("event_type") == "临时调整":
                    hit["event_type"] = "临时调整"
                continue
            if not effective_day and notice.get("event_type") != "临时调整":
                continue
            index = _BY_CODE.get(code)
            if not index:
                continue
            extra.append({
                "id": "%s-%s" % (code, effective_day or (notice.get("announce_date") or "adhoc")),
                "index_code": code,
                "index_name": index["name"],
                "group": index["group"],
                "cadence": index["cadence"],
                "etfs": [dict(item) for item in index["etfs"]],
                "event_type": notice.get("event_type") or "临时调整",
                "announce_date": notice.get("announce_date"),
                "effective_date": effective_day or "",
                "notice_effective": close_day or "",
                "adds": parsed_adds,
                "removes": parsed_removes,
                "source_url": page_url or index["methodology_url"],
                "source_title": title,
                "list_url": list_url,
                "status": "待公告",
                "note": "公告未落入定期窗口，按标题收录。调入调出以公告附件为准。",
            })
    events.extend(extra)
    return events


def _sort_events(events, as_of):
    for event in events:
        event["status"] = status_of(
            event.get("announce_date"), event.get("effective_date"), as_of)

    def key(event):
        future = 0 if event["status"] != "已生效" else 1
        eff = event.get("effective_date") or "9999-99-99"
        if future:
            eff = "".join(chr(255 - ord(ch)) for ch in eff)
        order = INDEX_ORDER.index(event["index_code"]) if event["index_code"] in INDEX_ORDER else 99
        return (future, eff, order, event["index_code"])

    events.sort(key=key)
    return events


def build_events(as_of, notices=None):
    events = []
    for index in UNIVERSE:
        for month, friday, effective in scheduled_slots(index, as_of):
            events.append(_base_event(index, month, friday, effective, as_of))
    apply_notices(events, notices)
    return _sort_events(events, as_of)


def build_payload(as_of, notices=None, warnings=None, checked_at=None):
    warnings = list(warnings or [])
    events = build_events(as_of, notices)
    return {
        "schema": SCHEMA,
        "as_of": _iso(as_of),
        "updated_at": checked_at or datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "low_vol_code": "H30269",
        "low_vol_note": LOW_VOL_NOTE,
        "calendar_note": (
            "000922、H30269、000015 按编制方案每年 12 月调整一次，不是半年审。"
            "000016、000300 每年 6 月和 12 月审核，实施日为第二个星期五的下一交易日。"
            "ETF 跟随对应指数，不单列调样。"
            "510880 红利ETF华泰柏瑞跟踪上证红利，不跟踪中证红利。"
        ),
        "refresh": {
            # 上交所列表是目前能稳定打开的公告源。中证接口 500 只记警告，不把 live 打成失败。
            "live": not any(item.startswith("上交所指数公告：") for item in warnings),
            "warnings": warnings,
            "notice_count": len(notices or []),
            "checked_at": checked_at,
        },
        "universe": [
            {
                "code": item["code"],
                "name": item["name"],
                "group": item["group"],
                "cadence": item["cadence"],
                "methodology_url": item["methodology_url"],
                "etfs": item["etfs"],
            }
            for item in UNIVERSE
        ],
        "events": events,
    }


def _default_get(url):
    return fetch(url, referer=SSE_ORIGIN + "/", timeout=25, retries=2)


def _default_post(url, body):
    import urllib.request
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": "https://www.csindex.com.cn/",
            "Origin": "https://www.csindex.com.cn",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def gather_notices(get_text=None, post_json=None):
    """拉取公告。单个来源失败只记入 warnings。"""
    get_text = get_text or _default_get
    post_json = post_json or _default_post
    warnings = []
    notices = []
    try:
        html = get_text(SSE_LIST_URL)
        for item in parse_sse_list(html):
            if "调整" not in item["title"] or not matched_indexes(item["title"]):
                continue
            detail_url = urljoin(SSE_ORIGIN, item["href"])
            try:
                parsed = parse_sse_notice(get_text(detail_url))
            except Exception as exc:  # noqa: BLE001
                warnings.append("上交所公告页 %s：%s" % (detail_url, exc))
                continue
            parsed["url"] = detail_url
            parsed["title"] = parsed.get("title") or item["title"]
            notices.append(parsed)
    except Exception as exc:  # noqa: BLE001
        warnings.append("上交所指数公告：" + str(exc))
    try:
        payload = post_json(CSI_NOTICE_URL, CSI_NOTICE_BODY)
        notices.extend(parse_csi_payload(payload))
    except Exception as exc:  # noqa: BLE001
        warnings.append("中证指数公告：" + str(exc))
    return notices, warnings


def write_payload(payload, path=OUT_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def refresh(path=OUT_PATH, as_of=None, get_text=None, post_json=None):
    as_of = as_of or today_bj()
    notices, warnings = gather_notices(get_text=get_text, post_json=post_json)
    checked = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    payload = build_payload(as_of, notices, warnings, checked_at=checked)
    write_payload(payload, path)
    return payload


def main():
    payload = refresh()
    refresh_meta = payload["refresh"]
    print("调仓提醒 %s 条，数据日 %s，live=%s" % (
        len(payload["events"]), payload["as_of"], refresh_meta["live"]))
    for warning in refresh_meta["warnings"]:
        print("WARN", warning)
    for event in payload["events"]:
        print("%s %s %s 公告=%s 生效=%s 调入%d 调出%d" % (
            event["status"], event["index_code"], event["index_name"],
            event["announce_date"] or "-", event["effective_date"] or "-",
            len(event["adds"]), len(event["removes"])))


if __name__ == "__main__":
    main()

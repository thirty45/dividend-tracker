# -*- coding: utf-8 -*-
"""调仓提醒：日历、公告解析、抓取失败时保留种子。"""
import datetime
import unittest

from pipeline.net import FetchError
from pipeline.rebalance import (
    ANNOUNCED,
    apply_notices,
    build_events,
    build_payload,
    gather_notices,
    matched_indexes,
    next_trading_day,
    parse_csi_payload,
    parse_change_lists,
    parse_sse_list,
    parse_sse_notice,
    second_friday,
    status_of,
)


SSE_LIST = """
<dd>
  <a href="/market/sseindex/diclosure/c/c_20260909_10831727.shtml"
     title="关于上证180、上证380指数样本临时调整的公告">关于上证180、上证380指数样本临时调整的公告</a>
</dd>
<dd>
  <a href="/market/sseindex/diclosure/c/c_20260529_10820183.shtml"
     title="关于上证50、上证180、上证380等指数定期调整结果的公告">关于上证50、上证180、上证380等指数定期调整结果的公告</a>
</dd>
"""

SSE_NOTICE = """
<html><head><title>关于上证50、上证180、上证380等指数定期调整结果的公告 | 上海证券交易所</title></head>
<body>
根据指数规则，上海证券交易所与中证指数有限公司决定调整上证50、上证180、上证380、科创50等指数样本，于2026年6月12日收市后生效。
上述指数样本调整名单见附件。
<a href="10820183/files/sample.pdf">附件</a>
上海证券交易所 中证指数有限公司 2026年5月29日
页脚 2027年1月1日
</body></html>
"""

CHANGE_TEXT = """
上证50指数样本调整名单
调出
600104 上汽集团
600690 海尔智家
调入
600089 特变电工
600183 生益科技
上证180指数样本调整名单
600027 华电国际
"""


class CalendarTests(unittest.TestCase):
    def test_second_friday_and_next_session(self):
        self.assertEqual(second_friday(2026, 6), datetime.date(2026, 6, 12))
        self.assertEqual(next_trading_day(datetime.date(2026, 6, 12)), datetime.date(2026, 6, 15))
        self.assertEqual(second_friday(2026, 12), datetime.date(2026, 12, 11))
        self.assertEqual(next_trading_day(datetime.date(2026, 12, 11)), datetime.date(2026, 12, 14))

    def test_status(self):
        as_of = datetime.date(2026, 10, 18)
        self.assertEqual(status_of(None, "2026-12-14", as_of), "待公告")
        self.assertEqual(status_of("2026-11-28", "2026-12-14", as_of), "已公告")
        self.assertEqual(status_of("2026-05-29", "2026-06-15", as_of), "已生效")
        self.assertEqual(status_of("2026-12-14", "2026-12-14", datetime.date(2026, 12, 14)), "已生效")

    def test_window_uses_official_cadence(self):
        events = build_events(datetime.date(2026, 10, 18))
        ids = [event["id"] for event in events]
        self.assertIn("000922-2026-12", ids)
        self.assertIn("H30269-2026-12", ids)
        self.assertIn("000015-2026-12", ids)
        self.assertIn("000016-2026-06", ids)
        self.assertIn("000016-2026-12", ids)
        self.assertIn("000300-2026-06", ids)
        self.assertNotIn("000922-2026-06", ids)
        self.assertNotIn("H30269-2026-06", ids)
        self.assertNotIn("000015-2026-06", ids)
        self.assertFalse(any(event["index_code"] == "930955" for event in events))
        future = [event for event in events if event["status"] != "已生效"]
        past = [event for event in events if event["status"] == "已生效"]
        self.assertTrue(future)
        self.assertEqual([event["status"] for event in future], ["待公告"] * len(future))
        self.assertEqual(events[:len(future)], future)
        self.assertEqual({event["id"] for event in past}, {"000016-2026-06", "000300-2026-06"})

    def test_seed_lists_match_official_attachment_counts(self):
        sse = ANNOUNCED["000016-2026-06"]
        csi = ANNOUNCED["000300-2026-06"]
        self.assertEqual(len(sse["adds"]), 5)
        self.assertEqual(len(sse["removes"]), 5)
        self.assertEqual(len(csi["adds"]), 19)
        self.assertEqual(len(csi["removes"]), 19)
        june = {event["id"]: event for event in build_events(datetime.date(2026, 10, 18))}
        self.assertEqual(june["000016-2026-06"]["adds"][0], {"code": "600089", "name": "特变电工"})
        self.assertEqual(june["000300-2026-06"]["removes"][0], {"code": "000661", "name": "长春高新"})
        self.assertEqual(june["000922-2026-12"]["adds"], [])
        self.assertIn("512890", [etf["code"] for etf in june["H30269-2026-12"]["etfs"]])
        self.assertEqual(june["000015-2026-12"]["etfs"][0]["code"], "510880")


class ParseTests(unittest.TestCase):
    def test_match_indexes_without_low_vol_100(self):
        self.assertEqual(
            matched_indexes("关于上证50、上证180、上证380等指数定期调整结果的公告"),
            ["000016"],
        )
        self.assertEqual(matched_indexes("关于上证180、上证380指数样本临时调整的公告"), [])
        self.assertEqual(matched_indexes("关于中证红利指数样本定期调整的公告"), ["000922"])
        self.assertEqual(matched_indexes("关于中证红利低波动指数样本调整的公告"), ["H30269"])
        self.assertEqual(matched_indexes("关于中证红利低波动100指数样本调整的公告"), [])
        self.assertEqual(matched_indexes("930955 红利低波100 临时调整"), [])
        self.assertEqual(matched_indexes("沪深300指数样本定期调整"), ["000300"])

    def test_parse_sse_list_and_notice(self):
        rows = parse_sse_list(SSE_LIST)
        self.assertEqual(len(rows), 2)
        notice = parse_sse_notice(SSE_NOTICE)
        self.assertEqual(notice["announce_date"], "2026-05-29")
        self.assertEqual(notice["effective_close"], "2026-06-12")
        self.assertEqual(notice["event_type"], "定期审视")
        self.assertEqual(notice["pdfs"], ["10820183/files/sample.pdf"])
        self.assertTrue(notice["title"].startswith("关于上证50"))

    def test_parse_change_lists_stops_before_next_index(self):
        adds, removes = parse_change_lists(CHANGE_TEXT)
        self.assertEqual(removes, [
            {"code": "600104", "name": "上汽集团"},
            {"code": "600690", "name": "海尔智家"},
        ])
        self.assertEqual(adds, [
            {"code": "600089", "name": "特变电工"},
            {"code": "600183", "name": "生益科技"},
        ])

    def test_notice_does_not_wipe_seeded_constituents(self):
        events = build_events(datetime.date(2026, 10, 18))
        notice = parse_sse_notice(SSE_NOTICE)
        notice["url"] = "https://www.sse.com.cn/market/sseindex/diclosure/c/c_20260529_10820183.shtml"
        notice["title"] = "关于上证50、上证180、上证380等指数定期调整结果的公告"
        apply_notices(events, [notice])
        hit = next(event for event in events if event["id"] == "000016-2026-06")
        self.assertEqual(len(hit["adds"]), 5)
        self.assertEqual(hit["announce_date"], "2026-05-29")
        self.assertTrue(hit["list_url"].endswith("sample.pdf"))

    def test_csi_failure_is_explicit(self):
        with self.assertRaises(FetchError):
            parse_csi_payload({"code": "500", "msg": "服务器异常", "success": False})

    def test_csi_rows_map_onto_tracked_indexes(self):
        notices = parse_csi_payload({
            "success": True,
            "data": [
                {"title": "关于沪深300指数样本临时调整的公告", "publishDate": "2026-09-01", "url": "https://www.csindex.com.cn/n/1"},
                {"title": "关于发布新指数的公告", "publishDate": "2026-09-02"},
            ],
        })
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["event_type"], "临时调整")
        self.assertEqual(matched_indexes(notices[0]["title"]), ["000300"])


class RefreshFailureTests(unittest.TestCase):
    def test_both_sources_down_keeps_calendar_and_seed(self):
        def boom(url, body=None):
            raise FetchError("down " + url)

        notices, warnings = gather_notices(get_text=boom, post_json=boom)
        self.assertEqual(notices, [])
        self.assertEqual(len(warnings), 2)
        payload = build_payload(datetime.date(2026, 10, 18), notices, warnings)
        self.assertFalse(payload["refresh"]["live"])
        self.assertEqual(payload["low_vol_code"], "H30269")
        ids = [event["id"] for event in payload["events"]]
        self.assertIn("000300-2026-06", ids)
        seeded = next(event for event in payload["events"] if event["id"] == "000016-2026-06")
        self.assertEqual(len(seeded["removes"]), 5)
        self.assertTrue(any(event["status"] == "待公告" for event in payload["events"]))


if __name__ == "__main__":
    unittest.main()

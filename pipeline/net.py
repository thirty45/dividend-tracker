# -*- coding: utf-8 -*-
"""轻量 HTTP 封装，仅用标准库，带重试与编码处理。"""

import gzip
import json
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class FetchError(RuntimeError):
    pass


def fetch(url, params=None, headers=None, referer=None, timeout=20,
          retries=3, delay=1.0, encoding=None):
    """GET 请求，返回文本。失败自动重试。"""
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(params)
    last_err = None
    for attempt in range(retries):
        try:
            hdrs = {
                "User-Agent": UA,
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            if referer:
                hdrs["Referer"] = referer
            if headers:
                hdrs.update(headers)
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
            if encoding is not None:
                return raw.decode(encoding, errors="replace")
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("gbk", errors="replace")
        except HTTPError as he:
            last_err = he
            if he.code == 514:
                # 频率限制：多等一会儿再试
                time.sleep(delay * (attempt + 1) + 12)
            else:
                time.sleep(delay * (attempt + 1) + 0.3)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(delay * (attempt + 1) + 0.3)
    raise FetchError("请求失败(%d次): %s -> %r" % (retries, url, last_err))


def fetch_json(url, params=None, headers=None, referer=None, timeout=20,
               retries=3, delay=1.0):
    text = fetch(url, params=params, headers=headers, referer=referer,
                 timeout=timeout, retries=retries, delay=delay)
    return json.loads(text)

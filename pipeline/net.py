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
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def fetch(url, params=None, headers=None, referer=None, timeout=20,
          retries=3, delay=1.0, encoding=None):
    """GET 请求，返回文本。失败自动重试。"""
    if retries < 1:
        raise ValueError("retries must be at least 1")
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
            if he.code in (400, 404, 501) or attempt + 1 == retries:
                break
            if he.code == 514:
                # 频率限制：多等一会儿再试
                time.sleep(delay * (attempt + 1) + 12)
            else:
                time.sleep(delay * (attempt + 1) + 0.3)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt + 1 < retries:
                time.sleep(delay * (attempt + 1) + 0.3)
    code = last_err.code if isinstance(last_err, HTTPError) else None
    raise FetchError("请求失败(%d次): %s -> %r" % (attempt + 1, url, last_err), code=code)


def fetch_json(url, params=None, headers=None, referer=None, timeout=20,
               retries=3, delay=1.0):
    text = fetch(url, params=params, headers=headers, referer=referer,
                 timeout=timeout, retries=retries, delay=delay)
    return json.loads(text)


class SourceGuard:
    """单个数据源的熔断 + 限速：连续整只失败 max_fail 次后暂停请求该源。

    只统计“整只抓取失败”（如腾讯所有窗口都 501/5xx），不统计早期窗口无数据的 501，
    避免把正常的上市前空窗误判为封禁。min_interval 是跨线程的最小请求间隔（秒）。
    熔断后每 cooldown 秒放行一只试探：成功即恢复，失败继续熔断。
    这样源只是短时 501/限流时，后面的股票还能回到该源，不会整轮都跳过。
    """

    def __init__(self, name, max_fail=8, min_interval=0.0, cooldown=60.0):
        import threading
        self.name = name
        self.max_fail = max_fail
        self.min_interval = min_interval
        self.cooldown = cooldown
        self._reopen_at = 0.0
        self.trips = 0
        self._lock = threading.Lock()
        self._streak = 0
        self._next = 0.0
        self.tripped = False
        self.ok_count = 0
        self.fail_count = 0

    def blocked(self):
        with self._lock:
            if not self.tripped:
                return False
            now = time.monotonic()
            if self.cooldown and now >= self._reopen_at:
                self._reopen_at = now + self.cooldown  # 本周期只放行这一只试探
                return False
            return True

    def ok(self):
        with self._lock:
            self._streak = 0
            self.ok_count += 1
            recovered = self.tripped
            self.tripped = False
        if recovered:
            print("   %s 试探成功，恢复使用" % self.name, flush=True)

    def fail(self, exc=None):
        with self._lock:
            self._streak += 1
            self.fail_count += 1
            if self.tripped or self._streak < self.max_fail:
                return
            self.tripped = True
            self.trips += 1
            self._reopen_at = time.monotonic() + self.cooldown
        print("::warning title=%s 熔断::连续 %d 只失败，暂停请求该源 %ds 后试探，期间改用备用源（最后错误：%s）"
              % (self.name, self.max_fail, self.cooldown, str(exc)[-160:]), flush=True)

    def await_turn(self, max_wait=None):
        """熔断时阻塞等待下一个试探窗口，避免整批股票瞬间全跳过。

        返回 True 表示当前线程拿到试探/正常名额（随后应再 wait 限速）；
        False 表示在 max_wait 内仍熔断。
        """
        if max_wait is None:
            max_wait = max(float(self.cooldown), 1.0) + 2.0
        deadline = time.monotonic() + max_wait
        while True:
            if not self.blocked():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            with self._lock:
                wake_at = self._reopen_at if self.tripped else time.monotonic()
            sleep_for = min(2.0, max(0.05, wake_at - time.monotonic()), remaining)
            time.sleep(sleep_for)

    def wait(self):
        """限速：多线程共享同一节奏，避免狂刷备用源。"""
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.min_interval
        if slot > now:
            time.sleep(slot - now)

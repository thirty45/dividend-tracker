"""Shared validation and cache metadata for independently adjusted K-lines."""
import datetime
import json
import math
from pathlib import Path


def valid_bars(bars, min_date=None, tolerance=0.010001):
    if not isinstance(bars, list) or not bars:
        return False
    previous = ""
    try:
        for bar in bars:
            date = bar["d"]
            if not isinstance(date, str) or len(date) != 10 or date <= previous:
                return False
            if datetime.date.fromisoformat(date).isoformat() != date:
                return False
            if any(isinstance(bar[k], bool) or not isinstance(bar[k], (int, float))
                   for k in ("o", "c", "h", "l", "v")):
                return False
            o, c, h, l, v = (bar[k] for k in ("o", "c", "h", "l", "v"))
            if not all(math.isfinite(x) for x in (o, c, h, l, v)):
                return False
            # Independently rounded adjusted OHLC can differ by one price tick.
            if h < max(o, c, l) - tolerance or l > min(o, c, h) + tolerance or v < 0:
                return False
            previous = date
        return min_date is None or previous >= min_date
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def read_series(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) and valid_bars(payload.get("bars")) else None
    except (OSError, ValueError, TypeError):
        return None


def history_complete(payload):
    """Historical completeness is independent of the next trading day's freshness."""
    if not isinstance(payload, dict) or payload.get("history_complete") is False:
        return False
    bars = payload.get("bars")
    return bool(valid_bars(bars) and payload.get("coverage_complete")
                and payload.get("coverage_start") == bars[0]["d"]
                and payload.get("coverage_end") == bars[-1]["d"])


def coverage_fields(bars, target_date):
    return {
        "coverage_start": bars[0]["d"] if bars else "",
        "coverage_end": bars[-1]["d"] if bars else "",
        "coverage_complete": valid_bars(bars, target_date),
    }


def merge_bars(old, new, keep=0):
    by_date = {b["d"]: b for b in old or []}
    by_date.update((b["d"], b) for b in new or [])
    bars = [by_date[d] for d in sorted(by_date)]
    return bars[-keep:] if keep else bars


def align_adjusted_history(old, new):
    """Align old adjusted prices only when overlap supports a single factor."""
    prior = {b["d"]: b for b in old}
    ratios = sorted(b["c"] / prior[b["d"]]["c"] for b in new
                    if b["d"] in prior and prior[b["d"]]["c"] != 0)
    if not ratios:
        raise ValueError("前复权增量缺少重叠价格，需重新核验完整历史")
    factor = ratios[len(ratios) // 2]
    if factor <= 0 or any(abs(r / factor - 1) > 0.01 for r in ratios):
        raise ValueError("前复权重叠比例不一致，需重新核验完整历史")
    if abs(factor - 1) <= 0.0001:
        return old
    return [dict(b, **{k: b[k] * factor for k in ("o", "c", "h", "l")}) for b in old]

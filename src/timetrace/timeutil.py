"""时间工具：内部一律 epoch 秒（float），只在边界转 ISO 串与本地时间。

为什么用 `time.mktime` 而不是 tzinfo 算术：在 Windows 上
`datetime.now().astimezone().tzinfo` 拿到的是"此刻的固定偏移"，跨夏令时会差一小时；
`time.mktime` 走操作系统真正的时区规则，并且直接给出我们要的 epoch 秒。
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta

from timetrace.domain import Range
from timetrace.errors import UsageError

RANGE_HELP = "可用取值：today | yesterday | week | month | all | YYYY-MM-DD | YYYY-MM-DD..YYYY-MM-DD"


def now_epoch() -> float:
    return time.time()


def epoch_to_iso(epoch: float) -> str:
    stamp = datetime.fromtimestamp(epoch, tz=UTC)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def iso_to_epoch(text: str) -> float:
    raw = str(text).strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    return datetime.fromisoformat(raw).timestamp()


def local_date(epoch: float) -> date:
    return datetime.fromtimestamp(epoch).date()


def local_day_start(day: date) -> float:
    return time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1))


def local_day_end(day: date) -> float:
    return local_day_start(day + timedelta(days=1))


def format_local(epoch: float, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return time.strftime(fmt, time.localtime(epoch))


def format_clock(epoch: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


def format_duration(seconds: float) -> str:
    """紧凑格式：45s / 12m / 3h12m。"""
    total = round(max(0.0, seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m" if secs < 30 else f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def format_percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _hour_start(epoch: float) -> float:
    parts = time.localtime(epoch)
    return time.mktime((parts.tm_year, parts.tm_mon, parts.tm_mday, parts.tm_hour, 0, 0, 0, 0, -1))


def split_by_local_hour(start: float, end: float) -> list[tuple[int, float]]:
    """把 [start, end) 按本地小时切成 (小时, 秒数) —— 报告里的每小时直方图用。"""
    if end <= start:
        return []
    buckets: dict[int, float] = {}
    cursor = start
    guard = 0
    while cursor < end and guard < 100_000:
        guard += 1
        boundary = min(end, _hour_start(cursor) + 3600.0)
        hour = time.localtime(cursor).tm_hour
        buckets[hour] = buckets.get(hour, 0.0) + max(0.0, boundary - cursor)
        cursor = boundary
    return sorted(buckets.items())


def split_by_local_day(start: float, end: float) -> list[tuple[date, float]]:
    """把 [start, end) 按本地自然日切成 (日期, 秒数) —— 报告里的每日明细用。"""
    if end <= start:
        return []
    buckets: dict[date, float] = {}
    cursor = start
    guard = 0
    while cursor < end and guard < 100_000:
        guard += 1
        day = local_date(cursor)
        boundary = min(end, local_day_end(day))
        buckets[day] = buckets.get(day, 0.0) + max(0.0, boundary - cursor)
        cursor = boundary
    return sorted(buckets.items())


def parse_range(spec: str | None, *, now: float | None = None) -> Range:
    """把 `today|yesterday|week|month|all|YYYY-MM-DD|A..B` 解析成 Range。

    `week` 从本周一开始，`month` 从本月 1 号开始；`end` 一律取"现在"，
    因为按 24:00 算会让早上的覆盖率看起来像 10%。
    """
    moment = now_epoch() if now is None else now
    raw = (spec or "today").strip()
    key = raw.lower()
    today = local_date(moment)

    if key == "today":
        return Range(local_day_start(today), moment, "今天")
    if key == "yesterday":
        day = today - timedelta(days=1)
        return Range(local_day_start(day), local_day_end(day), f"昨天 {day.isoformat()}")
    if key == "week":
        monday = today - timedelta(days=today.weekday())
        return Range(local_day_start(monday), moment, "本周")
    if key == "month":
        return Range(local_day_start(today.replace(day=1)), moment, "本月")
    if key == "all":
        return Range(None, None, "全部")

    if ".." in raw:
        left, _, right = raw.partition("..")
        start_day = _parse_day(left.strip(), raw) if left.strip() else None
        end_day = _parse_day(right.strip(), raw) if right.strip() else None
        start = local_day_start(start_day) if start_day else None
        end = local_day_end(end_day) if end_day else moment
        if start is not None and end is not None and end < start:
            raise UsageError(f"区间结束早于开始：{raw}", hint=RANGE_HELP)
        label = (
            f"{start_day.isoformat() if start_day else '最早'} → {end_day.isoformat() if end_day else '现在'}"
        )
        return Range(start, end, label)

    day = _parse_day(raw, raw)
    return Range(local_day_start(day), local_day_end(day), day.isoformat())


def _parse_day(text: str, original: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as cause:
        raise UsageError(f"看不懂的区间：{original}", hint=RANGE_HELP) from cause


def describe_range(rng: Range) -> str:
    left = format_local(rng.start, "%Y-%m-%d %H:%M") if rng.start is not None else "最早"
    right = format_local(rng.end, "%Y-%m-%d %H:%M") if rng.end is not None else "现在"
    return f"{rng.label}（{left} → {right}）"

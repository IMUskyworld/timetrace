"""测试用的样本流构造器：让 segment/ report 的边界测试一眼能读。"""

from __future__ import annotations

import time

from timetrace.domain import Sample

DEFAULT_DAY = "2026-09-23"


def at(clock: str, day: str = DEFAULT_DAY) -> float:
    """本地时间字符串 → epoch 秒。`at("10:00")`、`at("10:00:15")` 都行。"""
    parts = [int(piece) for piece in clock.split(":")]
    while len(parts) < 3:
        parts.append(0)
    year, month, mday = (int(piece) for piece in day.split("-"))
    return time.mktime((year, month, mday, parts[0], parts[1], parts[2], 0, 0, -1))


def sample(
    ts: float,
    exe: str,
    *,
    idle: float = 0.0,
    title: str = "",
    pid: int = 1000,
    kind: str = "active",
    note: str = "",
) -> Sample:
    return Sample(
        ts=ts,
        pid=pid,
        exe=exe,
        title=title,
        idle_s=idle,
        kind=kind,
        note=note,
    )


def stream(
    entries: list[tuple[str, str] | tuple[str, str, float]], *, day: str = DEFAULT_DAY
) -> list[Sample]:
    """`[("10:00", "code.exe"), ("10:00:05", "code.exe", 7)]` → Sample 列表。第三条目是 idle 秒数。"""
    out: list[Sample] = []
    for entry in entries:
        clock, exe = entry[0], entry[1]
        idle = float(entry[2]) if len(entry) > 2 else 0.0
        out.append(sample(at(clock, day), exe, idle=idle))
    return out


def clock_entries(*pairs: tuple[str, str]) -> list[tuple[str, str]]:
    return list(pairs)

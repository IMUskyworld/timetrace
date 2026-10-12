#!/usr/bin/env python3
"""timetrace 开销对照（DEVELOPMENT.md §15）。

对着软目标量五件事：
  1. 单次采样开销（真实 Windows API，报 P50/P95）；
  2. 一天 17280 行的写入速度；
  3. 30 天报告（约 51.8 万条采样）的生成时间；
  4. 一天的磁盘占用；
  5. 常驻内存（本进程 RSS，量级参考）。

    python scripts/bench_overhead.py [--quick] [--json]

`--quick` 跳过 30 天那一段（质量闸门用），默认全跑。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from timetrace.config import Config  # noqa: E402
from timetrace.domain import Sample  # noqa: E402
from timetrace.html_template import render_html  # noqa: E402
from timetrace.paths import Paths  # noqa: E402
from timetrace.report import build_report, render_console, render_json  # noqa: E402
from timetrace.segment import build_segments  # noqa: E402
from timetrace.store import Store  # noqa: E402
from timetrace.timeutil import parse_range  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover
        pass

CONFIG = Config()
DAY_SECONDS = 86400
APP = ("code.exe", "msedge.exe", "explorer.exe", "qoder.exe", "chrome.exe")


SCHEDULE: tuple[tuple[str | None, int], ...] = (
    # (应用, 持续分钟)。None = 离开（idle_s 涨过阈值）
    ("code.exe", 47),
    ("msedge.exe", 12),
    ("code.exe", 63),
    (None, 25),
    ("msedge.exe", 18),
    ("code.exe", 55),
    ("qoder.exe", 41),
    (None, 12),
    ("chrome.exe", 35),
    ("code.exe", 52),
)


def synthetic_day(
    day_start: float,
    *,
    hours: int = 24,
    interval: float = 5.0,
    sleeps: bool = True,
    skip_lunch: bool = True,
) -> list[Sample]:
    """造一天真实形状的采样。

    形状来自真实使用：几段几十分钟的连续应用（会形成专注块）、中间穿插短切换、
    一到两段离开（idle_s 涨过阈值），以及一段 40 分钟没有任何采样行的休眠。

    真实的"离开"有两种，报告要分别处理，所以这里都造出来：
    * **空闲**：采样照常，只是 idle_s 涨过阈值 → 「（空闲）」；
    * **休眠**：整段没有采样行 → 「（未记录）」。
    """
    samples: list[Sample] = []
    deadline = day_start + hours * 3600
    moment = day_start
    index = 0
    lunch_at = day_start + hours * 3600 * 0.45
    lunch_done = not skip_lunch or hours < 3

    while moment < deadline:
        if not lunch_done and moment >= lunch_at:
            moment += 40 * 60  # 午休：休眠 40 分钟，一条采样都不写
            lunch_done = True
            continue
        app, minutes = SCHEDULE[index % len(SCHEDULE)]
        block_end = min(moment + minutes * 60, deadline)
        while moment < block_end:
            idle = 320.0 + (moment - day_start) % 600 if app is None else 1.0 + (moment - day_start) % 9
            samples.append(
                Sample(
                    ts=moment,
                    pid=1000 + index % 5,
                    exe=app or "code.exe",
                    title=f"{app} 的窗口" if app else "（离开）",
                    idle_s=idle,
                )
            )
            moment += interval
        index += 1
    _ = sleeps
    return samples


def synthetic_days(
    count: int, *, end: float, hours: int = 24, interval: float = 5.0, skip_lunch: bool = False
) -> list[Sample]:
    """多天样本，**按时间升序**返回。

    段切分假设输入有序（store 的 ORDER BY ts 保证），倒序输入会被当成"时间倒退"丢弃
    —— 那是给 NTP 校时准备的防御，不是给造数据的。
    """
    out: list[Sample] = []
    for offset in reversed(range(count)):
        out.extend(
            synthetic_day(
                end - (offset + 1) * DAY_SECONDS, hours=hours, interval=interval, skip_lunch=skip_lunch
            )
        )
    return out


def measure_tracker_rss(home: Path, *, seconds: float = 6.0) -> float:
    """量**tracker 进程自己**的内存，而不是量 bench 脚本自己。"""
    if sys.platform != "win32":
        return 0.0
    import ctypes
    import os
    import subprocess
    from ctypes import wintypes

    command = [sys.executable, "-m", "timetrace", "track", "--quiet", "--home", str(home)]
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONPATH"] = str(ROOT / "src")
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment)
    peak = 0.0
    try:
        deadline = time.time() + seconds
        while time.time() < deadline and process.poll() is None:
            peak = max(peak, _rss_of(process.pid, ctypes, wintypes))
            time.sleep(0.4)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
    return peak


def _rss_of(pid: int, ctypes_module, wintypes_module) -> float:
    class PROCESS_MEMORY_COUNTERS(ctypes_module.Structure):
        _fields_: ClassVar[list[tuple[str, object]]] = [  # pyright: ignore[reportGeneralTypeIssues]
            ("cb", wintypes_module.DWORD),
            ("PageFaultCount", wintypes_module.DWORD),
            ("PeakWorkingSetSize", ctypes_module.c_size_t),
            ("WorkingSetSize", ctypes_module.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes_module.c_size_t),
            ("QuotaPagedPoolUsage", ctypes_module.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes_module.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes_module.c_size_t),
            ("PagefileUsage", ctypes_module.c_size_t),
            ("PeakPagefileUsage", ctypes_module.c_size_t),
        ]

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes_module.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes_module.WinDLL("psapi", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return 0.0
    try:
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes_module.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes_module.byref(counters), counters.cb):
            return 0.0
        return round(counters.WorkingSetSize / 1024 / 1024, 1)
    finally:
        kernel32.CloseHandle(handle)


def measure_sampling(rounds: int = 200) -> dict[str, float]:
    """真实 Windows API 的采样开销（拿不到窗口时返回空 dict，由调用方标 SKIP）。"""
    try:
        from timetrace import winapi
    except Exception:  # pragma: no cover
        return {}
    supported, _ = winapi.platform_supported()
    if not supported:
        return {}
    winapi.foreground_window()  # 预热
    winapi.idle_seconds()
    durations: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter()
        winapi.foreground_window()
        winapi.idle_seconds()
        durations.append((time.perf_counter() - started) * 1000)
    ordered = sorted(durations)
    return {
        "samples": float(rounds),
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[int(len(ordered) * 0.95) - 1], 3),
        "max_ms": round(max(ordered), 3),
        "mean_ms": round(statistics.fmean(ordered), 3),
    }


def measure_day_write(samples: list[Sample], home: Path) -> dict[str, float]:
    paths = Paths(home).ensure()
    store = Store(paths.db).initialize()
    try:
        started = time.perf_counter()
        written = store.insert_samples(samples)
        elapsed = time.perf_counter() - started
        store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        size = paths.db.stat().st_size
        counted = store.count_samples()
    finally:
        store.close()
    return {
        "rows": float(written),
        "counted": float(counted),
        "seconds": round(elapsed, 3),
        "per_row_ms": round(elapsed / written * 1000, 4),
        "disk_kb": round(size / 1024, 1),
    }


def synthetic_worst_case(start: float, *, interval: float = 5.0, days: int = 30) -> list[Sample]:
    """造"段数 == 采样数"的最坏情况：exe 与标题每条都在变，`_merge` 一条都合不掉。

    常规 `synthetic_day` 是长块（一个应用连续几十分钟），段会被大量合并，`build_report`
    实际只处理几千段 —— 只测它会漏掉"51.8 万个段"这条真实路径（浏览器标签页标题、
    编辑器切文件都会造成这种形状）。§15 的 < 2s 目标要按这条来卡。
    """
    total = int(days * DAY_SECONDS / interval)
    return [
        Sample(
            ts=start + index * interval,
            pid=1000 + index % 50,
            exe=APP[index % len(APP)],
            title=f"窗口 {index % 9973}",
            idle_s=0.0,
        )
        for index in range(total)
    ]


def measure_report(samples: list[Sample], *, label: str) -> dict[str, float]:
    started = time.perf_counter()
    segments = build_segments(samples, config=CONFIG)
    segmented = time.perf_counter()
    data = build_report(
        segments,
        rng=parse_range("all", now=samples[-1].ts + 60),
        config=CONFIG,
        now=samples[-1].ts + 60,
    )
    built = time.perf_counter()
    console = render_console(data)
    json_text = render_json(data)
    html = render_html(data)
    rendered = time.perf_counter()
    return {
        f"{label}_segment_s": round(segmented - started, 3),
        f"{label}_build_s": round(built - segmented, 3),
        f"{label}_render_s": round(rendered - built, 3),
        f"{label}_total_s": round(rendered - started, 3),
        f"{label}_rows": float(len(samples)),
        f"{label}_console_bytes": float(len(console)),
        f"{label}_json_bytes": float(len(json_text)),
        f"{label}_html_bytes": float(len(html)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="timetrace 开销对照")
    parser.add_argument("--quick", action="store_true", help="跳过 30 天报告那一段")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args()

    metrics: dict[str, object] = {}
    metrics["sampling"] = measure_sampling()

    with tempfile.TemporaryDirectory(prefix="timetrace-bench-") as tmp:
        home = Path(tmp)
        day_start = time.time() - DAY_SECONDS
        one_day = synthetic_day(day_start)
        metrics["one_day"] = {"samples": len(one_day)}
        metrics["write"] = measure_day_write(one_day, home / "write")
        metrics["report_one_day"] = measure_report(one_day, label="day")

        if not args.quick:
            thirty = synthetic_days(30, end=time.time())
            metrics["thirty_days"] = {"samples": len(thirty)}
            metrics["report_thirty_days"] = measure_report(thirty, label="month")

            # 最坏情况：段数 == 采样数。常规合成数据的段会大量合并，测不到这条路径。
            worst = synthetic_worst_case(time.time() - 30 * DAY_SECONDS)
            metrics["worst_case"] = {"samples": len(worst)}
            metrics["report_worst_case"] = measure_report(worst, label="worst")

        metrics["tracker_rss_mb"] = measure_tracker_rss(home / "rss")

    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        return 0

    print("timetrace 开销对照（DEVELOPMENT.md §15 软目标）")
    sampling = metrics["sampling"]
    if sampling:
        print(
            f"  单次采样      P50 {sampling['p50_ms']}ms · P95 {sampling['p95_ms']}ms · "
            f"max {sampling['max_ms']}ms（目标 P95 < 5ms）"
        )
    else:
        print("  单次采样      SKIP（非 Windows）")
    write = metrics["write"]  # type: ignore[assignment]
    print(
        f"  一天写入      {int(write['rows'])} 行 / {write['seconds']}s "
        f"（{write['per_row_ms']}ms 每行，目标 < 8s）"
    )
    print(f"  一天磁盘      {write['disk_kb']} KB（目标 < 400KB）")
    for key, label, limit in (
        ("report_one_day", "一天报告", None),
        ("report_thirty_days", "30 天报告", 2.0),
        ("report_worst_case", "30天最坏", 2.0),
    ):
        block = metrics.get(key)
        if not block:
            continue
        prefix = {"report_one_day": "day", "report_thirty_days": "month"}.get(key, "worst")
        total = block[f"{prefix}_total_s"]
        rows = int(block[f"{prefix}_rows"])
        suffix = f"（目标 < {limit}s）" if limit else ""
        print(f"  {label:<12}  {rows} 行 / {total}s{suffix}")
    print(f"  常驻内存      {metrics['tracker_rss_mb']} MB（tracker 进程实测，目标 < 40MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

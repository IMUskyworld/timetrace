"""报告层：console / HTML / JSON 三种输出，全部从同一份 ReportData 渲染。

三条纪律：
1. **数字同源**：三种渲染吃同一个对象，测试断言关键数字逐项一致（§20-5）；
2. **自包含**：HTML 零外部资源（无 CDN、无字体、无图片），标题一律转义（§20-6）；
3. **自报可信度**：覆盖率、最大采样间隔、丢弃采样数印在报告头部 —— 账本先说自己有多可信。
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date
from itertools import pairwise

from timetrace.config import Config
from timetrace.domain import (
    BUCKET_LABELS,
    KIND_APP,
    KIND_GAP,
    KIND_IDLE,
    KIND_LOCKED,
    KIND_PAUSED,
    Range,
    Segment,
    Segments,
)
from timetrace.segment import app_runs, app_totals, focus_blocks, switch_count
from timetrace.timeutil import (
    epoch_to_iso,
    format_clock,
    format_duration,
    format_local,
    format_percent,
    local_date,
    split_by_local_day,
    split_by_local_hour,
)

CONSOLE_BAR_WIDTH = 22
SPECIAL_KINDS = (KIND_IDLE, KIND_LOCKED, KIND_PAUSED, KIND_GAP)
# 长区间（month/all）的 console 只列最近这么多天：终端不是表格软件，别刷屏。
DAILY_CONSOLE_MAX_DAYS = 31
DAILY_HTML_TABLE_MAX_DAYS = 92
DAILY_HTML_CHART_MAX_DAYS = 62
WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def daily_label(row: DailyRow) -> str:
    """`10-06周一`：月-日 + 星期，比 ISO 短，比只有月-日 多一点上下文。"""
    return f"{row.day.strftime('%m-%d')}{WEEKDAY_LABELS[row.day.weekday()]}"


# ------------------------------------------------------------------ 数据结构


@dataclass(slots=True)
class AppRow:
    exe: str
    seconds: float
    share: float
    excluded: bool = False
    label: str = ""

    def display(self) -> str:
        return self.label or self.exe or "（无窗口）"


@dataclass(slots=True)
class BlockRow:
    exe: str
    start: float
    end: float
    seconds: float

    def window(self) -> str:
        return f"{format_clock(self.start)}–{format_clock(self.end)}"


@dataclass(slots=True)
class HourRow:
    hour: int
    seconds: float
    by_app: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class GoalRow:
    # 一个目标的进度。只报数，不打分 —— 超了也没奖励，差了也不批评。
    exe: str
    target_seconds: float
    actual_seconds: float

    @property
    def progress(self) -> float:
        return (self.actual_seconds / self.target_seconds) if self.target_seconds else 0.0

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.target_seconds - self.actual_seconds)

    @property
    def over_seconds(self) -> float:
        return max(0.0, self.actual_seconds - self.target_seconds)


@dataclass(slots=True)
class DailyRow:
    """一天的账。`span` 是日历上的应有长度，`covered` 是真的被采样覆盖的部分。"""

    day: date
    span: float
    covered: float = 0.0
    totals: dict[str, float] = field(default_factory=dict)
    by_app: dict[str, float] = field(default_factory=dict)
    switches: int = 0

    @property
    def coverage(self) -> float:
        """按 §10 只算被采样的部分；「未记录」是 gap，不算覆盖。"""
        return ((self.covered - self.totals.get(KIND_GAP, 0.0)) / self.span) if self.span else 0.0

    @property
    def app_seconds(self) -> float:
        return self.totals.get(KIND_APP, 0.0)

    @property
    def kind_seconds(self) -> dict[str, float]:
        """所有出现过的桶（应用 + 专用桶），报告里逐日列出，不漏任何一秒。"""
        out = {KIND_APP: self.app_seconds}
        for kind in SPECIAL_KINDS:
            out[kind] = self.totals.get(kind, 0.0)
        return out

    @property
    def top_app(self) -> tuple[str, float] | None:
        if not self.by_app:
            return None
        name, seconds = max(self.by_app.items(), key=lambda item: (item[1], item[0]))
        return (name, seconds) if seconds > 0 else None


@dataclass(slots=True)
class ReportData:
    generated_at: float
    range_label: str
    range_start: float
    range_end: float
    range_seconds: float
    totals: dict[str, float]
    coverage: float
    covered_seconds: float
    apps: list[AppRow]
    focus: list[BlockRow]
    hourly: list[HourRow]
    switches: int
    quality: dict[str, float]
    notes: list[str] = field(default_factory=list)
    interval: float = 5.0
    focus_min: float = 10.0
    # 放最后并给默认值：这两个是后加的字段，别让它们破坏既有的位置构造调用。
    daily: list[DailyRow] = field(default_factory=list)
    goals: list[GoalRow] = field(default_factory=list)

    @property
    def top_app(self) -> AppRow | None:
        ranked = [row for row in self.apps if not row.excluded]
        return ranked[0] if ranked else None

    def to_dict(self) -> dict[str, object]:
        """JSON 是公开接口，所以字段名显式写出来，别用 asdict 顺带把内部名字漏出去。"""
        return {
            "generated_at": epoch_to_iso(self.generated_at),
            "range": {
                "label": self.range_label,
                "start": epoch_to_iso(self.range_start),
                "end": epoch_to_iso(self.range_end),
                "seconds": round(self.range_seconds, 3),
            },
            "coverage": round(self.coverage, 6),
            "covered_seconds": round(self.covered_seconds, 3),
            "totals": {kind: round(seconds, 3) for kind, seconds in self.totals.items()},
            "apps": [
                {
                    "exe": row.exe,
                    "label": row.label,
                    "display": row.display(),
                    "seconds": round(row.seconds, 3),
                    "share": round(row.share, 6),
                    "excluded": row.excluded,
                }
                for row in self.apps
            ],
            "focus_blocks": [
                {
                    "exe": block.exe,
                    "start": epoch_to_iso(block.start),
                    "end": epoch_to_iso(block.end),
                    "seconds": round(block.seconds, 3),
                }
                for block in self.focus
            ],
            "hourly": [
                {
                    "hour": row.hour,
                    "seconds": round(row.seconds, 3),
                    "by_app": {name: round(seconds, 3) for name, seconds in row.by_app.items()},
                }
                for row in self.hourly
            ],
            "daily": [
                {
                    "day": row.day.isoformat(),
                    "span": round(row.span, 3),
                    "covered": round(row.covered, 3),
                    "coverage": round(row.coverage, 6),
                    "totals": {kind: round(seconds, 3) for kind, seconds in row.kind_seconds.items()},
                    "apps": [
                        {"exe": exe, "seconds": round(seconds, 3)}
                        for exe, seconds in sorted(row.by_app.items(), key=lambda item: (-item[1], item[0]))
                    ],
                    "switches": row.switches,
                }
                for row in self.daily
            ],
            "goals": [
                {
                    "exe": row.exe,
                    "target_seconds": round(row.target_seconds, 3),
                    "actual_seconds": round(row.actual_seconds, 3),
                    "progress": round(row.progress, 6),
                    "remaining_seconds": round(row.remaining_seconds, 3),
                    "over_seconds": round(row.over_seconds, 3),
                }
                for row in self.goals
            ],
            "switches": self.switches,
            "quality": {key: round(value, 3) for key, value in self.quality.items()},
            "notes": list(self.notes),
            "interval": self.interval,
            "focus_min": self.focus_min,
        }


# ------------------------------------------------------------------ 构造


def clip_segments(segments: list[Segment], start: float, end: float) -> list[Segment]:
    """把段裁到报告区间；完全在区间外的直接丢掉。"""
    out = []
    for segment in segments:
        left, right = max(segment.start, start), min(segment.end, end)
        if right <= left:
            continue
        out.append(replace(segment, start=left, end=right))
    return out


def fill_uncovered_as_gap(segments: list[Segment], start: float, end: float) -> list[Segment]:
    """把区间内"没有任何段"的时间补成 gap —— 账本守恒的最后一块拼图。

    §10 写得很清楚：早于首条采样、晚于末条采样的时间计入 `未记录`。少了这一步，
    `report --range month` 只跑了三天时，剩下 27 天会凭空消失 —— 所有桶加起来不到
    区间总长，"每一项加起来必须等于区间总长"这句承诺就成了空话。
    """
    if end <= start:
        return segments
    ordered = sorted(segments, key=lambda item: item.start)
    filled: list[Segment] = []
    cursor = start
    for segment in ordered:
        left, right = max(segment.start, start), min(segment.end, end)
        if right <= left:
            continue
        if left > cursor + 1e-9:
            filled.append(Segment(start=cursor, end=left, kind=KIND_GAP))
        filled.append(replace(segment, start=left, end=right))
        cursor = max(cursor, right)
    if cursor < end - 1e-9:
        filled.append(Segment(start=cursor, end=end, kind=KIND_GAP))
    return filled


def resolve_bounds(rng: Range, segments: Segments, *, now: float) -> tuple[float, float]:
    """开区间（`all` 或 `..B`）的边界只能靠数据解决：起点取第一条采样。"""
    if rng.start is not None:
        start = rng.start
    elif segments.first_ts is not None:
        start = segments.first_ts
    else:
        start = now
    if rng.end is not None:
        end = rng.end
    elif segments.last_ts is not None:
        end = segments.last_ts + 1.0
    else:
        end = now
    if end <= start:
        end = start
    return start, end


def build_report(
    segments: Segments,
    *,
    rng: Range,
    config: Config,
    now: float,
    generated_at: float | None = None,
) -> ReportData:
    start, end = resolve_bounds(rng, segments, now=now)
    clipped = clip_segments(segments.segments, start, end)
    # 先补洞再统计：区间里没被任何段覆盖的时间必须落进「未记录」，否则账本不守恒。
    accounted = fill_uncovered_as_gap(clipped, start, end)
    span = max(0.0, end - start)

    totals: dict[str, float] = {KIND_APP: 0.0}
    for kind in SPECIAL_KINDS:
        totals[kind] = 0.0
    for segment in accounted:
        totals[segment.kind] = totals.get(segment.kind, 0.0) + segment.duration
    # 覆盖率按 §10：只算真的被采样到的部分（应用 + 空闲 + 锁屏 + 暂停）。
    # 「未记录」是 gap，它恰恰证明那段**没有**被覆盖，把它算进分子会让休眠一整天的
    # 报告显示 100% 覆盖 —— 那正是这个工具最不该犯的谎。
    covered = sum(seconds for kind, seconds in totals.items() if kind != KIND_GAP)

    per_app = app_totals(accounted)
    excluded_names: set[str] = set()
    for segment in accounted:
        if segment.kind == KIND_APP and segment.excluded:
            excluded_names.add(segment.exe)
    # 占比一律以"区间总长"为分母：应用行与专用桶（空闲/未记录/锁屏/暂停）口径一致，
    # 所有行加起来正好等于 100%（账本守恒），而覆盖率只数其中"真的记到了"的那部分。
    apps = [
        AppRow(
            exe=exe,
            seconds=seconds,
            share=(seconds / span) if span else 0.0,
            excluded=exe in excluded_names,
        )
        for exe, seconds in per_app.items()
    ]
    apps.sort(key=lambda row: (-row.seconds, row.exe))
    for kind, label in BUCKET_LABELS.items():
        seconds = totals.get(kind, 0.0)
        if seconds <= 0:
            continue
        share = (seconds / span) if span else 0.0
        apps.append(AppRow(exe=kind, seconds=seconds, share=share, label=label, excluded=True))

    blocks = [
        BlockRow(exe=block.exe, start=block.start, end=block.end, seconds=block.duration)
        for block in focus_blocks(accounted, focus_min=config.focus_min)
    ]
    blocks.sort(key=lambda row: -row.seconds)

    hourly = _hourly(accounted)
    goals = _goals(per_app, config)

    quality = {
        "sample_count": float(segments.sample_count),
        "max_interval": segments.max_interval,
        "median_interval": segments.median_interval,
        "discarded": float(segments.discarded),
        "errors": float(segments.error_count),
    }
    notes = _notes(span, covered, quality, config)

    return ReportData(
        generated_at=generated_at if generated_at is not None else now,
        range_label=rng.label,
        range_start=start,
        range_end=end,
        range_seconds=span,
        totals=totals,
        coverage=(covered / span) if span else 0.0,
        covered_seconds=covered,
        apps=apps,
        focus=blocks,
        hourly=hourly,
        daily=_daily(accounted, start, end),
        goals=goals,
        switches=switch_count(accounted),
        quality=quality,
        notes=notes,
        interval=config.interval,
        focus_min=config.focus_min,
    )


def _hourly(segments: list[Segment]) -> list[HourRow]:
    buckets: dict[int, dict[str, float]] = {hour: {} for hour in range(24)}
    for segment in segments:
        if segment.kind != KIND_APP:
            continue
        for hour, seconds in split_by_local_hour(segment.start, segment.end):
            bucket = buckets.setdefault(hour, {})
            bucket[segment.exe] = bucket.get(segment.exe, 0.0) + seconds
    return [
        HourRow(hour=hour, seconds=sum(buckets[hour].values()), by_app=buckets[hour]) for hour in range(24)
    ]


def _goals(per_app: dict[str, float], config: Config) -> list[GoalRow]:
    # 把配置里的目标对着这一区间的实际应用时长比一比。
    # 只有配了 goals 才有这一节 —— 没配就不出现，绝不替用户发明目标。
    rows = [
        GoalRow(exe=exe, target_seconds=hours * 3600.0, actual_seconds=per_app.get(exe, 0.0))
        for exe, hours in config.goals
    ]
    # 排序：进度高的在前，同为 0 时按进程名 —— 同一份数据每次顺序一致。
    rows.sort(key=lambda row: (-row.progress, row.exe))
    return rows


def _daily(segments: list[Segment], start: float, end: float) -> list[DailyRow]:
    """逐日分账：每个桶、每个应用、每次切换都落到它真正发生的那一天。

    跨午夜的段按本地自然日切开（`split_by_local_day`），所以"23:50 挂到 00:10"会一半记在
    昨天、一半记在今天 —— 与每小时直方图同一套口径。
    """
    rows: dict[date, DailyRow] = {
        day: DailyRow(day=day, span=span) for day, span in split_by_local_day(start, end)
    }
    for segment in segments:
        # 跨午夜的段必须真的切开：23:50→00:05 的 15 分钟里，前 10 分钟属于昨天。
        for day, seconds in split_by_local_day(segment.start, segment.end):
            row = rows.get(day)
            if row is None:
                # 段落在区间内但日历桶没建出来（理论上只有 0 长度区间），补上而不是丢账。
                row = rows.setdefault(day, DailyRow(day=day, span=0.0))
            row.covered += seconds
            row.totals[segment.kind] = row.totals.get(segment.kind, 0.0) + seconds
            if segment.kind == KIND_APP and segment.exe:
                row.by_app[segment.exe] = row.by_app.get(segment.exe, 0.0) + seconds
    # 切换次数与 `switch_count` 同一口径：按"应用运行段"（app_runs）算，
    # 中间隔着断档也算一次切换 —— 否则逐日之和会和报告头部的总数对不上。
    runs = app_runs(segments)
    for previous, current in pairwise(runs):
        if previous.exe == current.exe:
            continue
        row = rows.get(local_date(current.start))
        if row is not None:
            row.switches += 1
    return [rows[day] for day in sorted(rows)]


def _notes(span: float, covered: float, quality: dict[str, float], config: Config) -> list[str]:
    notes: list[str] = []
    if span <= 0:
        notes.append("这个区间是空的（没有可报告的时间）")
        return notes
    if covered <= 0:
        notes.append("区间内没有任何采样：timetrace 可能没在跑（试 `timetrace track` 或检查 autostart）")
        return notes
    coverage = covered / span
    if coverage < 0.5:
        notes.append(
            f"覆盖率只有 {format_percent(coverage)}：区间内大部分时间没有记录，"
            "先确认 tracker 一直在跑，再相信下面的数字"
        )
    if quality["max_interval"] > config.gap_limit:
        over = format_duration(config.gap_limit)
        notes.append(
            f"最大采样间隔 {format_duration(quality['max_interval'])}（超过 {over} 算断档）："
            "这段被记成「未记录」，没有摊给任何应用"
        )
    if quality["errors"] > 0:
        notes.append(f"有 {int(quality['errors'])} 条采样出错（拿不到前台窗口），已单独计数")
    if quality["discarded"] > 0:
        notes.append(f"丢弃了 {int(quality['discarded'])} 条时间倒退的异常采样（NTP 校时？）")
    return notes


# ------------------------------------------------------------------ console


def display_width(text: str) -> int:
    """终端里的显示宽度：CJK 与全角符号占两列，其它占一列。"""
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def pad(text: str, width: int, *, align: str = "left") -> str:
    filling = max(0, width - display_width(text))
    if align == "right":
        return " " * filling + text
    return text + " " * filling


def render_console(data: ReportData) -> str:
    lines: list[str] = []
    header = (
        f"timetrace · {data.range_label}"
        f"  覆盖率 {format_percent(data.coverage)}"
        f"  最大采样间隔 {format_duration(data.quality['max_interval'])}"
        f"  切换 {data.switches} 次"
    )
    lines.append(header)
    lines.append(f"区间 {format_local(data.range_start)} → {format_local(data.range_end)}")
    lines.append("")

    width = max([display_width(row.display()) for row in data.apps] + [18])
    header_cells = f"{pad('时长', 9, align='right')}  {pad('占区间', 8, align='right')}"
    lines.append(f"  {pad('应用', width)}  {header_cells}  最长专注块")
    longest: dict[str, BlockRow] = {}
    for block in data.focus:
        longest.setdefault(block.exe, block)
    for row in data.apps:
        block = longest.get(row.exe)
        block_text = f"{format_duration(block.seconds)}（{block.window()}）" if block else "—"
        lines.append(
            f"  {pad(row.display(), width)}  {pad(format_duration(row.seconds), 9, align='right')}"
            f"  {pad(format_percent(row.share), 8, align='right')}  {block_text}"
        )

    if data.goals:
        lines.append("")
        lines.append(f"  目标对比（区间：{data.range_label}）")
        for row in data.goals:
            if row.over_seconds > 0:
                state = f"超出 {format_duration(row.over_seconds)}"
            elif row.remaining_seconds > 0:
                state = f"还差 {format_duration(row.remaining_seconds)}"
            else:
                state = "刚好达成"
            lines.append(
                f"  {pad(row.exe, 18)}  目标 {pad(format_duration(row.target_seconds), 9, align='right')}"
                f"  实际 {pad(format_duration(row.actual_seconds), 9, align='right')}"
                f"  进度 {pad(format_percent(row.progress), 7, align='right')}  {state}"
            )
        lines.append("  （只报进度，不打分：工具不知道这些时间用得好不好）")

    if len(data.daily) > 1:
        lines.append("")
        hidden = len(data.daily) - DAILY_CONSOLE_MAX_DAYS
        suffix = f"（只列最近 {DAILY_CONSOLE_MAX_DAYS} 天，共 {len(data.daily)} 天）" if hidden > 0 else ""
        lines.append(f"  每日明细{suffix}")
        lines.append(
            f"  {pad('日期', 9)}  {pad('应用', 8, align='right')}  {pad('空闲', 8, align='right')}"
            f"  {pad('未记录', 8, align='right')}  {pad('锁屏', 7, align='right')}"
            f"  {pad('暂停', 7, align='right')}  {pad('覆盖率', 8, align='right')}  头号应用"
        )
        for row in data.daily[-DAILY_CONSOLE_MAX_DAYS:]:
            top = row.top_app
            top_text = f"{top[0]} {format_duration(top[1])}" if top else "—"
            kinds = row.kind_seconds
            lines.append(
                f"  {pad(daily_label(row), 9)}"
                f"  {pad(format_duration(row.app_seconds), 8, align='right')}"
                f"  {pad(format_duration(kinds[KIND_IDLE]), 8, align='right')}"
                f"  {pad(format_duration(kinds[KIND_GAP]), 8, align='right')}"
                f"  {pad(format_duration(kinds[KIND_LOCKED]), 7, align='right')}"
                f"  {pad(format_duration(kinds[KIND_PAUSED]), 7, align='right')}"
                f"  {pad(format_percent(row.coverage), 8, align='right')}  {top_text}"
            )

    lines.append("")
    lines.append("  每小时在用什么")
    peak = max((row.seconds for row in data.hourly), default=0.0)
    for row in data.hourly:
        if row.seconds <= 0:
            continue
        filled = round(row.seconds / peak * CONSOLE_BAR_WIDTH) if peak else 0
        names = sorted(row.by_app.items(), key=lambda item: -item[1])[:3]
        summary = " + ".join(name for name, _ in names if name)
        lines.append(f"  {row.hour:02d} {'█' * max(1, filled)} {summary}")

    if data.notes:
        lines.append("")
        for note in data.notes:
            lines.append(f"  ! {note}")

    lines.append("")
    lines.append(
        f"  采样 {int(data.quality['sample_count'])} 条"
        f"（中位间隔 {format_duration(data.quality['median_interval'])}"
        f"、异常 {int(data.quality['errors'])}"
        f"、丢弃 {int(data.quality['discarded'])}）"
    )
    return "\n".join(lines)


# ------------------------------------------------------------------ JSON


def render_json(data: ReportData) -> str:
    return json.dumps(data.to_dict(), ensure_ascii=False, indent=2)

"""把采样流切成时间轴：app / idle / paused / locked / gap。

**纯函数**：不 import sqlite3 / time / ctypes。输入是 Sample 序列与暂停区间，
输出是一条**完整覆盖**时间轴的段列表（相邻段无缝隙、无重叠）。

实现就是 DEVELOPMENT.md §9.4 的四步，优先级 `gap > paused > locked > idle > app`：

1. 原始归属：每条采样管它之后到下一个采样点的那段；
2. 空闲回填：`idle_s >= 阈值` 的采样，把它"真正的空闲起点"（`ts - idle_s`）到下一个采样
   之间整段划成 idle —— 这一步把"挂着窗口去吃饭"从应用时间里剔掉；
3. 暂停区间：`pauses` 表给出的区间一并在 app/idle 上挖掉；
4. 断档：两次采样间隔超过 `gap_limit` 的整段标为 gap（睡眠、关机、进程被杀）。

守恒律（§10）：所有段加起来必须等于 `[首条采样, 末条采样 + interval)`，
这条由 test_segment 用属性测试钉住。输入**不保证有序**时本函数先自己排序 ——
见 build_segments 里的注释：乱序输入不能让时间凭空消失。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from itertools import pairwise

from timetrace.config import Config
from timetrace.domain import (
    KIND_APP,
    KIND_GAP,
    KIND_IDLE,
    KIND_LOCKED,
    KIND_PAUSED,
    LOCKED_EXES,
    LOCKED_SYSTEM_EXES,
    SAMPLE_ACTIVE,
    SAMPLE_PAUSED,
    Pause,
    Sample,
    Segment,
    Segments,
)

# 谁压过谁：数值大的赢（DEVELOPMENT.md §9.4 的优先级）
PRECEDENCE = {
    KIND_APP: 0,
    KIND_IDLE: 1,
    KIND_LOCKED: 2,
    KIND_PAUSED: 3,
    KIND_GAP: 4,
}


def is_locked(sample: Sample) -> bool:
    """锁屏/登录界面占据前台时，不代表有人在用机器。"""
    exe = sample.exe.lower()
    if exe in LOCKED_EXES:
        return True
    return exe in LOCKED_SYSTEM_EXES and not sample.title.strip()


def build_segments(
    samples: Sequence[Sample],
    *,
    config: Config,
    pauses: Iterable[Pause] = (),
    own_pids: Iterable[int] = (),
    excluded: Iterable[str] = (),
) -> Segments:
    excluded_names = {name.strip().lower() for name in excluded if name}
    own = {int(pid) for pid in own_pids}
    result = Segments()
    if not samples:
        return result

    # 契约防御：内部算法要求样本按 ts 升序（"下一条采样"决定当前的覆盖终点），
    # 但不把正确性押在调用方自觉上 —— 乱序输入会让下一条把当前采样的终点拽到
    # 过去，当前采样被误判"时间倒退"而丢弃，**时间凭空少一段**，守恒律当场破。
    # 那正是这个工具存在意义要防的事，所以在边界上自己排：
    # 已排序的输入 Timsort 是 O(n)，真实路径（store ORDER BY ts）零代价。
    ordered = sorted(samples, key=lambda item: item.ts)

    result.sample_count = len(ordered)
    result.first_ts = ordered[0].ts
    result.last_ts = ordered[-1].ts
    result.error_count = sum(1 for item in ordered if item.kind not in (SAMPLE_ACTIVE, SAMPLE_PAUSED))
    intervals = _intervals(ordered)
    result.max_interval = max(intervals) if intervals else 0.0
    result.median_interval = _median(intervals)

    segments = _raw_segments(ordered, config=config, own_pids=own, excluded=excluded_names, result=result)
    if not segments:
        return result

    span_start, span_end = segments[0].start, segments[-1].end
    pieces = _collect_pieces(ordered, pauses, config=config, limit=span_end, floor=span_start)
    result.segments = _merge(_apply(_resolve(pieces), segments))
    return result


def _collect_pieces(
    samples: Sequence[Sample],
    pauses: Iterable[Pause],
    *,
    config: Config,
    limit: float,
    floor: float,
) -> list[tuple[float, float, str, int]]:
    """收集所有"要盖在原始归属上"的区间（§9.2 回填 + §9.3 延伸 + 暂停 + 断档）。

    先全收集、再统一解决重叠，而不是每来一条就重扫一遍时间轴 ——
    后者是 O(样本数 × 段数)，真实长跑（30 天 50 万条）会慢到不可用。
    """
    pieces: list[tuple[float, float, str, int]] = []

    idle_priority = PRECEDENCE[KIND_IDLE]
    for index, current in enumerate(samples):
        if not current.is_idle(config.idle_threshold):
            continue
        start = max(current.ts - current.idle_s, floor)
        end = samples[index + 1].ts if index + 1 < len(samples) else limit
        if end > start:
            pieces.append((start, end, KIND_IDLE, idle_priority))

    for pause in pauses:
        start = max(pause.start_ts, floor)
        end = min(pause.end_ts if pause.end_ts is not None else limit, limit)
        if end > start:
            pieces.append((start, end, KIND_PAUSED, PRECEDENCE[KIND_PAUSED]))

    for index in range(1, len(samples)):
        previous, current = samples[index - 1], samples[index]
        if current.ts - previous.ts > config.gap_limit:
            pieces.append((previous.ts, current.ts, KIND_GAP, PRECEDENCE[KIND_GAP]))

    return pieces


def _resolve(pieces: list[tuple[float, float, str, int]]) -> list[tuple[float, float, str]]:
    """扫描线：把重叠的区间切成互不重叠的碎片，每片取优先级最高的那个。

    复杂度 O(m log m)，m = 区间数。
    """
    if not pieces:
        return []
    events: list[tuple[float, int, int, str]] = []
    for start, end, kind, priority in pieces:
        events.append((start, 0, priority, kind))
        events.append((end, 1, priority, kind))
    events.sort(key=lambda item: (item[0], item[1]))

    out: list[tuple[float, float, str]] = []
    active: list[tuple[int, str]] = []
    previous: float | None = None
    for position, is_end, priority, kind in events:
        if previous is not None and position > previous and active:
            best_kind = max(active)[1]
            if out and out[-1][1] == previous and out[-1][2] == best_kind:
                out[-1] = (out[-1][0], position, best_kind)  # 同类相邻，直接延长
            else:
                out.append((previous, position, best_kind))
        if is_end:
            active.remove((priority, kind))
        else:
            active.append((priority, kind))
        previous = position
    return out


def _apply(partition: list[tuple[float, float, str]], segments: list[Segment]) -> list[Segment]:
    """把优先级碎片盖到原始时间轴上：两个有序列表一次归并。

    碎片只在"优先级更高"时才替换原来的 kind，所以 app 段被空闲切成两半、锁屏段被断档吃掉
    这些情况都自然发生；时间轴始终完整覆盖（守恒律由此保住）。
    """
    if not partition:
        return segments
    out: list[Segment] = []
    index = 0
    for segment in segments:
        cursor = segment.start
        # 跳过完全在 segment 左侧的碎片
        while index < len(partition) and partition[index][1] <= cursor:
            index += 1
        probe = index
        while probe < len(partition) and partition[probe][0] < segment.end:
            start, end, kind = partition[probe]
            probe += 1
            if end <= cursor:
                continue
            if start > cursor:
                cut_end = min(start, segment.end)
                if cursor == segment.start and cut_end == segment.end:
                    out.append(segment)
                else:
                    out.append(replace(segment, start=cursor, end=cut_end))
            overlap_start = max(start, cursor)
            overlap_end = min(end, segment.end)
            if PRECEDENCE.get(kind, 0) > PRECEDENCE.get(segment.kind, 0):
                out.append(Segment(kind=kind, start=overlap_start, end=overlap_end))
            elif overlap_start == segment.start and overlap_end == segment.end:
                out.append(segment)
            else:
                out.append(replace(segment, start=overlap_start, end=overlap_end))
            cursor = overlap_end
            if cursor >= segment.end:
                break
        if cursor < segment.end:
            out.append(
                segment if cursor == segment.start else replace(segment, start=cursor, end=segment.end)
            )
    return out


# ------------------------------------------------------------------ 第一步：原始归属


def _raw_segments(
    samples: Sequence[Sample],
    *,
    config: Config,
    own_pids: set[int],
    excluded: set[str],
    result: Segments,
) -> list[Segment]:
    out: list[Segment] = []
    # `cursor` 是已产出时间轴的右端。NTP 把墙钟往回拨时，后一条采样的 ts 会小于它，
    # 直接照抄就得到**互相重叠的段**（sum(时长) > span），守恒律当场破掉 ——
    # 模块开头承诺的"无缝隙、无重叠"必须在这里兑现。
    cursor = None
    for index, current in enumerate(samples):
        following = samples[index + 1] if index + 1 < len(samples) else None
        end = following.ts if following is not None else current.ts + config.interval
        if end <= current.ts:
            # ts 倒退（NTP 校时）：不产生负长度段，只记一笔
            result.discarded += 1
            continue
        start = current.ts
        if cursor is not None and start < cursor - 1e-9:
            # 与上一段重叠：把重叠部分让出去（上一段先到先得），只留下游那段。
            # 被削掉的秒数计入 discarded —— 不猜它属于谁，也不让它被算两遍。
            result.discarded += 1
            start = cursor
            if end <= start:
                continue
        if current.kind == SAMPLE_PAUSED:
            out.append(Segment(kind=KIND_PAUSED, start=start, end=end))
        elif is_locked(current):
            out.append(Segment(kind=KIND_LOCKED, start=start, end=end))
        elif current.is_idle(config.idle_threshold):
            out.append(Segment(kind=KIND_IDLE, start=start, end=end))
        else:
            out.append(
                Segment(
                    kind=KIND_APP,
                    start=start,
                    end=end,
                    exe=current.exe,
                    title=current.title,
                    estimated=following is None,
                    excluded=(current.pid in own_pids) or current.exe in excluded,
                )
            )
        cursor = end
    return out


# ------------------------------------------------------------------ 第二/三步：区间覆盖


def _merge(segments: list[Segment]) -> list[Segment]:
    """合并相邻的同 kind 同应用段，让段数不至于爆炸。

    `app` 段按 (exe, title) 合并 —— 标题变化太频繁，按 exe 合并会把不同文档混成一坨；
    要按应用汇总的场合由 `app_totals()` / `app_runs()` 负责。
    """
    merged: list[Segment] = []
    for segment in segments:
        if segment.end <= segment.start:
            continue
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and previous.kind == segment.kind
            and previous.exe == segment.exe
            and previous.title == segment.title
            and previous.excluded == segment.excluded
            and abs(previous.end - segment.start) < 1e-9
        ):
            previous.end = segment.end
            previous.estimated = previous.estimated and segment.estimated
            continue
        merged.append(segment)
    return merged


# ------------------------------------------------------------------ 派生统计（供 report 用）


def app_totals(segments: Iterable[Segment]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for segment in segments:
        if segment.kind != KIND_APP:
            continue
        totals[segment.exe] = totals.get(segment.exe, 0.0) + segment.duration
    return totals


def app_runs(segments: Iterable[Segment]) -> list[Segment]:
    """把相邻的 app 段按 exe 串起来（跨标题、跨单条采样），专注块与切换次数都用它。

    30 天报告里这是最热的线性扫描（51.8 万段），所以只在"一段就是一整串"时才
    `replace()` 重建对象 —— 频繁切换的最坏情况下能省掉几十万次对象构造。
    **长度为 1 的串直接复用输入段**，因此调用方不要就地修改返回值。
    """
    runs: list[Segment] = []
    first: Segment | None = None
    end = 0.0
    title = ""
    count = 0

    def flush() -> None:
        nonlocal first, end, title, count
        if first is None or count == 0:
            return
        if count == 1:
            runs.append(first)
        else:
            run = replace(first, end=end, title=title)
            runs.append(run)
        first = None
        title = ""
        count = 0

    for segment in segments:
        if segment.kind != KIND_APP:
            flush()
            continue
        if first is not None and first.exe == segment.exe and abs(end - segment.start) < 1e-9:
            end = segment.end
            title = title or segment.title
            count += 1
            continue
        flush()
        first, end, title, count = segment, segment.end, segment.title, 1
    flush()
    return runs


def focus_blocks(
    segments: Iterable[Segment], *, focus_min: float, runs: list[Segment] | None = None
) -> list[Segment]:
    """同一应用连续不切换、且长度达到阈值的那些段。"""
    threshold = focus_min * 60.0
    # `runs` 由调用方传进来时就不再重算：30 天报告里这条线性扫描要走好几遍。
    effective = app_runs(segments) if runs is None else runs
    return [run for run in effective if run.duration >= threshold]


def switch_count(segments: Iterable[Segment], *, runs: list[Segment] | None = None) -> int:
    """应用之间的切换次数（同一应用换个窗口标题不算切换）。"""
    effective = app_runs(segments) if runs is None else runs
    return sum(1 for left, right in pairwise(effective) if left.exe != right.exe)


# ------------------------------------------------------------------ 小工具


def _intervals(samples: Sequence[Sample]) -> list[float]:
    out = []
    for index in range(1, len(samples)):
        delta = samples[index].ts - samples[index - 1].ts
        if delta > 0:
            out.append(delta)
    return out


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0

"""纯数据类与常量（不碰 IO，不 import 系统模块）。

内部时间单位统一用 **epoch 秒（float）**：算差值、比大小、做区间运算都最省事，
只在落库（ISO 字符串）和展示（本地时间字符串）两个边界上转换。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 采样点自身的类型（一行 samples 的 kind）
SAMPLE_ACTIVE = "active"
SAMPLE_PAUSED = "paused"
SAMPLE_ERROR = "error"
SAMPLE_KINDS = (SAMPLE_ACTIVE, SAMPLE_PAUSED, SAMPLE_ERROR)

# 派生段的类型（segment.kind）
KIND_APP = "app"
KIND_IDLE = "idle"
KIND_PAUSED = "paused"
KIND_GAP = "gap"
KIND_LOCKED = "locked"
SEGMENT_KINDS = (KIND_APP, KIND_IDLE, KIND_PAUSED, KIND_GAP, KIND_LOCKED)

# 锁屏/登录界面：这些进程占据前台时，不代表有人在用机器。
LOCKED_EXES = frozenset({"lockapp.exe", "logonui.exe"})
# 只有在标题也为空时才判定为锁屏，避免误伤（例如某些 shell 弹窗）
LOCKED_SYSTEM_EXES = frozenset({"winlogon.exe", "csrss.exe"})

BUCKET_LABELS = {
    KIND_IDLE: "（空闲）",
    KIND_PAUSED: "（已暂停）",
    KIND_GAP: "（未记录）",
    KIND_LOCKED: "（锁屏）",
}


@dataclass(frozen=True, slots=True)
class Sample:
    """一次采样。时间是 epoch 秒。

    这里**不存单调钟**：进程内算间隔是采样器自己的事（用 time.monotonic），
    存进库里既没人读，又白占每行 8 字节。
    """

    ts: float
    pid: int
    exe: str
    title: str
    idle_s: float
    kind: str = SAMPLE_ACTIVE
    note: str = ""

    def is_idle(self, threshold: float) -> bool:
        return self.idle_s >= threshold


@dataclass(slots=True)
class Segment:
    """一段时间被归给谁。`kind` 见 SEGMENT_KINDS。"""

    kind: str
    start: float
    end: float
    exe: str = ""
    title: str = ""
    estimated: bool = False
    excluded: bool = False

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def overlaps(self, start: float, end: float) -> bool:
        return self.start < end and start < self.end


@dataclass(slots=True)
class Segments:
    """一份样本流切分后的全部结果。"""

    segments: list[Segment] = field(default_factory=list)
    sample_count: int = 0
    error_count: int = 0
    discarded: int = 0
    first_ts: float | None = None
    last_ts: float | None = None
    max_interval: float = 0.0
    median_interval: float = 0.0

    def of_kind(self, kind: str) -> list[Segment]:
        return [segment for segment in self.segments if segment.kind == kind]

    def seconds_of_kind(self, kind: str) -> float:
        return sum(segment.duration for segment in self.of_kind(kind))


@dataclass(frozen=True, slots=True)
class Range:
    """报告区间。`None` 表示不设边界（`all` 或半开区间）。"""

    start: float | None
    end: float | None
    label: str


@dataclass(frozen=True, slots=True)
class Pause:
    """一段"明确不记"的时间。`end_ts=None` 表示还在暂停中。"""

    start_ts: float
    end_ts: float | None = None
    note: str = ""

    @property
    def is_open(self) -> bool:
        return self.end_ts is None

    def covers(self, moment: float) -> bool:
        if moment < self.start_ts:
            return False
        return self.end_ts is None or moment < self.end_ts

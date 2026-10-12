"""采样循环：取窗口 → 组装 Sample → 交给 sink → 按间隔睡到下一个采样点。

provider / sink / 时钟 / sleep 全部注入，所以这个模块在任何平台上都能被完整测试；
Windows 特有的东西只在 winapi.py 里。循环本身坚持一条纪律：
**任何单次采样的失败都不能中断循环，也不能悄悄消失** —— 失败的采样会以 `kind=error`
落库并在 summary 里计数。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from timetrace import winapi
from timetrace.config import Config
from timetrace.domain import SAMPLE_ACTIVE, SAMPLE_ERROR, SAMPLE_PAUSED, Pause, Sample
from timetrace.winapi import WindowInfo

MAX_TITLE = 512
# 连续写失败这么多次就停：磁盘满 / 库坏了的时候，宁可带着明确的退出码停下，
# 也不要空转着"假装还在记"—— 那正是这个项目最不能容忍的谎。
MAX_CONSECUTIVE_WRITE_FAILURES = 5


class WindowProvider(Protocol):
    def read(self) -> WindowInfo: ...
    def idle_seconds(self) -> float: ...
    def describe(self) -> str: ...


class SampleSink(Protocol):
    def insert_sample(self, sample: Sample) -> None: ...
    def active_pause(self, now: float) -> Pause | None: ...


_WHITESPACE_CONTROLS = frozenset("\t\n\r\v\f")


def normalize_title(raw: str) -> str:
    """窗口标题是任意外部输入：清洗控制字符、压平空白、限长。

    浏览器标签页标题可以塞进任何东西，所以这里要比"信任输入"更谨慎。
    制表/换行在原文本里是分隔符，转成空格；其余控制字符（NUL、响铃等）直接丢掉。
    """
    if not raw:
        return ""
    parts = []
    for char in raw:
        if char in _WHITESPACE_CONTROLS:
            parts.append(" ")
        elif char >= " " and char != "\x7f":
            parts.append(char)
    return " ".join("".join(parts).split())[:MAX_TITLE]


def normalize_exe(raw: str) -> str:
    return normalize_title(raw).lower()


class WindowsProvider:
    """真实实现，转调 winapi。单独一类只是为了把注入点写明白。"""

    def read(self) -> WindowInfo:
        return winapi.foreground_window()

    def idle_seconds(self) -> float:
        return winapi.idle_seconds()

    def describe(self) -> str:
        return "Win32 GetForegroundWindow + GetLastInputInfo"


@dataclass(slots=True)
class RunSummary:
    started_at: float = 0.0
    stopped_at: float = 0.0
    samples: int = 0
    errors: int = 0
    paused: int = 0
    write_errors: int = 0
    stopped_by: str = "limit"
    last_sample_ms: float = 0.0
    max_sample_ms: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.stopped_at - self.started_at)

    def describe(self) -> str:
        write_note = f"、写入失败 {self.write_errors} 条" if self.write_errors else ""
        stop_note = "" if self.stopped_by in {"limit", "signal"} else f"，因 {self.stopped_by} 停止"
        return (
            f"写入 {self.samples} 条采样（异常 {self.errors}、暂停 {self.paused}{write_note}），"
            f"单次采样 P-max {self.max_sample_ms:.1f}ms，用时 {self.duration:.0f}s{stop_note}"
        )


class Sampler:
    def __init__(
        self,
        *,
        provider: WindowProvider,
        sink: SampleSink,
        config: Config,
        clock: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        on_event: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.provider = provider
        self.sink = sink
        self.config = config
        self._clock = clock
        self._mono = mono
        self._sleeper = sleeper
        self._on_event = on_event
        # 出错走单独的回调：`--quiet` 关掉的是逐条刷屏，不是"出事了要不要留痕"。
        # 文件日志要的是后者 —— 人不在屏幕前时，这是唯一能回答"它到底怎么了"的东西。
        self._on_error = on_error
        self._next_due = 0.0
        self._last_ms = 0.0

    # -------------------------------------------------------------- 单次

    def sample_once(self) -> Sample:
        started = self._mono()
        ts = self._clock()
        pause = self.sink.active_pause(ts)
        if pause is not None:
            sample = Sample(
                ts=ts,
                pid=0,
                exe="",
                title="",
                idle_s=0.0,
                kind=SAMPLE_PAUSED,
                note=normalize_title(pause.note),
            )
            self._last_ms = (self._mono() - started) * 1000.0
            return sample

        kind = SAMPLE_ACTIVE
        note = ""
        try:
            info = self.provider.read()
            idle = float(self.provider.idle_seconds())
        # 这里是刻意兜住所有异常的边界：一次采样失败只能降级成一条 error 记录，
        # 绝不能把整个常驻循环带走。
        except Exception as cause:
            info = WindowInfo(pid=0, exe="", title="")
            idle = 0.0
            kind = SAMPLE_ERROR
            note = f"{type(cause).__name__}: {cause}"
        self._last_ms = (self._mono() - started) * 1000.0
        return Sample(
            ts=ts,
            pid=max(0, info.pid),
            exe=normalize_exe(info.exe),
            title=normalize_title(info.title) if self.config.titles else "",
            idle_s=max(0.0, idle),
            kind=kind,
            note=note,
        )

    # -------------------------------------------------------------- 循环

    def run(
        self,
        *,
        max_samples: int | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> RunSummary:
        summary = RunSummary(started_at=self._clock())
        self._next_due = self._mono()
        consecutive_write_failures = 0
        while True:
            if should_stop is not None and should_stop():
                summary.stopped_by = "signal"
                break

            sample = self.sample_once()
            try:
                self.sink.insert_sample(sample)
            # 写库失败同样不能把常驻循环带走：磁盘瞬时忙、别的进程短暂锁住都该扛过去。
            # 但连续失败到上限就必须停 —— 磁盘满 / 库坏了时空转着"假装在记"更糟。
            except Exception as cause:
                summary.write_errors += 1
                consecutive_write_failures += 1
                self._emit_write_failure(consecutive_write_failures, cause)
                if consecutive_write_failures >= MAX_CONSECUTIVE_WRITE_FAILURES:
                    summary.stopped_by = "write-error"
                    break
            else:
                consecutive_write_failures = 0
                summary.samples += 1
                if sample.kind == SAMPLE_PAUSED:
                    summary.paused += 1
                elif sample.kind == SAMPLE_ERROR:
                    summary.errors += 1
                    self._emit_error(f"采样失败：{sample.note}")
                summary.max_sample_ms = max(summary.max_sample_ms, self._last_ms)
                self._emit(sample)

            if max_samples is not None and summary.samples >= max_samples:
                summary.stopped_by = "limit"
                break

            self._schedule_next()
            delay = self._next_due - self._mono()
            if delay > 0:
                self._sleeper(delay)

        summary.stopped_at = self._clock()
        summary.last_sample_ms = self._last_ms
        return summary

    def _schedule_next(self) -> None:
        """按固定节奏推进；落后太多（休眠/暂停）就重新对齐，不补跑一串采样。"""
        now = self._mono()
        self._next_due += self.config.interval
        if self._next_due < now - self.config.interval:
            self._next_due = now

    def _emit_write_failure(self, consecutive: int, cause: Exception) -> None:
        self._emit_error(
            f"写入失败（连续 {consecutive}/{MAX_CONSECUTIVE_WRITE_FAILURES} 次）："
            f"{type(cause).__name__}: {cause}"
        )

    def _emit_error(self, message: str) -> None:
        if self._on_event is not None:
            self._on_event(message)
        if self._on_error is not None:
            self._on_error(message)

    def _emit(self, sample: Sample) -> None:
        if self._on_event is None:
            return
        if sample.kind == SAMPLE_ERROR:
            self._on_event(f"采样失败：{sample.note}")
        elif sample.kind == SAMPLE_PAUSED:
            return  # 暂停期间不刷屏
        else:
            self._on_event(f"{sample.exe or '（无窗口）'}  {sample.title}"[:160])


def make_provider() -> WindowProvider:
    """按平台给出 provider；不支持就让它抛 PlatformError（调用方决定怎么呈现）。"""
    return WindowsProvider()

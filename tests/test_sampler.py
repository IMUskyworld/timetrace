"""sampler：注入假 provider / 假时钟 / 假 sink，覆盖循环的每条分支。

这些测试在任何平台上都能跑（Linux CI 也绿），因为 Windows 特有的东西都被 provider 挡住了。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from timetrace.config import Config
from timetrace.domain import SAMPLE_ACTIVE, SAMPLE_ERROR, SAMPLE_PAUSED, Pause, Sample
from timetrace.sampler import MAX_TITLE, Sampler, normalize_exe, normalize_title
from timetrace.winapi import WindowInfo

HOME = WindowInfo(pid=100, exe="Code.exe", title="main.py — project")
BROWSER = WindowInfo(pid=200, exe="msedge.exe", title="文档")


# ------------------------------------------------------------------ 脚手架


class FakeClock:
    """时间、单调钟、sleep 共用一个虚拟时间轴，测试因此完全确定。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += max(0.0, seconds)

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakeProvider:
    responses: list[object]
    index: int = -1
    idle_value: float = 0.0

    def read(self) -> WindowInfo:
        self.index += 1
        entry = self.responses[min(self.index, len(self.responses) - 1)]
        if isinstance(entry, Exception):
            raise entry
        info, self.idle_value = entry  # type: ignore[misc]
        return info

    def idle_seconds(self) -> float:
        return self.idle_value

    def describe(self) -> str:
        return "fake"


class FakeSink:
    def __init__(self, pause: Pause | None = None) -> None:
        self.samples: list[Sample] = []
        self.pause = pause
        self.pause_checks = 0

    def insert_sample(self, sample: Sample) -> None:
        self.samples.append(sample)

    def active_pause(self, now: float) -> Pause | None:
        self.pause_checks += 1
        if self.pause is not None and self.pause.covers(now):
            return self.pause
        return None


def build(responses: list[object], *, config: Config | None = None, pause: Pause | None = None):
    clock = FakeClock()
    provider = FakeProvider(responses)
    sink = FakeSink(pause)
    sampler = Sampler(
        provider=provider,
        sink=sink,
        config=config or Config(),
        clock=clock.time,
        mono=clock.monotonic,
        sleeper=clock.sleep,
    )
    return sampler, provider, sink, clock


# ------------------------------------------------------------------ 规范化


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("  多   空格  ", "多 空格"),
        ("带\n换行\t和制表", "带 换行 和制表"),
        ("NUL\x00截断", "NUL截断"),
        ("控制\x07字符", "控制字符"),
    ],
)
def test_normalize_title_清洗标题(raw: str, expected: str):
    assert normalize_title(raw) == expected


def test_normalize_title_限长():
    assert len(normalize_title("字" * (MAX_TITLE + 200))) == MAX_TITLE


def test_normalize_exe_转小写():
    assert normalize_exe("Code.EXE") == "code.exe"
    assert normalize_exe("") == ""


# ------------------------------------------------------------------ 单次采样


def test_sample_once_正常路径():
    sampler, _, sink, clock = build([(HOME, 3.5)])
    sample = sampler.sample_once()
    assert sample.kind == SAMPLE_ACTIVE
    assert sample.exe == "code.exe"
    assert sample.title == "main.py — project"
    assert sample.pid == 100
    assert sample.idle_s == 3.5
    assert sample.ts == clock.time()
    assert sink.samples == []  # sample_once 不落库，run 才落


def test_sample_once_no_titles_时不写标题():
    sampler, _, _, _ = build([(HOME, 0.0)], config=Config(titles=False))
    assert sampler.sample_once().title == ""


def test_sample_once_暂停期间不读窗口():
    pause = Pause(start_ts=0.0, end_ts=None, note="开会")
    sampler, provider, _, _ = build([(HOME, 0.0)], pause=pause)
    sample = sampler.sample_once()
    assert sample.kind == SAMPLE_PAUSED
    assert sample.note == "开会"
    assert sample.exe == "" and sample.title == ""
    assert provider.index == -1, "暂停期间不该去读前台窗口"


def test_sample_once_provider_抛异常时降级成error采样():
    sampler, _, _, _ = build([RuntimeError("KABOOM")])
    sample = sampler.sample_once()
    assert sample.kind == SAMPLE_ERROR
    assert "KABOOM" in sample.note
    assert sample.exe == ""
    assert sample.pid == 0


def test_sample_once_负空闲被夹到零():
    sampler, _, _, _ = build([(HOME, -12.0)])
    assert sampler.sample_once().idle_s == 0.0


def test_sample_once_记录自身耗时():
    sampler, _, _, clock = build([(HOME, 0.0)])
    sampler.sample_once()
    assert sampler._last_ms >= 0.0
    assert clock.slept == []


# ------------------------------------------------------------------ 循环


def test_run_按上限停止并逐条落库():
    sampler, _, sink, clock = build([(HOME, 0.0), (BROWSER, 1.0)])
    summary = sampler.run(max_samples=3)
    assert summary.stopped_by == "limit"
    assert summary.samples == 3
    assert len(sink.samples) == 3
    assert [sample.exe for sample in sink.samples] == ["code.exe", "msedge.exe", "msedge.exe"]
    assert summary.duration == pytest.approx(10.0)  # 3 条、间隔 5s
    assert clock.slept == [5.0, 5.0]


def test_run_should_stop_提前退出():
    sampler, _, sink, _ = build([(HOME, 0.0)])
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    summary = sampler.run(should_stop=should_stop)
    assert summary.stopped_by == "signal"
    assert summary.samples == 2
    assert len(sink.samples) == 2


def test_run_统计异常与暂停():
    pause = Pause(start_ts=0.0, end_ts=None, note="")
    sampler, _, sink, _ = build([(HOME, 0.0), RuntimeError("boom")], pause=pause)
    summary = sampler.run(max_samples=3)
    assert summary.samples == 3
    assert summary.paused == 3, "暂停区间覆盖了全部采样"
    assert summary.errors == 0
    assert all(sample.kind == SAMPLE_PAUSED for sample in sink.samples)


def test_run_采样异常不中断循环():
    sampler, _, sink, _ = build([RuntimeError("第一次失败"), (HOME, 1.0)])
    summary = sampler.run(max_samples=2)
    assert summary.errors == 1
    assert [sample.kind for sample in sink.samples] == [SAMPLE_ERROR, SAMPLE_ACTIVE]
    assert sink.samples[1].exe == "code.exe"


def test_run_记录最大采样耗时():
    sampler, _, _, _ = build([(HOME, 0.0)])
    summary = sampler.run(max_samples=1)
    assert summary.max_sample_ms >= 0.0
    assert summary.last_sample_ms == summary.max_sample_ms
    assert "写入 1 条采样" in summary.describe()


def test_run_时钟大幅跳跃后不补跑一串采样():
    sampler, provider, sink, clock = build([(HOME, 0.0)])
    original_read = provider.read

    def read_and_jump():
        info = original_read()
        clock.advance(3600)  # 模拟笔记本休眠一小时
        return info

    provider.read = read_and_jump  # type: ignore[method-assign]
    summary = sampler.run(max_samples=2)
    assert summary.samples == 2
    assert len(sink.samples) == 2
    assert all(delay <= 5.0 for delay in clock.slept), f"不该有负延迟或突发补跑：{clock.slept}"


def test_run_on_event_收到可读事件():
    events: list[str] = []
    sampler, _, _, _ = build([(HOME, 0.0), RuntimeError("boom")])
    sampler._on_event = events.append
    sampler.run(max_samples=2)
    assert any("code.exe" in event for event in events)
    assert any("采样失败" in event and "boom" in event for event in events)

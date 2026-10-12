"""segment：守恒律、空闲回填、gap/锁屏优先级、专注块与切换次数。

这是全工具最要紧的一组测试 —— 报告里的每个数字都从这里来。
"""

from __future__ import annotations

import random
from itertools import pairwise

import pytest

from builders import at, sample
from timetrace.config import Config
from timetrace.domain import (
    KIND_APP,
    KIND_GAP,
    KIND_IDLE,
    KIND_LOCKED,
    KIND_PAUSED,
    Pause,
    Sample,
    Segment,
)
from timetrace.segment import (
    app_runs,
    app_totals,
    build_segments,
    focus_blocks,
    is_locked,
    switch_count,
)

INTERVAL = 5.0
THRESHOLD = 120.0
CONFIG = Config(interval=INTERVAL, idle_threshold=THRESHOLD)


def total(segments: list[Segment]) -> float:
    return sum(segment.duration for segment in segments)


def kinds(segments: list[Segment]) -> list[str]:
    return [segment.kind for segment in segments]


def assert_conserved(result, *, span: tuple[float, float]) -> None:
    """守恒律：所有段加起来 == 时间轴长度，无缝无叠。"""
    expected = span[1] - span[0]
    assert total(result.segments) == pytest.approx(expected, abs=0.002), (
        f"段总长 {total(result.segments)} != 区间 {expected}：{kinds(result.segments)}"
    )
    cursor = span[0]
    for segment in result.segments:
        assert segment.start == pytest.approx(cursor, abs=0.002), f"段之间有缝：{segment}"
        cursor = segment.end
    assert cursor == pytest.approx(span[1], abs=0.002)


# ------------------------------------------------------------------ 规格书里的例子


def test_规格书_9_4_的例子_空闲回填把挂机时间归零():
    """DEVELOPMENT.md §9.4 的可验算例子：interval=5s, threshold=120s。"""
    samples = [
        sample(at("10:00:00"), "code.exe", idle=2),
        sample(at("10:00:05"), "code.exe", idle=7),
        sample(at("10:00:10"), "msedge.exe", idle=12),
        sample(at("10:00:15"), "msedge.exe", idle=130),
        sample(at("10:00:20"), "msedge.exe", idle=135),
    ]
    samples += [
        sample(at("10:00:25") + index * 5, "msedge.exe", idle=140 + index * 5) for index in range(7)
    ]  # 10:00:25 … 10:00:55，idle 一路涨到 170
    samples.append(sample(at("10:01:00"), "code.exe", idle=1))

    result = build_segments(samples, config=CONFIG)
    assert result.segments[0].start == pytest.approx(at("10:00:00"))
    assert result.segments[-1].end == pytest.approx(at("10:01:05"))  # 末条 + interval
    assert_conserved(result, span=(at("10:00:00"), at("10:01:05")))

    idle = result.seconds_of_kind(KIND_IDLE)
    assert idle == pytest.approx(60.0), (
        f"空闲应为 60s，实际 {idle}：{[(s.kind, s.duration) for s in result.segments]}"
    )
    assert app_totals(result.segments) == {"code.exe": pytest.approx(5.0)}
    assert "msedge.exe" not in app_totals(result.segments)


def test_空档按规则算未记录而不是空闲():
    """规格书 §9.4 的反例：s4 之后直接跳到 10:01:00（40 秒空档 > gap_limit 15s）。"""
    samples = [
        sample(at("10:00:00"), "code.exe", idle=2),
        sample(at("10:00:05"), "code.exe", idle=7),
        sample(at("10:00:10"), "msedge.exe", idle=12),
        sample(at("10:00:15"), "msedge.exe", idle=130),
        sample(at("10:00:20"), "msedge.exe", idle=135),
        sample(at("10:01:00"), "code.exe", idle=1),
    ]
    result = build_segments(samples, config=CONFIG)
    assert kinds(result.segments) == [KIND_IDLE, KIND_GAP, KIND_APP]
    assert result.seconds_of_kind(KIND_IDLE) == pytest.approx(20.0)
    assert result.seconds_of_kind(KIND_GAP) == pytest.approx(40.0)
    assert app_totals(result.segments) == {"code.exe": pytest.approx(5.0)}
    assert_conserved(result, span=(at("10:00:00"), at("10:01:05")))


def test_没有回填就会虚报_15_秒():
    """同一个例子，把阈值调到很小 → 空闲不再吃掉前面的段，暴露"只在发现时才计空闲"的错觉。"""
    samples = [
        sample(at("10:00:00"), "code.exe", idle=2),
        sample(at("10:00:05"), "code.exe", idle=7),
        sample(at("10:00:10"), "msedge.exe", idle=12),
        sample(at("10:00:15"), "msedge.exe", idle=130),
    ]
    strict = build_segments(samples, config=Config(interval=INTERVAL, idle_threshold=10.0))
    assert app_totals(strict.segments) == {}  # 阈值 10s 时，前面几条也算空闲
    lenient = build_segments(samples, config=Config(interval=INTERVAL, idle_threshold=1000.0))
    assert sum(app_totals(lenient.segments).values()) > 0  # 阈值高 → 全算在用


# ------------------------------------------------------------------ 守恒律（属性测试）


@pytest.mark.parametrize("seed", [1, 2, 3, 7, 42, 99, 1234])
def test_随机样本流始终守恒(seed: int):
    rng = random.Random(seed)
    apps = ["code.exe", "msedge.exe", "explorer.exe", ""]
    moment = at("09:00")
    samples: list[Sample] = []
    for _ in range(200):
        step = rng.choice([INTERVAL, INTERVAL, INTERVAL, INTERVAL * 4])  # 偶尔来一次断档
        moment += step
        idle = rng.choice([0.0, 3.0, 30.0, 150.0, 4000.0])
        samples.append(sample(moment, rng.choice(apps), idle=idle, pid=rng.randint(1, 999)))
    result = build_segments(samples, config=CONFIG)
    assert_conserved(result, span=(samples[0].ts, samples[-1].ts + INTERVAL))


def test_守恒律_有暂停区间时也成立():
    samples = [
        sample(at("10:00:00"), "code.exe"),
        sample(at("10:00:05"), "code.exe"),
        sample(at("10:00:10"), "code.exe"),
        sample(at("10:00:15"), "code.exe"),
    ]
    pauses = [Pause(start_ts=at("10:00:07"), end_ts=at("10:00:13"))]
    result = build_segments(samples, config=CONFIG, pauses=pauses)
    assert_conserved(result, span=(at("10:00:00"), at("10:00:20")))
    assert result.seconds_of_kind(KIND_PAUSED) == pytest.approx(6.0)
    assert result.seconds_of_kind(KIND_APP) == pytest.approx(14.0)


# ------------------------------------------------------------------ 边界


def test_空输入给空结果():
    result = build_segments([], config=CONFIG)
    assert result.segments == []
    assert result.sample_count == 0
    assert result.first_ts is None


def test_单条采样也是一段且标了估算():
    result = build_segments([sample(at("10:00"), "code.exe")], config=CONFIG)
    assert len(result.segments) == 1
    segment = result.segments[0]
    assert segment.duration == pytest.approx(INTERVAL)
    assert segment.estimated is True


def test_全空闲的一天没有应用时间():
    samples = [sample(at("09:00") + index * INTERVAL, "code.exe", idle=600) for index in range(10)]
    result = build_segments(samples, config=CONFIG)
    assert app_totals(result.segments) == {}
    assert result.seconds_of_kind(KIND_IDLE) == pytest.approx(total(result.segments))


def test_时间倒退的采样被丢弃并计数():
    samples = [
        sample(at("10:00:10"), "code.exe"),
        sample(at("10:00:00"), "code.exe"),  # NTP 把钟拨回去了
        sample(at("10:00:20"), "msedge.exe"),
    ]
    result = build_segments(samples, config=CONFIG)
    assert result.discarded >= 1
    assert all(segment.duration > 0 for segment in result.segments)


def test_采样间隔统计():
    samples = [
        sample(at("10:00:00"), "a.exe"),
        sample(at("10:00:05"), "a.exe"),
        sample(at("10:00:10"), "a.exe"),
        sample(at("10:00:40"), "a.exe"),  # 30 秒断档
    ]
    result = build_segments(samples, config=CONFIG)
    assert result.max_interval == pytest.approx(30.0)
    assert result.median_interval == pytest.approx(5.0)


def test_异常采样被计数():
    error = sample(at("10:00:05"), "", kind="error", note="KABOOM")
    samples = [sample(at("10:00:00"), "code.exe"), error]
    result = build_segments(samples, config=CONFIG)
    assert result.error_count == 1


# ------------------------------------------------------------------ 优先级


def test_断档压过一切():
    samples = [
        sample(at("10:00:00"), "code.exe", idle=999),  # 就算同时满足空闲
        sample(at("10:01:00"), "code.exe"),  # 60 秒 > gap_limit(15s)
    ]
    result = build_segments(samples, config=CONFIG)
    assert kinds(result.segments) == [KIND_GAP, KIND_APP]
    assert result.seconds_of_kind(KIND_GAP) == pytest.approx(60.0)
    assert app_totals(result.segments) == {"code.exe": pytest.approx(5.0)}
    assert_conserved(result, span=(at("10:00:00"), at("10:01:05")))


def test_暂停压过空闲与应用():
    samples = [sample(at("10:00:00") + index * INTERVAL, "code.exe", idle=999) for index in range(4)]
    pauses = [Pause(start_ts=at("10:00:00"), end_ts=at("10:00:20"))]
    result = build_segments(samples, config=CONFIG, pauses=pauses)
    assert kinds(result.segments) == [KIND_PAUSED]
    assert result.seconds_of_kind(KIND_PAUSED) == pytest.approx(20.0)


def test_锁屏压过应用但可以被断档覆盖():
    samples = [
        sample(at("10:00:00"), "lockapp.exe"),
        sample(at("10:00:05"), "lockapp.exe"),
        sample(at("10:00:10"), "code.exe"),
    ]
    result = build_segments(samples, config=CONFIG)
    assert result.seconds_of_kind(KIND_LOCKED) == pytest.approx(10.0)
    assert app_totals(result.segments) == {"code.exe": pytest.approx(5.0)}

    with_gap = build_segments(
        [sample(at("10:00:00"), "lockapp.exe"), sample(at("11:00:00"), "code.exe")],
        config=CONFIG,
    )
    assert with_gap.seconds_of_kind(KIND_GAP) == pytest.approx(3600.0)
    assert with_gap.seconds_of_kind(KIND_LOCKED) == 0.0


def test_空闲不覆盖锁屏与暂停():
    pause = [Pause(start_ts=at("10:00:05"), end_ts=at("10:00:10"))]
    samples = [
        sample(at("10:00:00"), "lockapp.exe", idle=999),
        sample(at("10:00:05"), "code.exe", idle=999),
        sample(at("10:00:10"), "code.exe", idle=999),
    ]
    result = build_segments(samples, config=CONFIG, pauses=pause)
    assert KIND_LOCKED in kinds(result.segments)
    assert KIND_PAUSED in kinds(result.segments)
    assert result.seconds_of_kind(KIND_IDLE) > 0


# ------------------------------------------------------------------ 锁屏判定


@pytest.mark.parametrize("exe", ["lockapp.exe", "LockApp.EXE", "logonui.exe"])
def test_锁屏进程(exe: str):
    assert is_locked(sample(at("10:00"), exe))


def test_winlogon_只有标题为空才算锁屏():
    assert is_locked(sample(at("10:00"), "winlogon.exe", title=""))
    assert not is_locked(sample(at("10:00"), "winlogon.exe", title="正在登录"))
    assert not is_locked(sample(at("10:00"), "csrss.exe", title="控制台"))


def test_普通应用当然不算锁屏():
    assert not is_locked(sample(at("10:00"), "code.exe", title="main.py"))


# ------------------------------------------------------------------ 派生统计


def test_app_runs_按应用串联而不按标题():
    samples = [
        sample(at("10:00:00"), "code.exe", title="a.py"),
        sample(at("10:00:05"), "code.exe", title="b.py"),
        sample(at("10:00:10"), "code.exe", title="b.py"),
    ]
    result = build_segments(samples, config=CONFIG)
    runs = app_runs(result.segments)
    assert len(runs) == 1
    assert runs[0].duration == pytest.approx(15.0)
    assert switch_count(result.segments) == 0


def test_切换次数_忽略空闲但看重应用变化():
    samples = [
        sample(at("10:00:00"), "code.exe"),
        sample(at("10:00:05"), "msedge.exe"),
        sample(at("10:00:10"), "msedge.exe"),
        sample(at("10:00:15"), "code.exe"),
    ]
    result = build_segments(samples, config=CONFIG)
    assert switch_count(result.segments) == 2
    assert [run.exe for run in app_runs(result.segments)] == ["code.exe", "msedge.exe", "code.exe"]


def test_专注块只看够长的连续段():
    samples = [sample(at("09:00") + index * 60, "code.exe") for index in range(21)]  # 20 分钟
    samples += [sample(at("09:21") + index * 60, "msedge.exe") for index in range(6)]  # 5 分钟
    result = build_segments(samples, config=Config(interval=60.0, idle_threshold=THRESHOLD))
    blocks = focus_blocks(result.segments, focus_min=10.0)
    assert [block.exe for block in blocks] == ["code.exe"]
    assert blocks[0].duration == pytest.approx(21 * 60, abs=1.0)


def test_被空闲打断就不算一个专注块():
    samples = [sample(at("09:00") + index * 60, "code.exe") for index in range(6)]
    samples.append(sample(at("09:06"), "code.exe", idle=500))  # 中途离开
    samples += [sample(at("09:07") + index * 60, "code.exe") for index in range(6)]
    result = build_segments(samples, config=Config(interval=60.0, idle_threshold=THRESHOLD))
    assert focus_blocks(result.segments, focus_min=10.0) == []


def test_排除自己与排除名单():
    samples = [
        sample(at("10:00:00"), "python.exe", pid=777),
        sample(at("10:00:05"), "slack.exe", pid=888),
        sample(at("10:00:10"), "code.exe", pid=999),
    ]
    result = build_segments(samples, config=CONFIG, own_pids=(777,), excluded=["Slack.exe"])
    flags = {segment.exe: segment.excluded for segment in result.segments}
    assert flags["python.exe"] is True, "tracker 自己必须被自动排除"
    assert flags["slack.exe"] is True
    assert flags["code.exe"] is False
    # 排除只影响排名展示，不影响总量
    assert app_totals(result.segments)["python.exe"] == pytest.approx(5.0)


def test_墙钟回拨不产生重叠段():
    """NTP 把墙钟往回拨时，后一条采样的 ts 会小于前一条。

    照抄 ts 会得到互相重叠的段（sum(时长) > span），守恒律当场破掉 ——
    模块开头承诺的"无缝隙、无重叠"必须兑现。
    """
    base = at("09:00")
    samples = [
        sample(base + 0, "code.exe"),
        sample(base + 40, "code.exe"),
        sample(base - 20, "msedge.exe"),  # 时钟被拨回 60s
        sample(base + 20, "msedge.exe"),
    ]
    segments = build_segments(samples, config=Config(interval=60.0, idle_threshold=300.0))

    assert len(segments.segments) >= 2
    for left, right in pairwise(segments.segments):
        assert right.start >= left.end - 1e-9, f"段重叠了：{left} 与 {right}"

    total = sum(item.duration for item in segments.segments)
    span = segments.segments[-1].end - segments.segments[0].start
    assert total == pytest.approx(span, abs=1e-6), "重叠会让 sum(时长) 大于 span"
    assert segments.discarded > 0, "回拨造成的异常必须被计数，不能悄悄抹掉"


def test_时钟回拨后仍然首尾相接():
    """削掉重叠之后不能留下空洞 —— 守恒要求无缝。"""
    base = at("09:00")
    samples = [
        sample(base + 0, "code.exe"),
        sample(base + 40, "code.exe"),
        sample(base - 10, "msedge.exe"),
        sample(base + 200, "msedge.exe"),
    ]
    config = Config(interval=60.0, idle_threshold=300.0, gap_limit_factor=3.0)
    segments = build_segments(samples, config=config)
    items = segments.segments
    assert len(items) >= 2
    for left, right in pairwise(items):
        assert right.start == pytest.approx(left.end, abs=1e-6), "中间不能留空洞"


def test_正常采样不受回拨修复影响():
    """回归：没有时钟异常时，discarded 必须是 0，段数与采样数一致。"""
    samples = [sample(at("09:00") + index * 60, "code.exe") for index in range(10)]
    segments = build_segments(samples, config=Config(interval=60.0, idle_threshold=300.0))
    assert segments.discarded == 0
    assert len(segments.segments) == 1
    assert segments.segments[0].duration == pytest.approx(10 * 60)

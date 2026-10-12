"""domain：纯数据类的边界语义。没有这些钉子，报告的口径会悄悄跑偏。"""

from __future__ import annotations

from timetrace.domain import (
    BUCKET_LABELS,
    KIND_APP,
    KIND_GAP,
    KIND_IDLE,
    KIND_LOCKED,
    KIND_PAUSED,
    SAMPLE_ACTIVE,
    SAMPLE_ERROR,
    SAMPLE_KINDS,
    SAMPLE_PAUSED,
    SEGMENT_KINDS,
    Pause,
    Range,
    Sample,
    Segment,
    Segments,
)

# ------------------------------------------------------------------ 采样


def test_is_idle_用的是大于等于():
    """阈值上刚好等于就算离开 —— `>=` 写成 `>` 会让"正好 5 分钟"算成在用。"""
    sample = Sample(ts=0.0, pid=1, exe="a.exe", title="", idle_s=300.0)
    assert sample.is_idle(300.0) is True
    assert sample.is_idle(300.1) is False
    assert Sample(ts=0.0, pid=1, exe="a.exe", title="", idle_s=299.9).is_idle(300.0) is False


def test_采样类型三种齐全():
    assert SAMPLE_KINDS == (SAMPLE_ACTIVE, SAMPLE_PAUSED, SAMPLE_ERROR)
    assert SAMPLE_ACTIVE != SAMPLE_PAUSED != SAMPLE_ERROR


# ------------------------------------------------------------------ 段


def test_duration_负长度归零():
    """时钟回拨可能造出 end < start；duration 必须是 0 而不是负数，否则守恒律会算错。"""
    segment = Segment(kind=KIND_APP, start=100.0, end=40.0, exe="a.exe")
    assert segment.duration == 0.0
    assert Segment(kind=KIND_APP, start=0.0, end=0.0).duration == 0.0
    assert Segment(kind=KIND_APP, start=0.0, end=10.0).duration == 10.0


def test_overlaps_是半开区间():
    """[start, end) ：首尾相接不算重叠 —— 换成 `<=` 会让相邻段被误判。"""
    segment = Segment(kind=KIND_APP, start=10.0, end=20.0)
    assert segment.overlaps(5.0, 10.0) is False, "左端相接不算重叠"
    assert segment.overlaps(20.0, 30.0) is False, "右端相接不算重叠"
    assert segment.overlaps(9.9, 10.0) is False, "查询区间右端正好抵住它，也不算重叠"
    assert segment.overlaps(9.9, 10.1) is True, "只要越过左端 1 个单位就算"
    assert segment.overlaps(19.9, 21.0) is True
    assert segment.overlaps(12.0, 14.0) is True, "完全包含"
    assert segment.overlaps(5.0, 25.0) is True, "完全包住它"


def test_段类型五种齐全():
    assert set(SEGMENT_KINDS) == {KIND_APP, KIND_IDLE, KIND_PAUSED, KIND_GAP, KIND_LOCKED}


def test_专用桶标签正好覆盖非应用的四种():
    """守恒律靠这个：除 app 外的每一种段都必须有中文标签，漏一个报告就少一行。"""
    assert set(BUCKET_LABELS) == set(SEGMENT_KINDS) - {KIND_APP}
    assert BUCKET_LABELS[KIND_IDLE] == "（空闲）"
    assert BUCKET_LABELS[KIND_GAP] == "（未记录）"


# ------------------------------------------------------------------ Segments


def test_Segments_按类型取段与求秒():
    bucket = Segments(
        segments=[
            Segment(kind=KIND_APP, start=0.0, end=10.0, exe="a.exe"),
            Segment(kind=KIND_IDLE, start=10.0, end=25.0),
            Segment(kind=KIND_APP, start=25.0, end=40.0, exe="b.exe"),
            Segment(kind=KIND_GAP, start=40.0, end=50.0),
        ]
    )
    assert len(bucket.of_kind(KIND_APP)) == 2
    assert bucket.seconds_of_kind(KIND_APP) == 25.0, "0–10 共 10 秒 + 25–40 共 15 秒"
    assert bucket.seconds_of_kind(KIND_IDLE) == 15.0
    assert bucket.seconds_of_kind(KIND_GAP) == 10.0
    assert bucket.seconds_of_kind(KIND_LOCKED) == 0.0, "没有就该是 0，而不是报错"
    # 五个桶加起来 == 所有段的总长
    total = sum(bucket.segments[i].duration for i in range(len(bucket.segments)))
    assert sum(bucket.seconds_of_kind(k) for k in SEGMENT_KINDS) == total


def test_空_Segments_各方法都安全():
    empty = Segments()
    assert empty.of_kind(KIND_APP) == []
    assert empty.seconds_of_kind(KIND_APP) == 0.0
    assert empty.first_ts is None and empty.last_ts is None


# ------------------------------------------------------------------ 暂停


def test_暂停区间是半开的():
    """covers 是 [start, end)：end 那一刻已经恢复，不该还算暂停。"""
    pause = Pause(start_ts=100.0, end_ts=200.0, note="开会")
    assert pause.covers(99.9) is False
    assert pause.covers(100.0) is True, "开始那一刻算暂停"
    assert pause.covers(199.9) is True
    assert pause.covers(200.0) is False, "结束那一刻已经恢复"
    assert pause.covers(200.1) is False


def test_未结束的暂停覆盖到永远():
    pause = Pause(start_ts=100.0, end_ts=None, note="没恢复")
    assert pause.is_open is True
    assert pause.covers(100.0) is True
    assert pause.covers(10**12) is True


def test_已结束的暂停不再开():
    pause = Pause(start_ts=100.0, end_ts=200.0)
    assert pause.is_open is False


def test_开始之前的任何时刻都不算():
    pause = Pause(start_ts=100.0, end_ts=200.0)
    assert pause.covers(0.0) is False
    assert pause.covers(99.999) is False


# ------------------------------------------------------------------ Range


def test_Range_允许开区间():
    """None 是"不设边界"，不是错误 —— all 和半开区间都靠它。"""
    assert Range(None, None, "全部").start is None
    assert Range(None, 100.0, "到 100").end == 100.0
    assert Range(0.0, None, "从 0").end is None
    assert Range(0.0, 100.0, "闭").label == "闭"

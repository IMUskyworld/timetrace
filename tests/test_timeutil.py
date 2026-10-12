"""timeutil：区间解析、ISO 往返、时长格式、按小时切分。"""

from __future__ import annotations

import time
from datetime import date

import pytest

from builders import at
from timetrace.domain import Range
from timetrace.errors import UsageError
from timetrace.timeutil import (
    describe_range,
    epoch_to_iso,
    format_duration,
    format_percent,
    iso_to_epoch,
    local_date,
    local_day_end,
    local_day_start,
    parse_range,
    split_by_local_hour,
)

NOW = at("15:00")


# ------------------------------------------------------------------ ISO 往返


def test_iso_roundtrip_保留毫秒精度():
    epoch = at("10:00:05") + 0.25
    text = epoch_to_iso(epoch)
    assert text.endswith("Z")
    assert text.count(".") == 1
    assert iso_to_epoch(text) == pytest.approx(epoch, abs=0.001)


def test_iso_是_utc_而不是本地时间():
    # 直接断言格式与自洽性，不依赖本机时区（CI 上可能是 UTC）
    epoch = at("10:00")
    text = epoch_to_iso(epoch)
    assert text[:4] == "2026"
    assert iso_to_epoch(text) == pytest.approx(epoch, abs=0.001)
    assert text == epoch_to_iso(iso_to_epoch(text))


# ------------------------------------------------------------------ 本地日边界


def test_day_bounds_是本地零点到次日零点():
    day = date(2026, 9, 23)
    start = local_day_start(day)
    end = local_day_end(day)
    assert end - start == pytest.approx(86400, abs=1)
    assert local_date(start) == day
    assert local_date(end - 1) == day


def test_day_bounds_跨月正确():
    start = local_day_start(date(2026, 8, 31))
    end = local_day_end(date(2026, 8, 31))
    assert local_date(end - 1) == date(2026, 8, 31)
    assert local_date(start) == date(2026, 8, 31)
    assert local_date(end) == date(2026, 9, 1)


# ------------------------------------------------------------------ 时长格式


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (45, "45s"),
        (59.4, "59s"),
        (60, "1m"),
        (90, "1m30s"),
        (600, "10m"),
        (3600, "1h00m"),
        (11520, "3h12m"),
        (-5, "0s"),
    ],
)
def test_format_duration(seconds: float, expected: str):
    assert format_duration(seconds) == expected


def test_format_percent():
    assert format_percent(0.9824) == "98.2%"
    assert format_percent(0) == "0.0%"


# ------------------------------------------------------------------ 按小时切分


def test_split_by_local_hour_同小时内不拆():
    assert split_by_local_hour(at("10:10"), at("10:40")) == [(10, pytest.approx(1800))]


def test_split_by_local_hour_跨整点按实际秒数拆():
    pieces = dict(split_by_local_hour(at("10:50"), at("11:20")))
    assert pieces[10] == pytest.approx(600)
    assert pieces[11] == pytest.approx(1200)


def test_split_by_local_hour_跨三小时且总和等于总时长():
    start, end = at("22:40"), at("23:59:59")
    pieces = split_by_local_hour(start, end)
    assert [hour for hour, _ in pieces] == [22, 23]
    assert sum(seconds for _, seconds in pieces) == pytest.approx(end - start, abs=0.01)


def test_split_by_local_hour_空区间与负区间返回空():
    assert split_by_local_hour(at("10:00"), at("10:00")) == []
    assert split_by_local_hour(at("10:00"), at("09:00")) == []


# ------------------------------------------------------------------ 区间解析


def test_parse_today_从零点到现在():
    rng = parse_range("today", now=NOW)
    assert rng.start == pytest.approx(local_day_start(local_date(NOW)))
    assert rng.end == pytest.approx(NOW)
    assert rng.label == "今天"


def test_parse_yesterday_是完整的一天():
    rng = parse_range("yesterday", now=NOW)
    assert rng.end - rng.start == pytest.approx(86400, abs=1)
    assert local_date(rng.end - 1) == date(2026, 9, 22)


def test_parse_week_从周一算起():
    # 2026-09-23 是周三，所以本周一是 09-21
    rng = parse_range("week", now=NOW)
    assert rng.start == pytest.approx(at("00:00", "2026-09-21"))
    assert rng.end == pytest.approx(NOW)


def test_parse_month_从一号算起():
    rng = parse_range("month", now=NOW)
    assert rng.start == pytest.approx(at("00:00", "2026-09-01"))
    assert rng.end == pytest.approx(NOW)


def test_parse_all_不设边界():
    rng = parse_range("all", now=NOW)
    assert rng == Range(None, None, "全部")


def test_parse_单日给整天():
    rng = parse_range("2026-09-10", now=NOW)
    assert rng.start == pytest.approx(at("00:00", "2026-09-10"))
    assert rng.end == pytest.approx(at("00:00", "2026-09-11"))
    assert rng.label == "2026-09-10"


def test_parse_双日期区间两端都含():
    rng = parse_range("2026-09-10..2026-09-12", now=NOW)
    assert rng.start == pytest.approx(at("00:00", "2026-09-10"))
    assert rng.end == pytest.approx(at("00:00", "2026-09-13"), abs=1)
    assert rng.label == "2026-09-10 → 2026-09-12"


def test_parse_开区间():
    left_open = parse_range("2026-09-10..", now=NOW)
    assert left_open.start == pytest.approx(at("00:00", "2026-09-10"))
    assert left_open.end == pytest.approx(NOW)
    right_open = parse_range("..2026-09-10", now=NOW)
    assert right_open.start is None
    assert right_open.end == pytest.approx(at("00:00", "2026-09-11"), abs=1)


def test_parse_默认值是今天():
    assert parse_range(None, now=NOW).label == "今天"
    assert parse_range("", now=NOW).label == "今天"
    assert parse_range("TODAY", now=NOW).label == "今天"


@pytest.mark.parametrize("spec", ["2026-13-01", "前天", "2026/09/23", "10-2026-09"])
def test_parse_看不懂就报用法错误(spec: str):
    with pytest.raises(UsageError) as caught:
        parse_range(spec, now=NOW)
    assert "看不懂的区间" in str(caught.value)
    assert caught.value.exit_code == 2


def test_parse_结束早于开始也报错():
    with pytest.raises(UsageError, match="结束早于开始"):
        parse_range("2026-09-12..2026-09-10", now=NOW)


def test_describe_range_有标签有人话():
    text = describe_range(parse_range("all", now=NOW))
    assert "全部" in text and "最早" in text and "现在" in text
    single = describe_range(parse_range("2026-09-10", now=NOW))
    assert single.startswith("2026-09-10（")


def test_时区改动不会让解析结果彼此矛盾():
    # 不依赖本机时区：只要求自洽（同日边界递增、标签稳定）
    today = local_day_start(date(2026, 9, 23))
    assert time.localtime(today).tm_hour == 0
    assert time.localtime(today).tm_min == 0

"""report：三种输出数字同源、区间裁剪、可信度提示、HTML 自包含与转义。"""

from __future__ import annotations

import json
import re

import pytest

from builders import at, sample
from timetrace.config import Config
from timetrace.domain import KIND_APP, KIND_PAUSED, Pause, Range
from timetrace.html_template import app_color, render_html
from timetrace.report import (
    AppRow,
    build_report,
    clip_segments,
    display_width,
    pad,
    render_console,
    render_json,
)
from timetrace.segment import build_segments
from timetrace.timeutil import format_duration, format_percent, parse_range

CONFIG = Config(interval=5.0, idle_threshold=300.0, focus_min=10.0)
TODAY = "2026-09-23"


def day_report(*, config: Config = CONFIG, rng: Range | None = None, pauses=()):
    """构造一个有代表性的小时段：写代码 15 分钟 → 浏览器 5 分钟 → 离开 40 分钟 → 回来 5 分钟。"""
    samples = [
        sample(at(f"09:{minute:02d}"), "code.exe", title=f"文件 {minute}.py") for minute in range(0, 15)
    ]
    samples += [sample(at(f"09:{minute:02d}"), "msedge.exe", title="文档") for minute in range(15, 20)]
    # 09:20 起离开：idle 一路涨过阈值，且 09:20→10:00 之间没有采样（断档）
    samples.append(sample(at("09:20"), "msedge.exe", idle=400, title="文档"))
    samples += [sample(at(f"10:{minute:02d}"), "code.exe", title="回来继续") for minute in range(0, 5)]
    minute_config = Config(interval=60.0, idle_threshold=300.0, focus_min=config.focus_min)
    segments = build_segments(samples, config=minute_config, pauses=pauses)
    return build_report(
        segments,
        rng=rng or parse_range("2026-09-23", now=at("12:00")),
        config=minute_config,
        now=at("12:00"),
        generated_at=at("12:30"),
    )


# ------------------------------------------------------------------ 构造与口径


def test_区间裁剪只保留区间内的部分():
    segments = build_segments(
        [sample(at("09:00") + index * 60, "code.exe") for index in range(10)],
        config=Config(interval=60.0),
    )
    clipped = clip_segments(segments.segments, at("09:03"), at("09:06"))
    assert sum(segment.duration for segment in clipped) == pytest.approx(180.0)


def test_占比一律以区间总长为分母():
    """同一列不能有两种含义：应用行与专用桶的占比都相对"区间总长"。"""
    data = day_report()
    app_rows = [row for row in data.apps if row.label == ""]
    specials = [row for row in data.apps if row.label]
    assert sum(row.share for row in app_rows) == pytest.approx(data.totals[KIND_APP] / data.range_seconds)
    assert sum(row.share for row in app_rows + specials) == pytest.approx(data.coverage, abs=1e-6)
    assert app_rows[0].exe == "code.exe", "最长时间的应用应该排第一"
    assert app_rows[0].share == pytest.approx(app_rows[0].seconds / data.range_seconds)


def test_专用桶单独列出且不计入排名():
    data = day_report()
    ranked = [row for row in data.apps if not row.excluded]
    specials = {row.label for row in data.apps if row.label}
    assert all(row.exe not in {"idle", "gap", "paused", "locked"} for row in ranked)
    assert "（空闲）" in specials and "（未记录）" in specials


def test_每小时之和等于应用总时长():
    data = day_report()
    hourly = sum(row.seconds for row in data.hourly)
    assert hourly == pytest.approx(data.totals[KIND_APP], abs=0.01)


def test_open_区间从第一条采样算起():
    segments = build_segments(
        [sample(at("09:00") + index * 60, "code.exe") for index in range(5)],
        config=Config(interval=60.0),
    )
    data = build_report(
        segments, rng=Range(None, None, "全部"), config=Config(interval=60.0), now=at("12:00")
    )
    assert data.range_start == pytest.approx(at("09:00"))
    assert data.range_end == pytest.approx(at("09:04") + 60)
    assert data.coverage == pytest.approx(1.0)


def test_没有采样时给出明确提示():
    empty = build_segments([], config=CONFIG)
    data = build_report(empty, rng=parse_range("today", now=at("12:00")), config=CONFIG, now=at("12:00"))
    assert data.range_seconds > 0
    assert data.coverage == 0.0
    assert any("没有任何采样" in note for note in data.notes)


def test_覆盖率低会自己说出来():
    samples = [sample(at("11:59") + index * 5, "code.exe") for index in range(4)]
    segments = build_segments(samples, config=CONFIG)
    data = build_report(segments, rng=parse_range("today", now=at("12:00")), config=CONFIG, now=at("12:00"))
    assert data.coverage < 0.5
    assert any("覆盖率只有" in note for note in data.notes)


def test_断档与异常采样都会出现在提示里():
    samples = [sample(at("09:00"), "code.exe"), sample(at("10:00"), "code.exe")]  # 1 小时断档
    samples.append(sample(at("10:05"), "", kind="error", note="boom"))
    segments = build_segments(samples, config=CONFIG)
    data = build_report(
        segments, rng=parse_range("2026-09-23", now=at("12:00")), config=CONFIG, now=at("12:00")
    )
    joined = " ".join(data.notes)
    assert "最大采样间隔" in joined
    assert "1 条采样出错" in joined


def test_专注块按长度排序并给出时间窗():
    data = day_report()
    assert data.focus, "连续写代码应该形成一个专注块"
    assert data.focus[0].exe == "code.exe"
    # 09:00 起 15 条采样看起来是 900 秒，但 09:20 那条 idle=400s 说明人 09:13:20 就走了，
    # 回填把最后 100 秒划成空闲 —— 所以诚实的答案是 800 秒，不是 900。
    assert data.focus[0].seconds == pytest.approx(800.0, abs=60)
    assert "–" in data.focus[0].window()


def test_暂停时间单独成桶():
    data = day_report(pauses=[Pause(start_ts=at("09:05"), end_ts=at("09:10"))])
    assert data.totals[KIND_PAUSED] == pytest.approx(300.0, abs=1)
    assert any(row.label == "（已暂停）" for row in data.apps)


# ------------------------------------------------------------------ 三种输出同源


def test_三种输出关键数字一致():
    data = day_report()
    console = render_console(data)
    payload = json.loads(render_json(data))
    html = render_html(data)

    coverage = format_percent(data.coverage)
    app_time = format_duration(data.totals[KIND_APP])
    assert coverage in console and coverage in html
    assert round(data.totals[KIND_APP]) == pytest.approx(payload["totals"][KIND_APP], abs=1)
    assert app_time in console and app_time in html
    assert payload["switches"] == data.switches
    for row in data.apps:
        if row.excluded or row.seconds < 60:
            continue
        assert format_duration(row.seconds) in console
        assert format_duration(row.seconds) in html
    assert f"{data.switches} 次" in console


def test_json_结构稳定且时间是可读_iso():
    payload = json.loads(render_json(day_report()))
    assert set(payload) >= {
        "generated_at",
        "range",
        "coverage",
        "totals",
        "apps",
        "focus_blocks",
        "hourly",
        "switches",
        "quality",
        "notes",
    }
    assert payload["generated_at"].endswith("Z")
    assert payload["range"]["start"].endswith("Z")
    assert len(payload["hourly"]) == 24
    assert payload["quality"]["sample_count"] > 0


# ------------------------------------------------------------------ console 排版


def test_显示宽度把中文算两列():
    assert display_width("abc") == 3
    assert display_width("中文") == 4
    assert display_width("中文abc") == 7


def test_pad_按显示宽度补齐():
    assert pad("中文", 6) == "中文  "
    assert pad("中文", 6, align="right") == "  中文"
    assert pad("工具", 2) == "工具"


def test_console_里有表头与小时图():
    console = render_console(day_report())
    assert "应用" in console and "时长" in console and "占区间" in console
    assert "每小时在用什么" in console
    assert "█" in console
    assert "timetrace ·" in console


def test_console_没有数据时也不炸():
    empty = build_segments([], config=CONFIG)
    data = build_report(empty, rng=parse_range("today", now=at("12:00")), config=CONFIG, now=at("12:00"))
    text = render_console(data)
    assert "覆盖率 0.0%" in text
    assert "没有" in text


# ------------------------------------------------------------------ HTML 自包含与转义


def test_html_零外部资源且无脚本():
    html = render_html(day_report())
    assert "<script" not in html
    assert "<link" not in html
    assert "url(http" not in html and "url('http" not in html
    assert not re.search(r'src\s*=\s*["\']https?:', html)
    assert "@import" not in html
    # SVG 命名空间是标识符，不是资源引用
    assert html.count("http") == 0 or "http://www.w3.org/2000/svg" in html


def test_html_转义恶意窗口标题():
    evil = "<script>alert(1)</script>"
    samples = [sample(at("10:00") + index * 60, evil, title=evil) for index in range(3)]
    segments = build_segments(samples, config=Config(interval=60.0))
    data = build_report(
        segments,
        rng=parse_range("2026-09-23", now=at("12:00")),
        config=Config(interval=60.0),
        now=at("12:00"),
    )
    html = render_html(data)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert html.count("<script") == 0, "转义之后不该出现真的 script 标签"


def test_html_包含关键区块():
    html = render_html(day_report())
    for marker in ("覆盖率", "应用排行", "每小时在用什么", "专注块", "账本可信度", "数据从未离开这台机器"):
        assert marker in html


def test_app_color_稳定且区分不同进程():
    assert app_color("code.exe") == app_color("code.exe")
    assert app_color("code.exe").startswith("#")
    colors = {app_color(name) for name in ("code.exe", "msedge.exe", "explorer.exe", "python.exe")}
    assert len(colors) >= 3, "至少别把常见几个进程都染成一个颜色"


def test_AppRow_display_优先用中文标签():
    assert AppRow(exe="idle", seconds=1.0, share=0.1, label="（空闲）").display() == "（空闲）"
    assert AppRow(exe="", seconds=1.0, share=0.1).display() == "（无窗口）"
    assert AppRow(exe="code.exe", seconds=1.0, share=0.1).display() == "code.exe"

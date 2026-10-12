"""report：三种输出数字同源、区间裁剪、可信度提示、HTML 自包含与转义。"""

from __future__ import annotations

import json
import re

import pytest

from builders import at, sample
from timetrace.config import Config
from timetrace.domain import KIND_APP, KIND_GAP, KIND_PAUSED, Pause, Range
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
    # 账本守恒：所有行加起来 == 覆盖率 + 未记录占比（每一段都有归属，没有"其它"黑盒）。
    gap_share = data.totals[KIND_GAP] / data.range_seconds
    assert sum(row.share for row in app_rows + specials) == pytest.approx(data.coverage + gap_share, abs=1e-6)
    # 覆盖率只数"真的采样到"的部分：覆盖率 + 未记录占比 == 100%。
    assert data.coverage + gap_share == pytest.approx(1.0, abs=1e-6)
    assert app_rows[0].exe == "code.exe", "最长时间的应用应该排第一"
    assert app_rows[0].share == pytest.approx(app_rows[0].seconds / data.range_seconds)


def test_覆盖率不把未记录算进去():
    """§10 的定义：覆盖率 = (app+idle+locked+paused)/区间总长。

    曾经把 gap 也算进分子 —— 于是"休眠一整天"会显示 100% 覆盖。那是这个工具
    最不该犯的谎：未记录恰恰证明那段**没有**被覆盖。
    """
    samples = [sample(at(f"09:{minute:02d}"), "code.exe") for minute in range(5)]
    samples += [sample(at(f"10:{minute:02d}"), "msedge.exe") for minute in range(5)]
    config = Config(interval=60.0, idle_threshold=300.0, focus_min=10.0)
    segments = build_segments(samples, config=config)
    data = build_report(
        segments, rng=Range(at("09:00"), at("10:05"), "70 分钟"), config=config, now=at("10:05")
    )
    assert data.totals[KIND_GAP] > 3000, "中间 40 分钟没采样，应该是未记录"
    assert data.coverage == pytest.approx(540.0 / 3900.0, abs=1e-3)
    assert data.coverage < 0.2
    assert data.covered_seconds == pytest.approx(540.0, abs=1), "covered 不含 gap"


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


# ------------------------------------------------------------------ 每日明细


def multi_day_report(exes=("code.exe", "msedge.exe", "code.exe")):
    """三天、每天上午十一次采样：应用 10 分钟，其余时间 tracker 没在跑（断档）。"""
    days = ("2026-09-21", "2026-09-22", "2026-09-23")
    samples = []
    for day, exe in zip(days, exes, strict=True):
        samples += [sample(at(f"09:{minute:02d}", day), exe) for minute in range(11)]
    config = Config(interval=60.0, idle_threshold=300.0, focus_min=10.0)
    segments = build_segments(samples, config=config)
    rng = Range(at("00:00", days[0]), at("12:00", days[-1]), "三天")
    return build_report(segments, rng=rng, config=config, now=at("12:00", days[-1]))


def test_每日明细把账分到每一天():
    data = multi_day_report()
    assert [row.day.isoformat() for row in data.daily] == ["2026-09-21", "2026-09-22", "2026-09-23"]
    # 前两天的最后一分钟被"断档"吃掉（下一采样在 23 小时后），最后一天没有后续采样，
    # 所以按 interval 估算出 11 分钟 —— 这是采样法的既定口径，不是分账算错。
    assert [row.app_seconds for row in data.daily] == [600.0, 600.0, 660.0]
    # 每天的 span 加起来必须正好等于区间总长 —— 不能凭空多出或少掉一天。
    assert sum(row.span for row in data.daily) == pytest.approx(data.range_seconds)


def test_每日守恒_每个桶之和等于当天被记录的时长():
    data = multi_day_report()
    for row in data.daily:
        assert sum(row.kind_seconds.values()) == pytest.approx(row.covered, abs=1e-6)
        # 覆盖率只算被采样的部分（不含「未记录」），与整体报告同一口径。
        assert row.coverage == pytest.approx((row.covered - row.totals.get(KIND_GAP, 0.0)) / row.span)
    # 所有天的应用时长之和 == 整个报告的应用时长（口径一致，不是两套数字）。
    assert sum(row.app_seconds for row in data.daily) == pytest.approx(data.totals[KIND_APP])


def test_跨午夜的段按本地自然日切开():
    first, second = "2026-09-21", "2026-09-22"
    samples = [
        sample(at("23:50", first), "code.exe"),
        sample(at("00:05", second), "code.exe"),
        sample(at("00:20", second), "code.exe"),
    ]
    config = Config(interval=900.0, idle_threshold=300.0, focus_min=10.0)
    segments = build_segments(samples, config=config)
    rng = Range(at("23:50", first), at("00:20", second) + 900.0, "跨天")
    data = build_report(segments, rng=rng, config=config, now=at("00:35", second))
    by_day = {row.day.isoformat(): row.app_seconds for row in data.daily}
    assert by_day[first] == pytest.approx(600.0), "23:50→00:05 这段有 10 分钟属于前一天"
    assert by_day[second] == pytest.approx(2100.0)


def test_切换次数落在真正切换发生的那一天():
    """跨天的 code→edge 中间隔着断档，按 app_runs 口径仍算一次切换，落在 edge 那天。"""
    data = multi_day_report(exes=("code.exe", "msedge.exe", "code.exe"))
    assert data.daily[0].switches == 0, "第一天只用了 code.exe"
    assert data.daily[1].switches == 1, "第二天开头从 code 切到 edge"
    assert data.daily[2].switches == 1, "第三天又从 edge 切回 code"
    assert sum(row.switches for row in data.daily) == data.switches


def test_逐日切换之和永远等于报告总切换数():
    """守恒不只管时长：切换次数也必须能逐日对上总数。"""
    cases = (
        ("code.exe",) * 3,
        ("code.exe", "msedge.exe", "explorer.exe"),
        ("msedge.exe", "code.exe", "msedge.exe"),
    )
    for exes in cases:
        data = multi_day_report(exes=exes)
        assert sum(row.switches for row in data.daily) == data.switches


def test_头号应用是当天用得最多的那个():
    data = multi_day_report(exes=("code.exe", "msedge.exe", "code.exe"))
    assert data.daily[0].top_app == ("code.exe", pytest.approx(600.0))
    assert data.daily[1].top_app[0] == "msedge.exe"
    empty = build_report(
        build_segments([], config=CONFIG),
        rng=parse_range("today", now=at("12:00")),
        config=CONFIG,
        now=at("12:00"),
    )
    assert empty.daily[0].top_app is None


def test_json_里有每日明细且结构稳定():
    payload = json.loads(render_json(multi_day_report()))
    assert "daily" in payload
    assert [row["day"] for row in payload["daily"]] == ["2026-09-21", "2026-09-22", "2026-09-23"]
    first = payload["daily"][0]
    assert set(first) == {"day", "span", "covered", "coverage", "totals", "apps", "switches"}
    assert first["apps"][0]["exe"] == "code.exe"
    assert first["totals"][KIND_APP] == pytest.approx(600.0)


def test_console_只在多天区间列每日明细():
    assert "每日明细" in render_console(multi_day_report())
    assert "每日明细" not in render_console(day_report()), "单日报告已经有小时图，别重复"
    assert "周一" in render_console(multi_day_report())


def test_console_长区间只列最近若干天并说明():
    days = [f"2026-{month:02d}-{day:02d}" for month in (1, 2) for day in range(1, 29)]
    samples = [sample(at("09:00", day), "code.exe") for day in days]
    config = Config(interval=60.0)
    segments = build_segments(samples, config=config)
    rng = Range(at("00:00", days[0]), at("12:00", days[-1]), "很久")
    data = build_report(segments, rng=rng, config=config, now=at("12:00", days[-1]))
    text = render_console(data)
    assert "只列最近 31 天" in text
    assert text.count("周") >= 31


def test_html_多天区间有每日趋势_单日没有():
    multi = render_html(multi_day_report())
    assert "每日趋势" in multi
    assert "daily-table" in multi
    assert "09-21" in multi, "每日表格用 MM-DD 呈现日期"
    single = render_html(day_report())
    assert "每日趋势" not in single


def test_html_每日趋势依然是零外链且转义():
    html = render_html(multi_day_report())
    assert "<script" not in html
    assert "<link" not in html
    assert "url(http" not in html
    assert "aria-label=" in html


def test_区间里没采样的时间落进未记录而不是凭空消失():
    """§10：早于首条采样、晚于末条采样的时间计入「未记录」。

    少了这一步，`report --range month` 只跑了三天时，剩下 27 天会凭空消失 ——
    所有桶加起来不到区间总长，"每一项加起来必须等于区间总长"就成了空话。
    """
    samples = [sample(at(f"09:{minute:02d}"), "code.exe") for minute in range(5)]
    config = Config(interval=60.0, idle_threshold=300.0, focus_min=10.0)
    segments = build_segments(samples, config=config)
    data = build_report(segments, rng=Range(at("09:00"), at("12:00"), "上午"), config=config, now=at("12:00"))
    assert data.range_seconds == pytest.approx(10800.0)
    assert data.totals[KIND_GAP] == pytest.approx(10500.0), "10:00 之后没采样，那 3 小时是未记录"
    assert sum(data.totals.values()) == pytest.approx(data.range_seconds, abs=1e-6)
    assert data.coverage == pytest.approx(300.0 / 10800.0)


def test_完全没采样的区间整段是未记录():
    empty = build_segments([], config=CONFIG)
    data = build_report(empty, rng=Range(at("09:00"), at("10:00"), "一小时"), config=CONFIG, now=at("10:00"))
    assert data.totals[KIND_GAP] == pytest.approx(3600.0)
    assert data.coverage == 0.0
    assert sum(data.totals.values()) == pytest.approx(data.range_seconds, abs=1e-6)


def test_补洞不会把已有的段挤掉():
    """补洞只填空隙，绝不覆盖 app / idle / locked / paused。"""
    samples = [sample(at(f"09:{minute:02d}"), "code.exe") for minute in range(10)]
    samples += [sample(at(f"09:{minute:02d}"), "msedge.exe") for minute in range(20, 25)]
    config = Config(interval=60.0, idle_threshold=300.0, focus_min=10.0)
    segments = build_segments(samples, config=config)
    data = build_report(
        segments, rng=Range(at("09:00"), at("10:00"), "一小时"), config=config, now=at("10:00")
    )
    assert data.totals[KIND_APP] > 0, "两簇应用时间都得留下"
    assert data.totals[KIND_GAP] > 0, "两簇之间的 15 分钟是未记录"
    assert sum(data.totals.values()) == pytest.approx(data.range_seconds, abs=1e-6)
    # 应用时长应该就是两簇采样的 9+4 分钟（最后一分钟被断档吃掉）
    assert data.totals[KIND_APP] == pytest.approx(9 * 60 + 4 * 60, abs=60)


# ------------------------------------------------------------------ 目标对比


def goal_report(per_day_apps=("code.exe",) * 3, *, goals):
    """三天、每天上午若干分钟，配一组目标。"""
    days = ("2026-09-21", "2026-09-22", "2026-09-23")
    samples = []
    for day, exe in zip(days, per_day_apps, strict=True):
        # 每天 20 分钟，三天约 1 小时 —— 便于用整数小时当目标做断言。
        samples += [sample(at(f"09:{minute:02d}", day), exe) for minute in range(20)]
    config = Config(interval=60.0, idle_threshold=300.0, focus_min=10.0, goals=goals)
    segments = build_segments(samples, config=config)
    rng = Range(at("00:00", days[0]), at("23:59", days[-1]), "三天")
    return build_report(segments, rng=rng, config=config, now=at("23:59", days[-1]))


def test_没配目标就不出现目标区():
    """绝不替用户发明目标。"""
    data = day_report()
    assert data.goals == []
    assert "目标对比" not in render_console(data)
    assert "目标对比" not in render_html(data)
    assert json.loads(render_json(data))["goals"] == []


def test_目标进度按实际应用时长算():
    data = goal_report(goals=(("code.exe", 2.0),))
    assert len(data.goals) == 1
    row = data.goals[0]
    assert row.exe == "code.exe"
    assert row.target_seconds == pytest.approx(2 * 3600.0)
    # 三天每天 20 条 60s 采样 ≈ 每天 19 分钟（最后一分钟被断档吃掉）
    assert row.actual_seconds == pytest.approx(3 * 19 * 60, abs=120)
    assert 0 < row.progress < 1
    assert row.remaining_seconds == pytest.approx(row.target_seconds - row.actual_seconds)
    assert row.over_seconds == 0.0


def test_超出目标时如实说超出():
    data = goal_report(goals=(("code.exe", 0.5),))
    row = data.goals[0]
    assert row.over_seconds > 0
    assert row.remaining_seconds == 0.0
    assert row.progress > 1.0
    assert "超出" in render_console(data)


def test_配了但没用到的目标显示为零():
    data = goal_report(goals=(("msedge.exe", 5.0),))
    row = data.goals[0]
    assert row.actual_seconds == 0.0
    assert row.progress == 0.0
    assert row.remaining_seconds == pytest.approx(5 * 3600.0)
    assert "还差 5h00m" in render_console(data)


def test_多个目标按进度排序且三态一致():
    """三个应用各占一天，目标不同 → 按进度排序，三态输出逐项对得上。"""
    data = goal_report(
        per_day_apps=("code.exe", "msedge.exe", "explorer.exe"),
        goals=(("code.exe", 1.0), ("msedge.exe", 2.0), ("explorer.exe", 0.5)),
    )
    # 最后一天的末条采样按 interval 多估一分钟，所以 explorer 是 20min 而非 19min：
    # explorer 20/30=67% > code 19/60=32% > msedge 19/120=16%
    assert [row.exe for row in data.goals] == ["explorer.exe", "code.exe", "msedge.exe"]
    assert [round(row.progress, 2) for row in data.goals] == [0.67, 0.32, 0.16]
    console = render_console(data)
    payload = json.loads(render_json(data))
    html = render_html(data)
    assert "目标对比" in console and "目标对比" in html
    assert payload["goals"][0]["exe"] == "explorer.exe"
    for row in data.goals:
        assert row.exe in console and row.exe in html
        assert format_duration(row.target_seconds) in console
    assert "不打分" in console and "不打分" in html


def test_目标区不破坏_html_零外链():
    html = render_html(goal_report(goals=(("code.exe", 2.0),)))
    assert "<script" not in html and "<link" not in html
    assert "url(http" not in html
    assert "goal-track" in html


def test_json_目标结构稳定():
    payload = json.loads(render_json(goal_report(goals=(("code.exe", 2.0),))))
    first = payload["goals"][0]
    assert set(first) == {
        "exe",
        "target_seconds",
        "actual_seconds",
        "progress",
        "remaining_seconds",
        "over_seconds",
    }
    assert first["target_seconds"] == pytest.approx(7200.0)


# ------------------------------------------------------------------ 窗口标题细分


def title_report(*, include_titles: bool, titles=("main.py", "main.py", "docs.md")):
    samples = [sample(at(f"09:{minute:02d}"), "code.exe", title=title) for minute, title in enumerate(titles)]
    config = Config(interval=60.0, idle_threshold=300.0, focus_min=10.0)
    segments = build_segments(samples, config=config)
    return build_report(
        segments,
        rng=Range(at("09:00"), at("10:00"), "一小时"),
        config=config,
        now=at("10:00"),
        include_titles=include_titles,
    )


def test_不请求标题时报告里没有标题区():
    data = title_report(include_titles=False)
    assert data.titles == []
    assert "按窗口标题" not in render_console(data)
    assert json.loads(render_json(data))["titles"] == []


def test_标题细分按时长聚合并与应用总时长守恒():
    data = title_report(include_titles=True)
    assert len(data.titles) == 2
    assert data.titles[0].exe == "code.exe"
    # main.py 占两条、docs.md 占一条
    assert data.titles[0].seconds == pytest.approx(2 * data.titles[1].seconds, abs=1)
    assert sum(row.seconds for row in data.titles) == pytest.approx(data.totals[KIND_APP], abs=1e-6), (
        "标题细分之和必须等于应用总时长 —— 不能凭空多出或少掉一秒"
    )
    assert sum(row.share for row in data.titles) == pytest.approx(
        data.totals[KIND_APP] / data.range_seconds, abs=1e-6
    )


def test_标题细分三态同源():
    data = title_report(include_titles=True)
    console = render_console(data)
    payload = json.loads(render_json(data))
    html = render_html(data)
    assert "按窗口标题" in console and "按窗口标题" in html
    assert len(payload["titles"]) == 2
    for row in data.titles:
        assert row.title in console and row.title in html
        assert format_duration(row.seconds) in console
        assert row.title in {item["title"] for item in payload["titles"]}


def test_标题细分不再数采样条数():
    """旧实现 console 里显示的是"采样条数"，与报告其它列（秒）不是一个口径。"""
    data = title_report(include_titles=True)
    console = render_console(data)
    assert "采样条数" not in console, "口径必须统一成时长"
    assert "按窗口标题" in console


def test_标题细分长列表在_console_截断但_json_完整():
    titles = [f"文件{index}.py" for index in range(40)]
    data = title_report(include_titles=True, titles=titles)
    console = render_console(data)
    payload = json.loads(render_json(data))
    assert len(payload["titles"]) == 40, "JSON 必须完整"
    assert "还有 25 个标题" in console, "console 只列 15 个，并明说还剩多少"
    assert "按窗口标题" in render_html(data)


def test_标题为空时显示无标题而不是丢掉():
    data = title_report(include_titles=True, titles=("", "", ""))
    assert len(data.titles) == 1
    assert data.titles[0].title == ""
    assert "（无标题）" in render_console(data)
    assert "（无标题）" in render_html(data)


def test_标题细分不破坏_html_零外链():
    html = render_html(title_report(include_titles=True, titles=("<script>x</script>",) * 3))
    assert "<script" not in html
    assert "&lt;script&gt;" in html
    assert "<link" not in html


# ------------------------------------------------------------------ 被排除的进程（§9.8）


def excluded_report():
    samples = [sample(at(f"09:{minute:02d}"), "code.exe") for minute in range(20)]
    samples += [sample(at(f"09:{minute:02d}"), "noise.exe") for minute in range(20, 40)]
    config = Config(interval=60.0, idle_threshold=300.0, focus_min=10.0, exclude=("noise.exe",))
    segments = build_segments(samples, config=config, excluded=config.exclude)
    return build_report(
        segments,
        rng=Range(at("09:00"), at("10:00"), "一小时"),
        config=config,
        now=at("10:00"),
    )


def test_被排除的进程不计入排名但单独列出():
    """§9.8：--exclude 的进程归入 app 却不排名，且必须在报告里单独列出。"""
    data = excluded_report()
    ranked = [row for row in data.apps if not row.excluded and row.label == ""]
    assert [row.exe for row in ranked] == ["code.exe"]
    excluded = [row for row in data.apps if row.excluded and row.label == ""]
    assert [row.exe for row in excluded] == ["noise.exe"]


def test_被排除的进程在_console_里单独成段():
    text = render_console(excluded_report())
    assert "被排除的进程" in text
    assert "noise.exe" in text
    assert "不计入上面的排名" in text
    # 排名表里不该出现 noise.exe，但它必须在别处出现
    ranking = text.split("被排除的进程")[0]
    assert "noise.exe" not in ranking


def test_被排除的进程在_html_里单独成节():
    html = render_html(excluded_report())
    assert "被排除的进程" in html
    assert "不参与排名" in html
    ranking = html.split("被排除的进程")[0]
    assert "noise.exe" not in ranking.split("应用排行")[-1]


def test_被排除的时间照样入账_守恒不破():
    """排除只是不排名，不是把时间从账本上抹掉。"""
    data = excluded_report()
    excluded_seconds = sum(row.seconds for row in data.apps if row.excluded and row.label == "")
    assert excluded_seconds == pytest.approx(1200.0, abs=1)
    assert data.totals[KIND_APP] == pytest.approx(1200.0 + 1200.0, abs=1)
    assert sum(row.share for row in data.apps) == pytest.approx(1.0, abs=1e-6)


def test_json_里被排除的行带标志():
    payload = json.loads(render_json(excluded_report()))
    rows = {item["exe"]: item for item in payload["apps"]}
    assert rows["noise.exe"]["excluded"] is True
    assert rows["code.exe"]["excluded"] is False


def test_没有排除项时不出现那一节():
    samples = [sample(at(f"09:{minute:02d}"), "code.exe") for minute in range(10)]
    config = Config(interval=60.0, idle_threshold=300.0, focus_min=10.0)
    segments = build_segments(samples, config=config)
    data = build_report(
        segments,
        rng=Range(at("09:00"), at("10:00"), "一小时"),
        config=config,
        now=at("10:00"),
    )
    assert "被排除的进程" not in render_console(data)
    assert "被排除的进程" not in render_html(data)


# ------------------------------------------------------------------ 规则提醒（连续没离开）


def stretch_config(minutes: float) -> Config:
    return Config(interval=60.0, idle_threshold=300.0, focus_min=10.0, max_stretch_minutes=minutes)


def test_没配规则就不出现提醒区():
    """规则跟目标一样：不配就绝不出现，免得工具替用户决定什么算太久。"""
    samples = [sample(at(f"09:{minute:02d}"), "code.exe") for minute in range(120)]
    config = stretch_config(0.0)
    segments = build_segments(samples, config=config)
    data = build_report(
        segments, rng=Range(at("09:00"), at("11:00"), "两小时"), config=config, now=at("11:00")
    )
    assert data.stretches == []
    assert "连续没离开" not in render_console(data)
    assert "连续没离开" not in render_html(data)
    assert json.loads(render_json(data))["stretches"] == []


def test_超过阈值的时段被找出来():
    samples = [sample(at(f"09:{minute:02d}"), "code.exe") for minute in range(120)]
    config = stretch_config(90.0)
    segments = build_segments(samples, config=config)
    data = build_report(
        segments, rng=Range(at("09:00"), at("11:00"), "两小时"), config=config, now=at("11:00")
    )
    assert len(data.stretches) == 1
    row = data.stretches[0]
    assert row.seconds >= 90 * 60
    assert row.exes == ("code.exe",)
    assert "09:00" in row.window() and "11:00" in row.window()


def test_换应用不算离开_但锁屏和空闲算():
    """连续没离开 = 中间可以换应用；空闲/锁屏/暂停/未记录都算离开了。"""
    samples = [sample(at("09:00") + index * 60, "code.exe") for index in range(45)]
    samples += [sample(at("09:45") + index * 60, "msedge.exe") for index in range(45)]
    config = stretch_config(60.0)
    segments = build_segments(samples, config=config)
    data = build_report(
        segments, rng=Range(at("09:00"), at("11:00"), "两小时"), config=config, now=at("11:00")
    )
    assert len(data.stretches) == 1, "换了应用但人没走，应该还是一段"
    assert data.stretches[0].exes == ("code.exe", "msedge.exe")
    assert data.stretches[0].seconds >= 60 * 60


def test_中间断档把一段劈成两段():
    """中间隔了超过 gap_limit 的时间 → 两段，各自判断是否够长。"""
    # 每簇 70 分钟 > 60 分钟阈值；中间隔 90 分钟（远超 gap_limit=180s）
    samples = [sample(at("09:00") + index * 60, "code.exe") for index in range(70)]
    samples += [sample(at("12:00") + index * 60, "code.exe") for index in range(70)]
    config = stretch_config(60.0)
    segments = build_segments(samples, config=config)
    data = build_report(
        segments, rng=Range(at("09:00"), at("15:00"), "六小时"), config=config, now=at("15:00")
    )
    assert len(data.stretches) == 2, "中间断档必须把时段劈开"
    for row in data.stretches:
        assert row.seconds >= 60 * 60
    starts = sorted(row.start for row in data.stretches)
    assert starts[0] == pytest.approx(at("09:00"))
    assert starts[1] == pytest.approx(at("12:00"))


def test_不足阈值的时段不报():
    samples = [sample(at("09:00") + index * 60, "code.exe") for index in range(30)]
    config = stretch_config(90.0)
    segments = build_segments(samples, config=config)
    data = build_report(
        segments, rng=Range(at("09:00"), at("11:00"), "两小时"), config=config, now=at("11:00")
    )
    assert data.stretches == [], "30 分钟不该触发 90 分钟的规则"


def test_提醒区三态同源():
    samples = [sample(at("09:00") + index * 60, "code.exe") for index in range(120)]
    config = stretch_config(90.0)
    segments = build_segments(samples, config=config)
    data = build_report(
        segments, rng=Range(at("09:00"), at("11:00"), "两小时"), config=config, now=at("11:00")
    )
    console = render_console(data)
    payload = json.loads(render_json(data))
    html = render_html(data)
    assert "连续没离开" in console and "连续没离开" in html
    assert len(payload["stretches"]) == 1
    assert payload["stretches"][0]["apps"] == ["code.exe"]
    assert payload["stretches"][0]["seconds"] == pytest.approx(data.stretches[0].seconds, abs=1)
    assert "只报事实" in console and "只报事实" in html
    assert "90m" in console or "1h30m" in console


def test_提醒区不破坏_html_零外链():
    samples = [sample(at("09:00") + index * 60, "<b>x</b>") for index in range(120)]
    config = stretch_config(90.0)
    segments = build_segments(samples, config=config)
    data = build_report(
        segments, rng=Range(at("09:00"), at("11:00"), "两小时"), config=config, now=at("11:00")
    )
    html = render_html(data)
    assert "<script" not in html and "<link" not in html
    assert "&lt;b&gt;" in html


# ------------------------------------------------------------------ 日历热力图（M1）


def calendar_report(days: int):
    """造 N 天数据，每天 09:00 起若干分钟。"""
    start_day = "2026-09-01"
    import datetime as dt

    base = dt.date.fromisoformat(start_day)
    samples = []
    minutes_by_day = [30 + (index % 4) * 30 for index in range(days)]
    for offset, minutes in enumerate(minutes_by_day):
        day = (base + dt.timedelta(days=offset)).isoformat()
        for minute in range(minutes):
            samples.append(sample(at(f"09:{minute:02d}", day), "code.exe"))
    config = Config(interval=60.0, idle_threshold=300.0, focus_min=10.0)
    segments = build_segments(samples, config=config)
    end = (base + dt.timedelta(days=days - 1)).isoformat()
    rng = Range(at("00:00", start_day), at("23:59", end), f"{days} 天")
    return build_report(segments, rng=rng, config=config, now=at("23:59", end))


def test_日历一格一天且覆盖整个区间():
    data = calendar_report(30)
    assert len(data.calendar) == 30
    assert data.calendar[0].day.isoformat() == "2026-09-01"
    assert data.calendar[-1].day.isoformat() == "2026-09-30"
    # 一格一天，且与 daily 逐日行一一对应
    assert [cell.day for cell in data.calendar] == [row.day for row in data.daily]


def test_日历按应用时长着色_不把未记录算进去():
    data = calendar_report(20)
    for cell, row in zip(data.calendar, data.daily, strict=True):
        assert cell.app_seconds == pytest.approx(row.app_seconds, abs=1e-6)
        assert cell.has_samples == (row.covered > 0)
        # 应用时长一定小于等于当天被记录的总时长（未记录/空闲不参与色阶）
        assert cell.app_seconds <= row.covered + 1e-6


def test_没有采样的那天标成无记录():
    """休眠一整天的格子必须和"有采样但没用到应用"区分开。"""
    start = "2026-09-01"
    samples = [sample(at(f"09:{m:02d}", start), "code.exe") for m in range(30)]
    config = Config(interval=60.0, idle_threshold=300.0, focus_min=10.0)
    segments = build_segments(samples, config=config)
    rng = Range(at("00:00", start), at("23:59", "2026-09-05"), "5 天")
    data = build_report(segments, rng=rng, config=config, now=at("23:59", "2026-09-05"))
    assert len(data.calendar) == 5
    assert data.calendar[0].has_samples is True
    assert all(not cell.has_samples for cell in data.calendar[1:])
    assert data.calendar[0].app_seconds > 0
    assert all(cell.app_seconds == 0 for cell in data.calendar[1:])


def test_短区间仍用堆叠柱_长区间才用日历():
    """两周以上日历才好读；更短的逐日堆叠更有信息量。"""
    short = calendar_report(10)
    long = calendar_report(30)
    short_html = render_html(short)
    long_html = render_html(long)
    assert "每日趋势" in short_html and "每日趋势" in long_html
    assert "热力图" not in short_html, "10 天不该出热力图"
    assert "热力图" in long_html, "30 天应该出热力图"
    assert "日历按周排列" in long_html
    # 两张图不互相替代：堆叠柱对 30 天照样有用（每天三桶占比），
    # 日历只负责"哪天忙哪天空白"这个模式。
    assert 'aria-label="每日应用时间堆叠图"' in long_html
    assert 'aria-label="每日应用时间堆叠图"' in short_html


def test_日历只在有足够天数时出现():
    one = day_report()  # 单日
    assert render_html(one).count("热力图") == 0


def test_日历_零外链且数据进_json():
    data = calendar_report(30)
    html = render_html(data)
    assert "<script" not in html and "<link" not in html
    assert "aria-label" in html
    payload = json.loads(render_json(data))
    assert len(payload["calendar"]) == 30
    first = payload["calendar"][0]
    assert set(first) == {"day", "app_seconds", "has_samples"}
    assert first["day"] == "2026-09-01"
    assert first["has_samples"] is True


def test_热力图_titel_里给绝对数值():
    """颜色只负责相对比较，绝对数字必须在 tooltip 里。"""
    html = render_html(calendar_report(30))
    assert "2026-09-01 · 应用" in html

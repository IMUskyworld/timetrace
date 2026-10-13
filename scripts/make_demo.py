#!/usr/bin/env python
"""生成确定性演示数据并渲染演示报告（默认输出 docs/demo.html）。

为什么需要它：

- README 的截图与 ``docs/demo.html`` 必须由工具的**真实产出**生成，而不是手写 HTML ——
  否则"演示"本身就是一种虚报，正是这个项目反对的东西；
- 任何人想看看报告长什么样，不必先记录自己几周的数据；
- ``tests/test_demo.py`` 钉住它的契约：区块全点亮、账本守恒、两次生成字节级一致、
  入库的 ``docs/demo.html`` 与生成器保持同步。

用法::

    python scripts/make_demo.py                       # 渲染 docs/demo.html，数据落临时目录
    python scripts/make_demo.py --out DIR --html FILE # 指定数据目录与输出文件

设计约束：

- **确定性**：固定随机种子 + 固定日期区间（不依赖"今天"），除 ``generated_at``
  外两次生成字节一致；
- **只写 ``--out`` 指定的目录**（默认系统临时目录），不碰真实数据目录；
- 合成数据里的进程名与窗口标题全是编的，不含任何真实使用记录。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from timetrace.cli import main as cli_main
from timetrace.domain import SAMPLE_PAUSED, Sample
from timetrace.store import Store

DEMO_SEED = 20260913
# 三个完整周（周一→周日）：堆叠柱、日历热力图（>15 天才出现）、小时图三张视觉全点亮。
DEMO_RANGE = "2026-09-28..2026-10-18"
INTERVAL = 10.0
# 配置 deliberately 点亮三个可选区块：目标对比 / 规则提醒 / 排除项。
DEMO_CONFIG = {
    "interval": INTERVAL,
    "goals": {"code.exe": 100, "msedge.exe": 30},
    "max_stretch_minutes": 90,
    "exclude": ["steam.exe"],
}

# 演示报告里的内联 SVG → 独立资产文件名（按 aria-label 认领，不靠出现顺序）。
SVG_ASSETS = {
    "每日应用时间堆叠图": "demo-daily.svg",
    "按周排列的每日应用时长热力图": "demo-calendar.svg",
    "每小时应用时间": "demo-hourly.svg",
}
_SVG_RE = re.compile(r'<svg\b[^>]*aria-label="([^"]+)"[^>]*>.*?</svg>', re.S)
_VIEWBOX_RE = re.compile(r'viewBox="0 0 (\d+) (\d+)"')

TITLES = {
    "code.exe": [
        "main.py - timetrace - Visual Studio Code",
        "report.py - timetrace - Visual Studio Code",
        "segment.py - timetrace - Visual Studio Code",
    ],
    "msedge.exe": [
        "PDCA 循环_百度搜索",
        "timetrace · Issues - GitHub",
        "内蒙古大学教务系统",
    ],
    "chatgpt.exe": ["ChatGPT", "帮我看下这段守恒测试"],
    "explorer.exe": ["资料库", "下载"],
    "steam.exe": ["Steam"],
    "lockapp.exe": [""],
}


@dataclass(frozen=True)
class Block:
    """一天里的一段行为。

    mode：active=在用 / idle=人走了窗口开着 / locked=锁屏 / paused=明确暂停 /
    hole=没采样（休眠，那段记进「未记录」）。
    """

    start: str
    end: str
    exe: str
    mode: str = "active"


WEEKDAY_BLOCKS = [
    Block("08:55", "09:00", "explorer.exe"),
    Block("09:00", "10:10", "code.exe"),
    Block("10:10", "10:18", "msedge.exe"),
    Block("10:18", "11:30", "code.exe"),
    Block("11:30", "12:00", "msedge.exe"),
    Block("12:00", "13:00", "msedge.exe", mode="idle"),  # 午饭：窗口开着，人走了
    Block("13:00", "13:15", "chatgpt.exe"),
    Block("13:15", "14:40", "code.exe"),
    Block("14:40", "15:00", "explorer.exe"),
    Block("15:00", "15:40", "lockapp.exe", mode="locked"),  # 锁屏
    Block("15:40", "17:20", "code.exe"),
    Block("17:20", "17:30", "steam.exe"),  # 被排除的进程也有时间，报告里单独列出
]

SATURDAY_BLOCKS = [
    Block("10:30", "11:30", "msedge.exe"),
    Block("11:30", "12:15", "chatgpt.exe"),
    Block("14:00", "15:30", "msedge.exe"),
    Block("15:30", "16:00", "explorer.exe"),
]

SUNDAY_BLOCKS = [Block("11:00", "11:05", "explorer.exe")]  # 周日几乎没开电脑

# 周一（第 0 天）上午的"周会"：明确暂停半小时，报告里单独成桶。
MEETING = (Block("09:50", "10:20", "code.exe", mode="paused"), "周会")
# 周四（第 3 天）下午"睡着"了一小时：没有任何采样 → 未记录，不摊给任何应用。
SLEEP_HOLE = Block("15:00", "15:40", "code.exe", mode="hole")


def at(clock: str, day: str) -> float:
    """本地时间字符串 → epoch 秒（与 tests/builders.py 同一约定）。"""
    hour, minute = (int(piece) for piece in clock.split(":"))
    year, month, mday = (int(piece) for piece in day.split("-"))
    return time.mktime((year, month, mday, hour, minute, 0, 0, 0, -1))


def _day_plan(day_index: int, day_date: dt.date) -> list[Block]:
    weekday = day_date.weekday()
    if weekday == 6:
        return list(SUNDAY_BLOCKS)
    if weekday == 5:
        return list(SATURDAY_BLOCKS)
    blocks = list(WEEKDAY_BLOCKS)
    if day_index == 0:
        blocks = [
            Block("09:00", "09:50", "code.exe"),
            MEETING[0],
            Block("10:20", "10:35", "msedge.exe"),
            Block("10:35", "11:30", "code.exe"),
            *blocks[4:],
        ]
    if day_index == 3:
        blocks = [SLEEP_HOLE if block.start == "15:00" else block for block in blocks]
    return blocks


def _emit(samples: list[Sample], day: str, block: Block, rng: random.Random) -> None:
    if block.mode == "hole":
        return
    start = at(block.start, day)
    end = at(block.end, day)
    titles = TITLES.get(block.exe, [""])
    ts = start
    index = 0
    while ts < end:
        if block.mode == "idle":
            # 人走了多久，Windows 就会报多大的空闲秒数 —— 回填靠的就是这个数。
            idle = ts - start
        elif block.mode == "locked":
            idle = float(rng.choice([0, 1, 2]))
        else:
            idle = float(rng.choice([0, 0, 0, 1, 2, 3, 7]))
        kind = SAMPLE_PAUSED if block.mode == "paused" else "active"
        samples.append(
            Sample(
                ts=ts,
                pid=1000 + (index % 50),
                exe=block.exe,
                title=titles[index % len(titles)],
                idle_s=idle,
                kind=kind,
            )
        )
        ts += INTERVAL
        index += 1


def generate(store: Store) -> int:
    """把三周合成数据写进 store，返回采样条数。"""
    rng = random.Random(DEMO_SEED)
    first_day = DEMO_RANGE.split("..")[0]
    year, month, mday = (int(piece) for piece in first_day.split("-"))
    start_date = dt.date(year, month, mday)
    samples: list[Sample] = []
    for day_index in range(21):
        day_date = start_date + dt.timedelta(days=day_index)
        day = day_date.isoformat()
        for block in _day_plan(day_index, day_date):
            _emit(samples, day, block, rng)
    written = store.insert_samples(samples)
    # 暂停区间单独记（真实 tracker 也是这样：采样行照写，区间另有其表）。
    meeting_start = at(MEETING[0].start, (start_date).isoformat())
    meeting_end = at(MEETING[0].end, (start_date).isoformat())
    store.open_pause(meeting_start, note=MEETING[1])
    store.close_pause(meeting_end)
    return written


def _standalone_svg(svg: str) -> str:
    """把内联 SVG 改成可独立渲染的版本。

    内联 SVG 靠页面的 `svg { width:100% }` CSS 伸缩，独立文件必须自带尺寸；
    xmlns 也必须补，否则浏览器和 README 的 <img> 都不认。
    热力图格子小（原始 144px 宽），放大两倍在 README 里才看得清。
    """
    head = svg.split(">", 1)[0]
    if "xmlns=" not in head:
        svg = svg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    match = _VIEWBOX_RE.search(svg.split(">", 1)[0])
    if match is None:
        return svg
    width, height = int(match.group(1)), int(match.group(2))
    scale = 2 if "calendar" in svg.split(">", 1)[0] else 1
    target = f'width="{width * scale}" height="{height * scale}"'
    head = svg.split(">", 1)[0]
    if re.search(r'width="\d+"', head):
        # 模板给日历写死了尺寸：改写它（rect 上的小数宽度不匹配 \d+，不会误伤）
        svg = re.sub(r'width="\d+" height="\d+"', target, svg, count=1)
    else:
        svg = svg.replace("<svg", f"<svg {target}", 1)
    return svg


def _extract_svgs(html: str, assets_dir: Path) -> list[Path]:
    """把演示报告里的内联 SVG 抽成独立文件。

    抽出来的必须是工具的**真实产出**（不是另画的图）。按 aria-label 认领文件名。
    """
    assets_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for match in _SVG_RE.finditer(html):
        label, svg = match.group(1), match.group(0)
        name = SVG_ASSETS.get(label)
        if name is None:
            continue
        path = assets_dir / name
        path.write_text(_standalone_svg(svg), encoding="utf-8")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成确定性演示数据并渲染演示 HTML 报告")
    parser.add_argument("--out", help="数据目录（默认系统临时目录，避免污染仓库）")
    parser.add_argument("--html", help="输出的 HTML 文件（默认 <repo>/docs/demo.html）")
    parser.add_argument("--assets", help="SVG 资产输出目录（默认与 --html 同目录）")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    html_path = Path(args.html) if args.html else repo_root / "docs" / "demo.html"
    assets_dir = Path(args.assets) if args.assets else html_path.parent
    if args.out:
        home = Path(args.out)
        home.mkdir(parents=True, exist_ok=True)
    else:
        home = Path(tempfile.mkdtemp(prefix="timetrace-demo-"))

    store = Store(home / "timetrace.db").initialize()
    try:
        written = generate(store)
    finally:
        store.close()
    (home / "config.json").write_text(json.dumps(DEMO_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")

    html_path.parent.mkdir(parents=True, exist_ok=True)
    code = cli_main(
        [
            "report",
            "--range",
            DEMO_RANGE,
            "--html",
            "--titles",
            "--out",
            str(html_path),
            "--home",
            str(home),
        ]
    )
    if code != 0:
        return code
    svgs = _extract_svgs(html_path.read_text(encoding="utf-8"), assets_dir)
    print(f"演示数据 {written} 条 → {home}")
    print(f"演示报告 → {html_path}")
    for path in svgs:
        print(f"图表资产 → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

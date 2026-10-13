"""演示数据契约：docs/demo.html 是项目的"脸"，不能歪，也不能悄悄失真。

`scripts/make_demo.py` 是 README 截图与 `docs/demo.html` 的唯一来源。它生成的数据必须：

1. **点亮报告的每一个区块** —— 否则截图就是骗人的（这个项目最恨的事）；
2. **守恒** —— 否则演示在教用户错误的东西；
3. **确定** —— 否则两次生成的 demo 不一样，diff 全是噪音；
4. **与入库的 docs/demo.html 保持同步** —— 否则报告层一改，demo 就烂在仓库里。
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMITTED_DEMO = ROOT / "docs" / "demo.html"

# HTML 里唯一每次生成都不一样的东西：页头那行"生成于 …"。归一化掉再比对。
_GENERATED_AT = re.compile(r"· 生成于 [^<]*")


def _load_make_demo():
    """scripts/ 不在 pythonpath 里，按文件路径加载模块本身。

    必须先注册进 sys.modules 再 exec_module：模块里有 @dataclass，
    dataclasses 解析 KW_ONLY 时要按 __module__ 去 sys.modules 里找命名空间，
    没注册会直接 AttributeError。
    """
    spec = importlib.util.spec_from_file_location("make_demo", ROOT / "scripts" / "make_demo.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_demo"] = module
    spec.loader.exec_module(module)
    return module


make_demo = _load_make_demo()


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, cwd=ROOT)


@pytest.fixture(scope="module")
def demo_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """生成一次演示（数据 + HTML + JSON 报告），模块内共用 —— 生成要几秒，别反复跑。"""
    home = tmp_path_factory.mktemp("demo_home")
    out_dir = tmp_path_factory.mktemp("demo_out")
    html_path = out_dir / "demo.html"
    gen = _run(
        [
            sys.executable,
            str(ROOT / "scripts" / "make_demo.py"),
            "--out",
            str(home),
            "--html",
            str(html_path),
        ]
    )
    assert gen.returncode == 0, f"make_demo.py 失败：\n{gen.stdout}\n{gen.stderr}"
    report = _run(
        [
            sys.executable,
            str(ROOT / "scripts" / "timetrace_dev.py"),
            "report",
            "--range",
            make_demo.DEMO_RANGE,
            "--json",
            "--titles",
            "--home",
            str(home),
        ]
    )
    assert report.returncode == 0, f"report --json 失败：\n{report.stdout}\n{report.stderr}"
    return {
        "home": home,
        "html_path": html_path,
        "html": html_path.read_text(encoding="utf-8"),
        "data": json.loads(report.stdout),
    }


# ------------------------------------------------------------------ 区块点亮


def test_演示数据点亮报告的每个区块(demo_run: dict[str, object]):
    """少任何一个区块，README 的截图就在撒谎。"""
    data = demo_run["data"]
    assert isinstance(data, dict)

    # 应用行：四个常客 + 排除项
    app_names = {row["exe"] for row in data["apps"] if not row["excluded"]}
    assert {"code.exe", "msedge.exe", "explorer.exe", "chatgpt.exe"} <= app_names
    excluded = {row["exe"] for row in data["apps"] if row["excluded"] and row["exe"].endswith(".exe")}
    assert "steam.exe" in excluded, "配置里排除了 steam.exe，演示数据里却没有它的时间"

    # 四个专用桶：空闲（午饭回填）/ 锁屏 / 暂停（周会）/ 未记录（过夜 + 休眠）
    totals = data["totals"]
    for kind in ("idle", "locked", "paused", "gap"):
        assert totals[kind] > 0, f"演示数据没有点亮「{kind}」桶"

    # 目标对比：两个目标都该有进度
    goals = {row["exe"]: row for row in data["goals"]}
    assert set(goals) == {"code.exe", "msedge.exe"}
    assert all(0 < row["progress"] < 1 for row in goals.values()), "进度条不该是 0% 或 100%"

    # 规则提醒：至少一段连续没离开
    assert data["stretches"], "演示数据没有触发 max_stretch_minutes 提醒"

    # 专注块、小时分布、标题细分
    assert data["focus_blocks"], "没有专注块"
    assert data["hourly"], "没有小时分布"
    assert data["titles"], "--titles 细分是空的"

    # 每日明细与日历：21 天一天不少（三天周日各记了 5 分钟，也都算有采样）
    assert len(data["daily"]) == 21
    assert len(data["calendar"]) == 21
    assert all(cell["has_samples"] for cell in data["calendar"])


def test_演示HTML包含全部章节(demo_run: dict[str, object]):
    html = demo_run["html"]
    assert isinstance(html, str)
    for section in (
        "应用排行",
        "目标对比",
        "连续没离开",
        "被排除的进程",
        "按窗口标题",
        "每日趋势",
        "每小时在用什么",
        "专注块",
        "账本可信度",
    ):
        assert section in html, f"HTML 里没有「{section}」章节"
    assert html.count("<svg") >= 2, "热力图与每日堆叠柱是两张 SVG"


# ------------------------------------------------------------------ 守恒


def test_演示数据守恒(demo_run: dict[str, object]):
    """演示数据也必须过守恒律 —— 它是用户接触到的第一份报告。"""
    data = demo_run["data"]
    assert isinstance(data, dict)
    totals = data["totals"]
    span = data["range"]["seconds"]
    assert sum(totals.values()) == pytest.approx(span, abs=1e-3), "各桶之和必须等于区间总长"
    covered = sum(seconds for kind, seconds in totals.items() if kind != "gap")
    assert covered == pytest.approx(data["covered_seconds"], abs=1e-3)
    assert data["coverage"] == pytest.approx(covered / span, abs=1e-6)

    # 逐日也一样：每天五个桶之和 = 当天的记录区间
    for row in data["daily"]:
        assert sum(row["totals"].values()) == pytest.approx(row["span"], abs=1e-3), (
            f"{row['day']} 的每日账本不守恒"
        )


# ------------------------------------------------------------------ 确定性


def test_演示生成是确定性的(tmp_path: Path):
    """同一种子 + 固定日期 → 除"生成于"外字节一致。截图和 demo.html 才不会飘。"""
    first = tmp_path / "run1"
    second = tmp_path / "run2"
    for target in (first, second):
        gen = _run(
            [
                sys.executable,
                str(ROOT / "scripts" / "make_demo.py"),
                "--out",
                str(target / "home"),
                "--html",
                str(target / "demo.html"),
            ]
        )
        assert gen.returncode == 0, gen.stderr
    normalize = lambda text: _GENERATED_AT.sub("· 生成于 <X>", text)  # noqa: E731
    assert normalize((first / "demo.html").read_text(encoding="utf-8")) == normalize(
        (second / "demo.html").read_text(encoding="utf-8")
    )


def test_入库的demo_html与生成器同步(demo_run: dict[str, object]):
    """docs/demo.html 烂在仓库里比没有更糟 —— 报告层一改，这个测试就红。"""
    assert COMMITTED_DEMO.exists(), "docs/demo.html 不在仓库里（被删了？还是没生成过？）"
    normalize = lambda text: _GENERATED_AT.sub("· 生成于 <X>", text)  # noqa: E731
    fresh = demo_run["html"]
    assert isinstance(fresh, str)
    assert normalize(COMMITTED_DEMO.read_text(encoding="utf-8")) == normalize(fresh), (
        "docs/demo.html 与 make_demo.py 的当前产出不一致 —— 重跑 python scripts/make_demo.py 刷新它"
    )


def test_入库的图表资产与生成器同步(demo_run: dict[str, object], tmp_path: Path):
    """三张 SVG 是从演示报告里抽的真实产出（不是另画的图），也必须同步。"""
    fresh = make_demo._extract_svgs(demo_run["html"], tmp_path)
    assert sorted(path.name for path in fresh) == [
        "demo-calendar.svg",
        "demo-daily.svg",
        "demo-hourly.svg",
    ]
    for path in fresh:
        committed = ROOT / "docs" / path.name
        assert committed.exists(), f"docs/{path.name} 不在仓库里"
        text = committed.read_text(encoding="utf-8")
        assert text == path.read_text(encoding="utf-8"), (
            f"docs/{path.name} 与当前产出不一致 —— 重跑 python scripts/make_demo.py"
        )
        # 独立 SVG 必须带 xmlns，否则浏览器和 README 都不渲染
        assert text.startswith("<svg"), "抽出来的不是一个完整 SVG"
        assert 'xmlns="http://www.w3.org/2000/svg"' in text, "独立 SVG 缺 xmlns"

"""闸门自身的合同：`verify_mvp.TARGET` 必须和 §15 那张表逐个数字对上。

闸门是用来证明"达标"的 —— 如果闸门比规格松，绿灯就是假的。
（实测抓到过一次：§15 写「磁盘 < 120 字节/行」，TARGET 却是 200；
§15 的「report --range today 端到端 < 1s」则压根没进闸门。）
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERIFY_MVP = ROOT / "scripts" / "verify_mvp.py"

# §15 的行名 → TARGET 的键。两份必须一一对应，多一个少一个都算漂移。
SPEC_TO_GATE = {
    "单次采样开销": "p95_ms",
    "常驻内存（tracker 进程）": "tracker_rss_mb",
    "一天 17280 行写入": "day_write_s",
    "30 天报告（51.8 万行，最坏情况）": "month_report_s",
    "30 天报告·最坏形状（段数 == 采样数）": "month_report_s",
    "磁盘占用": "bytes_per_row",
    "`report --range today` 端到端": "day_report_s",
}


def _target_values() -> dict[str, float]:
    """用 AST 把 `TARGET = {...}` 抠出来，不 import 也不执行脚本。

    实测踩过坑：`importlib` 会命中 `.pyc` 缓存 —— 源码刚被改过、且新旧内容**长度相同**
    （120.0 → 200.0）而 mtime 只精确到秒时，会读到旧值，测试莫名其妙地红/绿。
    契约测试必须确定性，所以走 AST 读字面量。
    """
    tree = ast.parse(VERIFY_MVP.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "TARGET" for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            assert isinstance(value, dict), "TARGET 不再是字面量字典？"
            return {str(key): float(val) for key, val in value.items()}
    raise AssertionError("在 scripts/verify_mvp.py 里没找到 TARGET = {...}")


def _spec_rows() -> dict[str, str]:
    text = (ROOT / "DEVELOPMENT.md").read_text(encoding="utf-8")
    assert "## 15." in text, "规格里找不到 §15 性能要求"
    section = text.split("## 15.", 1)[1].split("## 16.", 1)[0]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        if not line.startswith("|") or "---" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2 or cells[0] in {"指标", ""}:
            continue
        rows[cells[0]] = cells[1]
    return rows


def test_15_的每一行都被闸门覆盖():
    rows = _spec_rows()
    assert rows, "§15 的表没解析出来，测试的解析器该修了"
    missing = set(rows) - set(SPEC_TO_GATE)
    assert not missing, f"§15 新增了 {sorted(missing)}，闸门却没跟着看，绿灯会是假的"


def test_闸门的目标全部来自_15():
    orphan = set(_target_values()) - set(SPEC_TO_GATE.values())
    assert not orphan, f"TARGET 里 {sorted(orphan)} 在 §15 找不到出处"


@pytest.mark.parametrize("row_name", sorted(SPEC_TO_GATE))
def test_目标数值逐个对上(row_name: str):
    rows = _spec_rows()
    assert row_name in rows, f"§15 里没有「{row_name}」这一行 —— 规格改了就同步 SPEC_TO_GATE"
    cell = rows[row_name]
    match = re.search(r"<\s*([\d.]+)", cell)
    assert match, f"「{row_name}」的目标没写成 `< 数字` 的形式：{cell!r}"
    expected = float(match.group(1))
    key = SPEC_TO_GATE[row_name]
    actual = _target_values()[key]
    assert actual == expected, (
        f"§15「{row_name}」写的是 < {expected}，闸门 TARGET[{key!r}] 却是 {actual} —— "
        "闸门比规格松，绿灯不算数"
    )


def test_磁盘目标确实是_120_不是更松的值():
    """上面那条已经覆盖，但这一条把最要命的那个钉死，改坏了一定看得见。"""
    assert _target_values()["bytes_per_row"] == 120.0


# ------------------------------------------------------------------ 分位数
#
# 20-1 报的 P95 和 bench 卡的阈值都靠这个算法 —— 算错就是闸门卡错了东西。


def _load_bench():
    path = ROOT / "scripts" / "bench_overhead.py"
    spec = importlib.util.spec_from_file_location("timetrace_bench", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_分位数取的是最近秩():
    """旧公式 `int(n*p)-1` 只在 n 能被 100 整除时碰巧对，否则系统性偏低。"""
    percentile = _load_bench().percentile
    values = list(range(1, 101))  # 1..100
    assert percentile(values, 0.95) == 95
    assert percentile(values, 0.50) == 50
    assert percentile(values, 1.00) == 100
    assert percentile(values, 0.00) == 1


def test_分位数在旧公式会出错的采样数上也对():
    """n=150 时旧公式取到 94.67 分位、n=10 直接掉到 90 分位 —— 这两个必须钉死。"""
    percentile = _load_bench().percentile
    # ceil(0.95*n)：n=10→10、150→143、199→190、200→190（bench 默认 200 次）
    for n, expected in ((10, 10), (150, 143), (199, 190), (200, 190)):
        values = list(range(1, n + 1))
        assert percentile(values, 0.95) == expected, f"n={n} 的 95 分位应是 {expected}"


def test_分位数不怕没排序的输入():
    percentile = _load_bench().percentile
    assert percentile([5, 1, 4, 2, 3], 0.5) == 3


def test_分位数空输入要报错而不是返回零():
    """返回 0 会让性能闸门把"没测到"当成"测得极快"。"""
    percentile = _load_bench().percentile
    with pytest.raises(ValueError):
        percentile([], 0.95)


def test_探针和_bench_用的是同一个分位数实现():
    """两处各写一遍就会各错一遍 —— 必须是 import，不是复制粘贴。"""
    source = (ROOT / "scripts" / "verify" / "probes.py").read_text(encoding="utf-8")
    assert "from bench_overhead import percentile" in source, "probes 应该 import bench 的实现"
    assert "int(len(ordered) * 0.95)" not in source, "旧的错误公式不该再出现"
    bench = (ROOT / "scripts" / "bench_overhead.py").read_text(encoding="utf-8")
    assert "int(len(ordered) * 0.95)" not in bench, "bench 里也不该再有旧公式"


# ------------------------------------------------------------------ CI
#
# `.github/workflows/ci.yml` 一次都没跑过（仓库刚 git init），里面有两个会让它
# **两条腿全红** 的错误：探针名写错、非 Windows 上把 SKIP 判成 FAIL。


def _ci_text() -> str:
    return (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def _check_ps1_text() -> str:
    return (ROOT / "scripts" / "check.ps1").read_text(encoding="utf-8-sig")


def test_ci_调用的探针名都真实存在():
    """`probes.py windows_probe` 这种写错的名字会让 job 直接 exit 2 —— 一直是红的。"""
    calls = re.findall(r"probes\.py\s+([a-z_]+)", _ci_text())
    assert calls, "CI 里没找到任何 probes.py 调用？那这条测试白写了"

    # RUNNERS 的值是函数名（ast.Name），literal_eval 抠不动，只能取键。
    tree = ast.parse((ROOT / "scripts" / "verify" / "probes.py").read_text(encoding="utf-8"))
    runners: set[str] = set()
    for node in tree.body:
        is_target = isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "RUNNERS" for target in node.targets
        )
        if not is_target:
            continue
        assert isinstance(node.value, ast.Dict)
        runners = {key.value for key in node.value.keys if isinstance(key, ast.Constant)}
    assert runners, "没在 probes.py 里找到 RUNNERS"
    unknown = [name for name in calls if name not in runners]
    assert not unknown, f"CI 调用了不存在的探针 {unknown}（可用：{sorted(runners)}）"


def test_ci_格式检查范围与本地闸门一致():
    """本地查 `src tests scripts`，CI 只查 `src` 的话，测试文件写花了 CI 也不知道。"""
    ci = re.search(r"ruff format --check\s+([^\n]+)", _ci_text())
    local = re.search(r"ruff format --check\s+([^\n]+)", _check_ps1_text())
    assert ci and local, "两处都该有 ruff format --check"
    ci_scope = set(ci.group(1).split())
    local_scope = set(local.group(1).split())
    assert ci_scope == local_scope, f"CI 查 {sorted(ci_scope)}，本地查 {sorted(local_scope)} —— 必须一致"


def test_ci_会跑_15_验收与全部测试():
    text = _ci_text()
    assert "verify_mvp.py --skip-tests" in text, "CI 没跑 §20 验收"
    assert "uv run pytest" in text, "CI 没跑测试"
    assert "ruff check" in text and "pyright" in text


def test_非_windows_会跳过需要真桌面的那几条():
    """探针要真起 track，Linux 上 track 直接 PlatformError —— 那是"没法验"，不是"没通过"。"""
    verify = _load_verify_source()
    assert "DESKTOP_ONLY" in verify, "verify_mvp 该有一份「需要真桌面」的清单"
    for name in ("20-7 privacy", "20-8 e2e_real", "20-9 exit_codes"):
        assert name in verify, f"{name} 没进需要真桌面的清单"


def _load_verify_source() -> str:
    return (ROOT / "scripts" / "verify_mvp.py").read_text(encoding="utf-8")


def _load_verify_module() -> dict:
    """runpy 直接读源码执行，绕开 .pyc 缓存（importlib 有同秒改文件读到旧码的坑）。"""
    import runpy

    return runpy.run_path(str(ROOT / "scripts" / "verify_mvp.py"))


# ------------------------------------------------------------------ 内存没测到 ≠ 达标


def test_没测到内存时返回_None_而不是_0(monkeypatch: pytest.MonkeyPatch):
    """返回 0.0 会被闸门当成「0MB，远低于 40MB，达标」。"""
    monkeypatch.setattr("sys.platform", "linux")
    bench = _load_bench()
    assert bench.measure_tracker_rss(Path("/tmp/never-created")) is None


def _perf_metrics(rss) -> dict:
    return {
        "sampling": {"p95_ms": 0.02},
        "write": {"rows": 16800.0, "per_row_ms": 0.04, "disk_kb": 1848.0},
        "tracker_rss_mb": rss,
        "report_one_day": {"day_total_s": 0.02},
    }


def test_没测到内存时_20_10_不能算通过():
    """Windows 上测不到就该 FAIL，不是拿 0MB 混过去。"""
    import sys as _sys

    verify = _load_verify_module()
    status, detail = verify["check_perf"](False, _metrics=_perf_metrics(None))
    if _sys.platform == "win32":
        assert status == "FAIL" and "没能测到" in detail, f"Windows 上该 FAIL：{status} {detail}"
    else:
        assert status == "PASS" and "\u2014" in detail, f"非 Windows 不该硬报 MB 数：{detail}"


def test_测到内存时正常判阈值():
    """正路也要有测试：4.3MB 该 PASS，400MB 该 FAIL。"""
    verify = _load_verify_module()
    status, detail = verify["check_perf"](False, _metrics=_perf_metrics(4.3))
    assert status == "PASS", f"4.3MB 应达标：{detail}"
    status, detail = verify["check_perf"](False, _metrics=_perf_metrics(400.0))
    assert status == "FAIL" and "400" in detail, f"400MB 该 FAIL：{detail}"


def test_check_perf_的_12_注入口能绕开真跑_bench():
    """真跑一次 bench 要十几秒；单测只该测判定逻辑。"""
    verify = _load_verify_module()
    import inspect

    signature = inspect.signature(verify["check_perf"])
    assert "_metrics" in signature.parameters, "check_perf 该留一个测试注入口"

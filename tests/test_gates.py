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

"""闸门自身的合同：`verify_mvp.TARGET` 必须和 §15 那张表逐个数字对上。

闸门是用来证明"达标"的 —— 如果闸门比规格松，绿灯就是假的。
（实测抓到过一次：§15 写「磁盘 < 120 字节/行」，TARGET 却是 200；
§15 的「report --range today 端到端 < 1s」则压根没进闸门。）
"""

from __future__ import annotations

import ast
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

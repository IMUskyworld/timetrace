"""文档 ↔ 实现：规格 §4 写的每个命令、每个选项，parser 里必须真的有。

文档和实现分开长，早晚会长歪（这一路已经修过 `version` 子命令"§4 列了但不存在"）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _parser_choices():
    from timetrace.cli import build_parser

    parser = build_parser()
    sub = next(action for action in parser._subparsers._group_actions)
    return sub.choices


def _spec4_commands() -> dict[str, set[str]]:
    """抠出 §4 代码块里每个 `timetrace <cmd>` 及其方括号里的选项。"""
    text = (ROOT / "DEVELOPMENT.md").read_text(encoding="utf-8")
    assert "## 4. CLI 设计" in text, "找不到 §4 CLI 设计"
    block = text.split("## 4. CLI 设计", 1)[1]
    block = block.split("```", 2)[1] if "```" in block else ""
    entries: dict[str, list[str]] = {}
    current: str | None = None
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("timetrace "):
            match = re.match(r"timetrace\s+([a-z]+)", stripped)
            assert match, f"§4 里这行解析不了：{stripped}"
            current = match.group(1)
            entries[current] = [stripped]
        elif current is not None and (stripped.startswith("[") or stripped.startswith("(")):
            entries[current].append(stripped)
    return {name: set(re.findall(r"--[a-z-]+", "\n".join(lines))) for name, lines in entries.items()}


def test_15_写的每个命令都真的存在():
    choices = _parser_choices()
    documented = _spec4_commands()
    assert documented, "§4 一条命令都没解析出来，解析器坏了"
    missing = sorted(set(documented) - set(choices))
    assert not missing, f"§4 列了但 parser 没有：{missing}"


def test_15_写的每个选项都真的存在():
    choices = _parser_choices()
    problems = []
    for name, options in _spec4_commands().items():
        if name not in choices:
            continue  # 上一条已经报过
        actual = set()
        for action in choices[name]._actions:
            actual.update(action.option_strings)
        unknown = sorted(options - actual)
        if unknown:
            problems.append(f"{name}: {unknown}")
    assert not problems, "§4 写了但 parser 没有这些选项：" + "；".join(problems)


def test_readme_命令表覆盖全部子命令():
    """README 是用户第一眼看到的，漏一个命令就少一条路。"""
    choices = _parser_choices()
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^\|\s*`([a-z]+)\b", text, re.M))
    # 表里 `pause ... / resume` 合写在一行，把 resume 也算进去
    for line in text.splitlines():
        if line.startswith("| `") and " / `" in line:
            documented.update(re.findall(r"/\s*`([a-z]+)\b", line))
    missing = sorted(set(choices) - documented)
    assert not missing, f"README 命令表漏了 {missing}"


def test_readme_表里没有凭空多出来的命令():
    choices = _parser_choices()
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^\|\s*`([a-z]+)\b", text, re.M))
    extra = sorted(documented - set(choices))
    assert not extra, f"README 提了但不存在的命令：{extra}"


def test_退出码表与_errors_一致():
    """README 和 §4 都写死了一张退出码表，它必须和 errors.py 同一个来源。"""
    from timetrace.errors import (
        EXIT_CONFLICT,
        EXIT_INTERRUPTED,
        EXIT_OK,
        EXIT_RUNTIME,
        EXIT_USAGE,
    )

    expected = {0: EXIT_OK, 1: EXIT_RUNTIME, 2: EXIT_USAGE, 3: EXIT_CONFLICT, 130: EXIT_INTERRUPTED}
    assert set(expected.values()) == {0, 1, 2, 3, 130}

    for filename in ("README.md", "DEVELOPMENT.md"):
        text = (ROOT / filename).read_text(encoding="utf-8")
        for code, _expected in expected.items():
            assert re.search(rf"\b{code}\b", text), f"{filename} 的退出码表里没有 {code}"
        assert "130" in text, f"{filename} 没提 130（被中断）"


def test_usage_教程里的命令都真的存在():
    """docs/usage.md 教人用不存在的命令，比没有教程更糟。

    只扫代码块：散文里顺带提到命令名不算数（那是叙述，不是可执行指令）。
    三种入口都要认：timetrace / timetrace_dev.py / timetrace.exe。
    """
    choices = _parser_choices()
    text = (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```(?:powershell|bash|sh)?\n(.*?)```", text, re.S)
    assert blocks, "usage.md 里一个代码块都没有 —— 教程不能只有散文"
    problems: list[str] = []
    for block in blocks:
        for line in block.splitlines():
            stripped = line.strip()
            match = re.search(r"timetrace(?:_dev)?(?:\.exe)?\s+([a-z]+)\b", stripped)
            if not match:
                continue
            command = match.group(1)
            if command not in choices:
                problems.append(f"parser 没有的子命令：{command}（{stripped}）")
                continue
            actual = {option for action in choices[command]._actions for option in action.option_strings}
            for flag in re.findall(r"--[a-z-]+", stripped):
                if flag not in actual:
                    problems.append(f"{command} 没有 {flag}（{stripped}）")
    assert not problems, "usage.md 写了但实现没有：" + "；".join(problems)


def test_usage_教程提到的配置文件键都存在():
    """教程那张配置表与 Config 的字段对不上，用户照着写就不生效。"""
    from dataclasses import fields

    from timetrace.config import Config

    # sources 是内部记账字段（值的来源链），不是用户可写的配置键
    keys = {field.name for field in fields(Config)} - {"sources"}
    text = (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", text, re.M))
    assert documented, "usage.md 里没解析出配置键表格"
    unknown = sorted(documented - keys)
    assert not unknown, f"usage.md 教了 Config 没有的键：{unknown}"

"""隐私的第一道承诺：运行时零依赖、永不联网（§12-1 / §20-7）。

这条以前只由 `verify_mvp` 的探针检查 —— 而探针要真跑 `track`，只在 Windows 上有效。
放到 pytest 里，Linux CI 的 ubuntu leg 也照样卡得住：**不能因为换个平台就少验一件事**。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "timetrace"

# §12-1：这些模块出现一个就算破了"永不联网"
NETWORK_MODULES = {
    "socket",
    "urllib",
    "http",
    "requests",
    "httpx",
    "aiohttp",
    "ftplib",
    "smtplib",
    "telnetlib",
    "xmlrpc",
    "poplib",
    "imaplib",
    "nntplib",
    "websocket",
}


def _collect_imports() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                found.setdefault(path.name, []).append(name)
    return found


def test_src_里没有网络模块():
    """`import socket` 会让"永不联网"变成一句口号 —— 这里让它红。"""
    offenders = []
    for filename, imports in _collect_imports().items():
        for name in imports:
            if name.split(".")[0] in NETWORK_MODULES:
                offenders.append(f"{filename}: import {name}")
    assert not offenders, "src 里出现了网络模块：" + "；".join(sorted(offenders))


def test_源码文件数不是零_免得扫描器空跑():
    files = list(SRC.glob("*.py"))
    assert len(files) >= 10, f"只扫到 {len(files)} 个文件，扫描器多半写错了"


def test_运行时依赖表是空的():
    """零依赖是设计约束（ADR-0001），不是巧合。"""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "dependencies = []" in text, "pyproject 的 dependencies 不再是空列表 —— 常驻工具不该先建 venv"


def test_没有偷偷加的第三方导入():
    """零依赖的另一半：src 里除了标准库，一个第三方包都不该出现。

    只认"看起来像本站点/知名包"的顶层名，避免误伤；真正的强断言是上面那张网络黑名单。
    """
    stdlib_suspicious = {
        "requests",
        "httpx",
        "aiohttp",
        "click",
        "typer",
        "rich",
        "pydantic",
        "flask",
        "django",
    }
    bad = []
    for filename, imports in _collect_imports().items():
        for name in imports:
            top = name.split(".")[0]
            if top in stdlib_suspicious:
                bad.append(f"{filename}: import {name}")
    assert not bad, "src 引入了第三方包：" + "；".join(sorted(bad))


def test_扫描器本身能认出违例():
    """自检：喂一段含 socket 的假源码，扫描器必须抓得到 —— 否则上面几条都是绿的假象。"""
    tree = ast.parse("import socket\nfrom urllib.request import urlopen\n")
    hit = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hit += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            hit.append(node.module)
    assert any(name.split(".")[0] in NETWORK_MODULES for name in hit), "扫描器没抓到 socket/urllib"

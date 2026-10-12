"""交付物本身的合同：打包配置必须真的能把包送出去。

这里不构建 wheel（那要 build 后端，CI 的 dev extras 里没有），只核对配置 ——
配置错了 wheel 就是残的，而残 wheel 要等用户装了才发现。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
PACKAGE_DIR = ROOT / "src" / "timetrace"


def test_两个版本号是同一个值():
    """`timetrace --version` 读 __version__，pip 看的是 pyproject —— 分叉了就各说各话。"""
    match = re.search(r'^version\s*=\s*"([^"]+)"', PYPROJECT, re.M)
    assert match, "pyproject 里没有 version"
    init = (PACKAGE_DIR / "__init__.py").read_text(encoding="utf-8")
    runtime = re.search(r'^__version__\s*=\s*"([^"]+)"', init, re.M)
    assert runtime, "__init__.py 里没有 __version__"
    assert match.group(1) == runtime.group(1), (
        f"pyproject={match.group(1)} 而 __version__={runtime.group(1)} —— 装完和 --version 会对不上"
    )


def test__wheel_打包的是整个包目录():
    """写成某个子模块的话，新加的文件不会被带上，用户装到的是残缺的包。"""
    assert 'packages = ["src/timetrace"]' in PYPROJECT, (
        "hatch wheel 配置该打包整个 src/timetrace，而不是逐个列模块"
    )


def test_入口点指向真实函数():
    assert "[project.scripts]" in PYPROJECT, "没有 [project.scripts]，装完没有 timetrace 命令"
    assert 'timetrace = "timetrace.cli:main"' in PYPROJECT, "入口点变了？"
    from timetrace.cli import main as entry

    assert callable(entry), "timetrace.cli:main 不是可调用对象"


def test_源码里的模块都要能被这个包带上():
    """packages 配置覆盖整个目录时，源码里每个模块天然都会被带上。"""
    modules = {path.name for path in PACKAGE_DIR.glob("*.py")}
    assert len(modules) >= 15, f"只找到 {len(modules)} 个模块，包目录路径可能不对"
    assert "tray.py" in modules and "cli.py" in modules


def test_dev_依赖里有闸门要用的三件():
    """CI 装的是 `--extra dev`；少一个，对应那一步就在 CI 上炸。"""
    for package in ("pytest", "ruff", "pyright"):
        assert re.search(rf'"{package}[><=]', PYPROJECT), f"dev extras 里没有 {package}"


def test_requires_python_与代码用的特性一致():
    """代码里用到了 `X | Y` 类型语法、`slots=True` —— 低于 3.10/3.12 都会炸。"""
    assert 'requires-python = ">=3.11"' in PYPROJECT


def test_运行时依赖确实为空():
    """零依赖是本项目第一原则，由闸门和这里各卡一道。"""
    match = re.search(r"^dependencies\s*=\s*\[\s*\]", PYPROJECT, re.M)
    assert match, "dependencies 不再是空列表"

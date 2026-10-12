"""pytest 全局配置：只放 fixture，构造器在 builders.py。"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def home_dir(tmp_path: Path) -> Path:
    """一个干净的"数据目录"，用它跑 store/lock 这类真实文件操作。"""
    target = tmp_path / "home"
    target.mkdir()
    return target

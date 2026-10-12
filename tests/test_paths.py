"""paths：数据目录解析。整个项目的路径都从这里出，**绝不能依赖 CWD**（§22.4）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from timetrace.paths import Paths, default_home, resolve_home

WINDOWS = sys.platform == "win32"


# ------------------------------------------------------------------ 默认值


def test_windows_默认在_localappdata(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\me\AppData\Local")
    assert default_home() == Path(r"C:\Users\me\AppData\Local\timetrace")


def test_windows_没有_localappdata_退回_home(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("USERPROFILE", str(Path.home()))
    assert default_home() == Path.home() / "AppData" / "Local" / "timetrace"


def test_linux_用_xdg(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/home/me/.local/share")
    assert default_home() == Path("/home/me/.local/share/timetrace")


def test_linux_没有_xdg_退回_home(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert default_home() == Path.home() / ".local" / "share" / "timetrace"


# ------------------------------------------------------------------ 优先级


def test_优先级_命令行压过环境变量压过默认(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("TIMETRACE_HOME", str(tmp_path / "from-env"))
    assert resolve_home(str(tmp_path / "from-cli")) == tmp_path / "from-cli"
    assert resolve_home(None) == tmp_path / "from-env"
    monkeypatch.delenv("TIMETRACE_HOME")
    assert resolve_home(None) == default_home()


def test_环境变量支持波浪号(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TIMETRACE_HOME", "~/timetrace-data")
    assert resolve_home(None) == Path.home() / "timetrace-data"


def test_显式路径支持波浪号():
    assert resolve_home("~/somewhere") == Path.home() / "somewhere"


# ------------------------------------------------------------------ 空值绝不能落到 CWD


def test_空字符串当没给_绝不指向当前目录(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """`--home ""` 多半是 shell 变量没展开。

    照单全收的话 `Path("")` 会解析成**当前工作目录** —— 数据库就会被建进项目目录，
    正是 §22.4「不要依赖 CWD」要防的事。
    """
    monkeypatch.delenv("TIMETRACE_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    assert resolve_home("") == default_home()
    assert resolve_home("   ") == default_home()
    assert default_home() != Path.cwd(), "默认值本身就不该是 CWD"


def test_空的环境变量也当没给(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("TIMETRACE_HOME", "")
    assert resolve_home(None) == tmp_path / "AppData" / "timetrace"
    monkeypatch.setenv("TIMETRACE_HOME", "   ")
    assert resolve_home(None) == tmp_path / "AppData" / "timetrace"


def test_解析结果与_cwd_无关(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """同一个 --home，在任何工作目录下都该解析成同一条绝对路径。"""
    monkeypatch.delenv("TIMETRACE_HOME", raising=False)
    first = resolve_home(str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    second = resolve_home(str(tmp_path / "home"))
    assert first == second
    assert first.is_absolute()


# ------------------------------------------------------------------ Paths 属性


def test_所有路径都挂在同一个_home_下(tmp_path: Path):
    paths = Paths.resolve(str(tmp_path / "data"))
    assert paths.home == tmp_path / "data"
    assert paths.db == tmp_path / "data" / "timetrace.db"
    assert paths.lock == tmp_path / "data" / "lock"
    assert paths.config == tmp_path / "data" / "config.json"
    assert paths.report == tmp_path / "data" / "report.html"
    assert paths.log_dir == tmp_path / "data" / "logs"
    assert paths.log_file == tmp_path / "data" / "logs" / "track.log"


def test_ensure_建目录且幂等(tmp_path: Path):
    paths = Paths.resolve(str(tmp_path / "nested" / "deep"))
    assert not paths.home.exists()
    assert paths.ensure() is paths, "应该返回自己，方便链式用"
    assert paths.home.is_dir()
    assert paths.ensure().home.is_dir(), "第二次也要能过"


def test_describe_就是_home(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TIMETRACE_HOME", raising=False)
    assert Paths.resolve(None).describe() == str(resolve_home(None))


def test_真实默认目录_属性齐全():
    """在真的默认目录上跑一遍 —— 这些属性是所有命令的地基。"""
    paths = Paths.resolve(None)
    for attribute in ("db", "lock", "config", "report", "log_dir", "log_file"):
        value = getattr(paths, attribute)
        assert value.is_absolute(), f"{attribute} 不是绝对路径：{value}"
        assert value.parent in (paths.home, paths.log_dir), f"{attribute} 跑到 home 外面去了"

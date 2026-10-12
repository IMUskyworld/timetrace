"""托盘（M4）：回调接线、非 Windows 的拒绝方式、以及最重要的"起不来也不影响记录"。"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from timetrace.cli import _start_tray, build_parser
from timetrace.config import Config
from timetrace.errors import PlatformError
from timetrace.paths import Paths
from timetrace.tray import MENU_LABELS, TrayActions, TrayIcon, TrayUnavailable, platform_supported

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows 桌面")


class FakeLog:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, message: str) -> None:
        self.lines.append(message)


def make_actions() -> TrayActions:
    return TrayActions(
        open_report=lambda: None,
        toggle_pause=lambda paused: "ok",
        quit=lambda: None,
        log=lambda message: None,
    )


# ------------------------------------------------------------------ 平台与结构


def test_非_windows_给可读原因(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "linux")
    supported, reason = platform_supported()
    assert supported is False
    assert "只支持 Windows" in reason


def test_非_windows_起托盘抛_PlateformError(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "linux")
    tray = TrayIcon(tip="t", actions=make_actions())
    with pytest.raises(PlatformError) as caught:
        tray.start(timeout=0.5)
    assert "只支持 Windows" in str(caught.value)


def test_菜单项是固定的四个():
    assert set(MENU_LABELS) == {1001, 1002, 1003, 1004}
    assert MENU_LABELS[1001] == "打开今天报告"
    assert MENU_LABELS[1004] == "退出记录"


# ------------------------------------------------------------------ 失败要能降级


def test_托盘起不来时_返回_none_而不是抛(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """托盘是旁证，账才是主产物 —— 挂不上就记一行日志继续采样。"""
    from timetrace import tray as tray_module

    def explode(self, *args, **kwargs):
        raise TrayUnavailable("Shell 拒绝了")

    monkeypatch.setattr(TrayIcon, "start", explode)
    log = FakeLog()
    paths = Paths.resolve(tmp_path / "h").ensure()
    result = _start_tray(
        build_parser().parse_args(["track", "--tray", "--home", str(paths.home)]),
        paths,
        Config(),
        threading.Event(),
        log,  # type: ignore[arg-type]
    )
    assert result is None, "起不来必须返回 None，让 cmd_track 继续"
    assert any("托盘没挂上" in line for line in log.lines)
    assert any("Shell 拒绝了" in line for line in log.lines)
    assert tray_module.platform_supported()[0]


def test_托盘回调里的暂停与恢复走真实库(tmp_path: Path):
    """托盘线程里跑的回调必须用独立连接，且动作要落到库里。"""
    paths = Paths.resolve(tmp_path / "h").ensure()
    from timetrace.store import Store

    store = Store(paths.db).initialize()
    store.close()

    captured: dict[str, object] = {}

    def toggle(paused: bool) -> str:
        inner = Store(paths.db).open()
        try:
            from timetrace.timeutil import now_epoch

            if paused:
                opened = inner.open_pause(now_epoch(), note="从托盘暂停")
                return f"已暂停（{opened.start_ts} 起）"
            closed = inner.close_pause(now_epoch())
            return "本来就没有在暂停" if closed is None else "已恢复"
        finally:
            inner.close()

    log = FakeLog()
    actions = TrayActions(
        open_report=lambda: captured.setdefault("report", True),
        toggle_pause=toggle,
        quit=lambda: captured.setdefault("quit", True),
        log=log.write,
    )
    assert actions.toggle_pause(True).startswith("已暂停")
    reopened = Store(paths.db).open()
    try:
        assert reopened.active_pause(10**10) is not None, "暂停必须真的写进 pauses 表"
    finally:
        reopened.close()
    assert actions.toggle_pause(False) == "已恢复"
    reopened = Store(paths.db).open()
    try:
        assert reopened.active_pause(10**10) is None, "恢复要把 pause 关掉"
        assert len(reopened.pauses()) == 1, "区间本身要留着（报告里显示为已暂停）"
    finally:
        reopened.close()


def test_起停幂等_不抛异常():
    """stop() 收尾失败也不能反过来影响记录 —— 所以它必须吞掉一切。"""
    tray = TrayIcon(tip="t", actions=make_actions())
    tray.stop()  # 从没 start 过
    tray.stop()  # 第二次也不该抛


# ------------------------------------------------------------------ 真实桌面探针


@windows_only
@pytest.mark.windows
def test_真实挂上再摘掉():
    """在真桌面上 NIM_ADD → NIM_DELETE 走一遍，结构体布局错了这里会炸。"""
    logs: list[str] = []
    tray = TrayIcon(
        tip="timetrace 测试",
        actions=TrayActions(
            open_report=lambda: None,
            toggle_pause=lambda paused: "ok",
            quit=lambda: None,
            log=logs.append,
        ),
    )
    tray.start(timeout=5.0)
    try:
        assert tray._hwnd, "窗口应该建出来"
        tray.set_tip("timetrace 测试 · 改过了")
    finally:
        tray.stop()
    assert any("托盘已挂上" in line for line in logs)
    assert any("托盘已移除" in line for line in logs)
    assert tray._hwnd is None, "收尾要把窗口句柄清掉"

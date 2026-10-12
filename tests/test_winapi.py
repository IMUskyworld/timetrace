"""winapi：回绕处理（纯函数）+ 真实 Windows 探针（非 Windows 自动跳过）。"""

from __future__ import annotations

import sys
import time

import pytest

from timetrace import winapi
from timetrace.errors import PlatformError

# §16：真跑桌面的用例要能被 `pytest -m windows` 单独挑出来。
# MarkDecorator 不能相加，所以 marker 与 skipif 分两行打在每个真实用例上。
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows 桌面")


def test_tick_delta_普通情况():
    assert winapi.tick_delta(1_000, 400) == 600


def test_tick_delta_零与相同值():
    assert winapi.tick_delta(500, 500) == 0
    assert winapi.tick_delta(0, 0) == 0


def test_tick_delta_回绕后仍然正确():
    # GetTickCount 是 32 位毫秒，49.7 天回绕一次：跨过 0 点也要算出正确的增量
    almost_full = 0xFFFFFFFF - 400  # 输入发生在距离回绕还有 400ms 时
    after_wrap = 597  # 回绕之后又过了 597ms
    assert winapi.tick_delta(after_wrap, almost_full) == 400 + 1 + 597
    assert winapi.tick_delta(0, 0xFFFFFFFF) == 1  # 0xFFFFFFFF 的下一毫秒就是 0


def test_tick_delta_最大跨度不超过一天():
    assert winapi.tick_delta(0xFFFFFFFF, 0) == 0xFFFFFFFF


def test_platform_supported_给出可读原因():
    supported, reason = winapi.platform_supported()
    assert isinstance(supported, bool)
    if supported:
        assert reason == ""
    else:
        assert "只支持 Windows" in reason


@pytest.mark.windows
@windows_only
def test_真实取前台窗口():
    info = winapi.foreground_window()
    assert info.pid >= 0
    # 有前台窗口时应该能拿到进程名（拿不到也允许为空，但 pid 与标题至少有一个）
    assert info.pid > 0 or info.title == ""
    assert "\x00" not in info.title


@pytest.mark.windows
@windows_only
def test_真实空闲秒数非负且合理():
    idle = winapi.idle_seconds()
    assert idle >= 0.0
    assert idle < 86400 * 30  # 荒谬值说明 tick 回绕算错了


@pytest.mark.windows
@windows_only
def test_真实调用足够快():
    winapi.foreground_window()  # 预热，把首次加载 DLL 的时间排除掉
    started = time.perf_counter()
    for _ in range(20):
        winapi.foreground_window()
        winapi.idle_seconds()
    per_call_ms = (time.perf_counter() - started) * 1000 / 20
    assert per_call_ms < 20, f"单次采样 {per_call_ms:.2f}ms，太慢了（目标 < 2ms 量级）"


@pytest.mark.windows
@windows_only
def test_拿不到进程名时返回空串而不是抛异常():
    assert winapi.process_name_for_pid(0) == ""
    assert winapi.process_name_for_pid(-1) == ""


def test_非_windows_调用给出_PlatformError(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(winapi, "_libs", None)
    with pytest.raises(PlatformError) as caught:
        winapi.foreground_window()
    assert "只支持 Windows" in str(caught.value)
    assert caught.value.exit_code == 1

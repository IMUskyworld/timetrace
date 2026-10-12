"""Win32 前台窗口与空闲时间（ctypes，零运行时依赖）。

**这是全项目唯一碰系统 API 的文件。** 非 Windows 平台 `import` 不报错、
调用时才抛 `PlatformError` —— 这样 CI 的 Linux leg 也能收集并跳过测试，
而不是在收集阶段就炸掉。

只做三件事：
1. 问"现在哪个窗口在前台"（进程名 + 标题）；
2. 问"距离上一次键鼠输入过了多少秒"（只读一个数字，不读输入内容）；
3. 处理 `GetTickCount` 的 32 位回绕（49.7 天一次）。
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass

from timetrace.errors import PlatformError

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TICK_MASK = 0xFFFFFFFF
_INITIAL_PATH_BUFFER = 1024
_MAX_PATH_BUFFER = 32768


@dataclass(frozen=True, slots=True)
class WindowInfo:
    pid: int
    exe: str
    title: str

    @property
    def is_empty(self) -> bool:
        return self.pid <= 0 and not self.exe and not self.title


def tick_delta(now_tick: int, then_tick: int) -> int:
    """两次 32 位 tick 的差值（毫秒），正确处理回绕。

    纯函数：单测直接喂合成值，不需要真的等 49.7 天。
    """
    return (int(now_tick) - int(then_tick)) & TICK_MASK


def platform_supported() -> tuple[bool, str]:
    """返回 (能不能用, 原因)。doctor 与 CLI 用它决定是报错还是继续。"""
    if sys.platform != "win32":
        return False, f"前台窗口采样目前只支持 Windows（当前平台 {sys.platform}）"
    return True, ""


class _Libs:
    """懒加载并配置 ctypes 函数签名（配置错签名在 64 位下会静默截断指针）。"""

    user32: ctypes.WinDLL
    kernel32: ctypes.WinDLL

    def __init__(self) -> None:
        supported, reason = platform_supported()
        if not supported:
            raise PlatformError(reason, hint="macOS/Linux 后端在 M3（DEVELOPMENT.md §17）")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure()

    def _configure(self) -> None:
        u32, k32 = self.user32, self.kernel32
        u32.GetForegroundWindow.restype = wintypes.HWND
        u32.GetForegroundWindow.argtypes = []
        u32.GetWindowTextLengthW.restype = ctypes.c_int
        u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u32.GetWindowTextW.restype = ctypes.c_int
        u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u32.GetLastInputInfo.restype = wintypes.BOOL
        u32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        k32.GetTickCount.restype = wintypes.DWORD
        k32.GetTickCount.argtypes = []
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


_libs: _Libs | None = None


def libs() -> _Libs:
    global _libs
    if _libs is None:
        _libs = _Libs()
    return _libs


def _process_name(handle: int) -> str:
    """进程可执行文件名（小写）。拿不到就返回空串 —— 采样不该因为一个进程而失败。"""
    kernel32 = libs().kernel32
    size = wintypes.DWORD(_INITIAL_PATH_BUFFER)
    buffer = ctypes.create_unicode_buffer(size.value)
    if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        return buffer.value.rsplit("\\", 1)[-1].lower()
    size = wintypes.DWORD(_MAX_PATH_BUFFER)
    buffer = ctypes.create_unicode_buffer(size.value)
    if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        return buffer.value.rsplit("\\", 1)[-1].lower()
    return ""


def process_name_for_pid(pid: int) -> str:
    if pid <= 0:
        return ""
    kernel32 = libs().kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        return _process_name(handle)
    finally:
        kernel32.CloseHandle(handle)


def foreground_window() -> WindowInfo:
    """当前前台窗口。没有任何窗口时返回空的 WindowInfo（不是错误）。"""
    user32 = libs().user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return WindowInfo(pid=0, exe="", title="")
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(max(0, length) + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return WindowInfo(pid=pid.value, exe=process_name_for_pid(pid.value), title=buffer.value)


STILL_ACTIVE = 259


def pid_alive(pid: int) -> bool:
    """这个 PID 现在还活着吗？用于判断锁文件是不是"上一轮留下的尸体"。

    注意：Windows 会复用 PID，所以"活着"只是必要条件而不是充分条件 ——
    极端情况下会误判成"仍在运行"，此时用户用 `--force` 覆盖即可。
    """
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # 存在但不让我们发信号
        return True
    kernel32 = libs().kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def idle_seconds() -> float:
    """距离上一次键鼠输入的秒数（Read-Only：只读一个时间戳，不读输入内容）。"""
    libs_ = libs()
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not libs_.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return tick_delta(libs_.kernel32.GetTickCount(), info.dwTime) / 1000.0

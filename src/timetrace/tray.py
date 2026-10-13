"""系统托盘图标（M4）—— 可选，零依赖：只用标准库 + ctypes。

设计上只有两条铁律：

1. **托盘绝不能影响记录。** 它跑在自己的守护线程里，有自己的消息循环、自己的
   SQLite 连接；主循环只负责采样。任何一处失败都抛 `TrayUnavailable`，
   调用方记一行日志就继续采样 —— 图标没了是小事，账断了才是大事。
2. **不碰键盘、不联网、不截屏。** 图标只是几个菜单项，动作都是已经存在的命令。

非 Windows 平台 `import` 不报错、调用时才抛 `PlatformError`（与 `winapi.py` 同一套纪律），
这样 CI 的 Linux leg 也能收集并跳过测试。
"""

from __future__ import annotations

import ctypes
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

from timetrace.errors import PlatformError

# 菜单/窗口常量（windows.h 的值，写死以免引入第三方包）
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_NULL = 0x0000
WM_COMMAND = 0x0111
WM_LBUTTONDBLCLK = 0x0203
WM_LBUTTONUP = 0x0202
IDI_APPLICATION = 32512
MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
HWND_MESSAGE = -3
WS_OVERLAPPED = 0x00000000

# 菜单命令 ID
CMD_REPORT = 1001
CMD_PAUSE = 1002
CMD_RESUME = 1003
CMD_QUIT = 1004

MENU_LABELS = {
    CMD_REPORT: "打开今天报告",
    CMD_PAUSE: "暂停记录",
    CMD_RESUME: "恢复记录",
    CMD_QUIT: "退出记录",
}


class TrayUnavailable(RuntimeError):
    """托盘起不来（结构体对不上、Shell 拒绝、消息循环挂了）—— 调用方只需记一行日志。"""


@dataclass
class TrayActions:
    """托盘能做的四件事。全部由调用方注入，tray 模块自己不认识数据库。"""

    open_report: Callable[[], None]
    toggle_pause: Callable[[bool], str]  # 传入"当前是否已暂停"，返回给人看的一句话
    quit: Callable[[], None]
    log: Callable[[str], None]


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _NOTIFYICONDATAW(ctypes.Structure):
    """NOTIFYICONDATAW（Vista 布局）。写错 cbSize 会被 Shell 直接拒绝 —— 所以用 sizeof。"""

    class _U(ctypes.Union):
        _fields_ = [("timeout", ctypes.c_uint32), ("version", ctypes.c_uint32)]

    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("hWnd", wintypes.HWND),
        ("uID", ctypes.c_uint32),
        ("uFlags", ctypes.c_uint32),
        ("uCallbackMessage", ctypes.c_uint32),
        ("hIcon", wintypes.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.c_uint32),
        ("dwStateMask", ctypes.c_uint32),
        ("szInfo", ctypes.c_wchar * 256),
        ("uTimeoutOrVersion", _U),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.c_uint32),
        ("guidItem", _GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _last_error(what: str) -> OSError:
    """把 GetLastError 转成带说明的异常 —— 直接 `raise get_last_error()` 是 raise 一个 int。"""
    code = ctypes.get_last_error()
    return OSError(code, f"{what} 失败（GetLastError={code}）")


def platform_supported() -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, f"托盘目前只支持 Windows（当前平台 {sys.platform}）"
    return True, ""


class TrayIcon:
    """一个守护线程里的托盘图标。`start()` 失败会抛 `TrayUnavailable`，不影响采样。"""

    def __init__(self, *, tip: str, actions: TrayActions) -> None:
        self.tip = tip
        self.actions = actions
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._failure: str = ""
        self._hwnd: int | None = None
        self._nid: _NOTIFYICONDATAW | None = None
        # ctypes 句柄/函数表本来就是动态的，且 WinDLL 只在 Windows 上存在 ——
        # 标成 Any 既避开 Linux CI 上 ctypes.WinDLL 不存在的问题，也不会把
        # 这堆 Win32 胶水代码硬拗成假的静态类型。真正的防线是真桌面探针测试。
        self._user32: Any = None
        self._shell32: Any = None
        self._module: Any = None
        self._wndproc: Any = None
        self._icon: int | None = None
        self._quit_posted = False

    # ---------------------------------------------------------- 生命周期

    def start(self, *, timeout: float = 5.0) -> None:
        """起线程建图标；5 秒内没成就抛 `TrayUnavailable`。"""
        supported, reason = platform_supported()
        if not supported:
            raise PlatformError(reason, hint="去掉 --tray 就能用")
        self._thread = threading.Thread(target=self._run, name="timetrace-tray", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self.stop()
            raise TrayUnavailable(f"{timeout:g}s 内托盘没就绪")
        if self._failure:
            message = self._failure
            self.stop()
            raise TrayUnavailable(message)

    def stop(self) -> None:
        """请求退出并等线程收尾。幂等，也不抛异常 —— 收尾失败不能反过来影响记录。"""
        thread, self._thread = self._thread, None
        if thread is None:
            return
        hwnd = self._hwnd
        try:
            if hwnd and self._user32 is not None:
                # WM_CLOSE 走 DefWindowProc → DestroyWindow → 消息循环收到 WM_QUIT
                self._user32.PostMessageW(hwnd, 0x0010, 0, 0)
            else:
                self._post_quit()
        except Exception:
            self._post_quit()
        thread.join(timeout=5.0)
        if thread.is_alive():
            self.actions.log("托盘线程没能在 5s 内退出（图标可能残留，下次 start 会重挂）")

    def set_tip(self, tip: str) -> None:
        """改图标悬停文字。改不了就算了，托盘是旁证不是主产物。"""
        self.tip = tip
        if self._nid is None or self._user32 is None or not self._hwnd:
            return
        try:
            self._nid.szTip = tip[:127]
            self._nid.uFlags = NIF_TIP
            self._shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))
        except Exception:
            return

    # ---------------------------------------------------------- 线程体

    def _run(self) -> None:
        try:
            self._build()
        except Exception as cause:
            self._failure = f"{type(cause).__name__}: {cause}"
            self._ready.set()
            return
        self._ready.set()
        self._message_loop()
        self._cleanup()

    def _build(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # 签名必须写全：64 位下不写 restype/argtypes 会静默截断指针。
        user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.ATOM
        # 不写 argtypes，64 位句柄会被当 c_int 处理 → OverflowError（实测 argument 11）。
        user32.CreateWindowExW.argtypes = [
            ctypes.c_uint,  # dwExStyle
            wintypes.LPCWSTR,  # lpClassName
            wintypes.LPCWSTR,  # lpWindowName
            ctypes.c_uint,  # dwStyle
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,  # X / Y / nWidth / nHeight
            wintypes.HWND,  # hWndParent（消息窗口用 HWND_MESSAGE = -3）
            wintypes.HMENU,  # hMenu
            wintypes.HINSTANCE,  # hInstance
            wintypes.LPVOID,  # lpParam
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
        user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
        user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        user32.LoadIconW.restype = wintypes.HICON
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        wndproc_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM
        )
        user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        shell32.Shell_NotifyIconW.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL

        holder = self

        @wndproc_type
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_TRAY:
                if lparam in (WM_RBUTTONUP, WM_CONTEXTMENU, WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    holder.actions.log(f"托盘点击事件 0x{lparam:04X}，打开菜单")
                    holder._show_menu(hwnd)
                    return 0
                return 0
            if msg == WM_COMMAND:
                holder._run_command(int(wparam & 0xFFFF))
                return 0
            if msg == 0x0010:  # WM_CLOSE
                holder._post_quit()
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = wndproc  # 防止被 GC：ctypes 回调没有强引用就会失效
        self._user32, self._shell32 = user32, shell32
        self._module = kernel32.GetModuleHandleW(None)

        class_name = f"timetrace-tray-{id(self):x}"
        wc = _WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
        wc.hInstance = self._module
        wc.lpszClassName = class_name
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise _last_error("RegisterClassW")

        hwnd = user32.CreateWindowExW(
            0,
            class_name,
            "timetrace",
            WS_OVERLAPPED,
            0,
            0,
            0,
            0,
            HWND_MESSAGE,
            None,
            self._module,
            None,
        )
        if not hwnd:
            raise _last_error("CreateWindowExW")
        self._hwnd = hwnd

        icon = user32.LoadIconW(None, IDI_APPLICATION)
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = icon
        nid.szTip = self.tip[:127]
        self._icon, self._nid = icon, nid
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            raise _last_error("Shell_NotifyIconW(NIM_ADD)")
        self.actions.log(f"托盘已挂上：{self.tip}")

    def _message_loop(self) -> None:
        user32 = self._user32
        msg = wintypes.MSG()
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        user32.GetMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = ctypes.c_ssize_t
        while True:
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result in (0, -1):  # WM_QUIT 或出错都收工
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _show_menu(self, hwnd: int) -> None:
        user32 = self._user32
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [wintypes.HMENU, ctypes.c_uint, ctypes.c_size_t, wintypes.LPCWSTR]
        user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            None,
        ]
        user32.TrackPopupMenu.restype = ctypes.c_int
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        point = _POINT()
        user32.GetCursorPos(ctypes.byref(point))
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            for command in (CMD_REPORT, CMD_PAUSE, CMD_RESUME):
                user32.AppendMenuW(menu, MF_STRING, command, MENU_LABELS[command])
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, CMD_QUIT, MENU_LABELS[CMD_QUIT])
            # TrackPopupMenu 不会自己把点击转成 WM_COMMAND 之外还要处理"点外面关掉"，
            # 用 TPM_RETURNCMD 让它直接把命令 ID 返给我们，少一层分发。
            user32.SetForegroundWindow(hwnd)
            command = user32.TrackPopupMenu(
                menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, point.x, point.y, 0, hwnd, None
            )
            if command:
                self._run_command(command)
            # Shell 要求菜单关闭后给 owner 一个空消息，否则后续菜单交互可能失灵。
            user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(menu)

    def _run_command(self, command: int) -> None:
        try:
            if command == CMD_REPORT:
                self.actions.open_report()
            elif command == CMD_PAUSE:
                self.actions.log(self.actions.toggle_pause(True))
            elif command == CMD_RESUME:
                self.actions.log(self.actions.toggle_pause(False))
            elif command == CMD_QUIT:
                self.actions.quit()
                self._post_quit()
        except Exception as cause:
            self.actions.log(f"托盘命令 {command} 失败：{type(cause).__name__}: {cause}")

    def _post_quit(self) -> None:
        if self._quit_posted:
            return
        self._quit_posted = True
        try:
            if self._user32 is not None:
                self._user32.PostQuitMessage(0)
        except Exception:
            pass

    def _cleanup(self) -> None:
        try:
            if self._nid is not None and self._shell32 is not None:
                self._shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            if self._hwnd and self._user32 is not None:
                self._user32.DestroyWindow(self._hwnd)
            self.actions.log("托盘已移除")
        except Exception as cause:
            self.actions.log(f"托盘清理失败（图标可能残留）：{type(cause).__name__}: {cause}")
        finally:
            self._hwnd = None
            self._nid = None

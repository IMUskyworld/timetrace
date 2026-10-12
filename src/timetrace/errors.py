"""结构化错误与退出码契约（DEVELOPMENT.md §4）。

所有"可预期的失败"都抛 TimetraceError 的子类；cli 层统一捕获后
打印 message（+ 可选的 hint）并返回 exit_code。意外异常不吞：让它带栈崩掉，
因为那是 bug，不是用户操作问题。
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_CONFLICT = 3
EXIT_INTERRUPTED = 130


class TimetraceError(Exception):
    """可预期失败的基类。"""

    exit_code = EXIT_RUNTIME

    def __init__(self, message: str, *, hint: str | None = None, code: str = "runtime") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code

    def __str__(self) -> str:
        return f"{self.message}（{self.hint}）" if self.hint else self.message


class UsageError(TimetraceError):
    """参数非法、range 写法不对、互斥选项冲突。"""

    exit_code = EXIT_USAGE

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message, hint=hint, code="usage")


class PlatformError(TimetraceError):
    """平台不支持（MVP 只做 Windows）。"""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message, hint=hint, code="platform")


class StoreError(TimetraceError):
    """DB 打不开、写不进、schema 不认识。"""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message, hint=hint, code="store")


class LockBusy(TimetraceError):
    """已经有实例在跑（退出码 3 是契约的一部分，脚本靠它判断）。"""

    exit_code = EXIT_CONFLICT

    def __init__(self, message: str, *, holder_pid: int | None = None, hint: str | None = None) -> None:
        super().__init__(message, hint=hint, code="locked")
        self.holder_pid = holder_pid

"""错误类型与退出码合同（§4）。这一份是脚本靠它分支的地方，写错了外面就误判。"""

from __future__ import annotations

import pytest

from timetrace.errors import (
    EXIT_CONFLICT,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_RUNTIME,
    EXIT_USAGE,
    LockBusy,
    PlatformError,
    StoreError,
    TimetraceError,
    UsageError,
)


def test_退出码常量就是规格里那五个():
    """§4 的表：0 正常 · 1 运行时 · 2 用法 · 3 已有实例 · 130 被中断。"""
    assert (EXIT_OK, EXIT_RUNTIME, EXIT_USAGE, EXIT_CONFLICT, EXIT_INTERRUPTED) == (0, 1, 2, 3, 130)


@pytest.mark.parametrize(
    ("cls", "code", "name"),
    [
        (TimetraceError, EXIT_RUNTIME, "runtime"),
        (PlatformError, EXIT_RUNTIME, "platform"),
        (StoreError, EXIT_RUNTIME, "store"),
        (UsageError, EXIT_USAGE, "usage"),
        (LockBusy, EXIT_CONFLICT, "locked"),
    ],
)
def test_每种错误_声明自己的退出码(cls, code: int, name: str):
    error = cls("出事了", hint="照着做")  # type: ignore[call-arg]
    assert error.exit_code == code, f"{cls.__name__} 的退出码必须是 {code}"
    assert error.code == name
    assert error.message == "出事了"
    assert error.hint == "照着做"


def test_没有_hint_时_str_不带尾巴():
    assert str(TimetraceError("打不开")) == "打不开"


def test_有_hint_时_str_把提示带上():
    assert str(UsageError("看不懂", hint="用 --help")) == "看不懂（用 --help）"


def test_基类默认是运行时错误():
    """没有专门声明的子类落到 1 —— 兜底不能是 0（那会让脚本以为成功了）。"""

    class MyError(TimetraceError):
        pass

    assert MyError("x").exit_code == EXIT_RUNTIME
    assert EXIT_RUNTIME != EXIT_OK


def test_LockBusy_带上持有者_PID():
    """退出码 3 是契约，持有者 PID 让脚本能直接报出"谁占着"。"""
    error = LockBusy("已有实例", holder_pid=4321, hint="用 --force")
    assert error.exit_code == EXIT_CONFLICT
    assert error.holder_pid == 4321
    assert LockBusy("没有 PID").holder_pid is None


# ------------------------------------------------------------------ 真跑验证


def _spawn(*args: str) -> int:
    from timetrace.cli import main

    return main(list(args))


def test_cli_分发按错误类的声明返回(tmp_path, capsys):
    """cli 只调一次 `_fail` + `error.exit_code` —— 合同和实现不能劈成两半。"""
    home = str(tmp_path / "h")
    assert _spawn("status", "--home", home) == EXIT_OK
    capsys.readouterr()
    # purge 不给范围 = 用法错
    assert _spawn("purge", "--home", home) == EXIT_USAGE
    assert "要说清删什么" in capsys.readouterr().err
    # 还没数据 = 运行时错
    assert _spawn("report", "--home", home) == EXIT_RUNTIME


def test_argparse_自己的用法错也是_2(tmp_path):
    """非法子命令走 argparse 自己的退出码，跟 UsageError 一致（都是 2）。"""
    from timetrace.cli import build_parser

    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["bogus-command"])
    assert caught.value.code == EXIT_USAGE

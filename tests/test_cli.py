"""cli：自启脚本留痕、自启日志读取、doctor --range 体检。

这些是规格书之外补的"可验证性"：开机自启最怕静默失败，所以让它自己留证据。
"""

from __future__ import annotations

from pathlib import Path

from timetrace.cli import (
    autostart_log,
    build_parser,
    cmd_autostart,
    cmd_doctor,
    last_autostart_launch,
    launcher_lines,
)
from timetrace.domain import Sample
from timetrace.errors import EXIT_OK
from timetrace.paths import Paths
from timetrace.store import Store
from timetrace.timeutil import local_date, local_day_start, now_epoch


def test_launcher_lines_会先写自启日志再拉起(home_dir: Path):
    lines = launcher_lines(home_dir)
    joined = "\n".join(lines)
    assert f'if not exist "{home_dir / "logs"}" mkdir' in joined
    assert f'>> "{autostart_log(home_dir)}" echo' in joined
    assert joined.index("echo %date%") < joined.index('start "timetrace"'), "要先留痕再拉起"
    assert "-m timetrace track" in joined
    assert f'--home "{home_dir}"' in joined


def test_last_autostart_launch_读最后一行(home_dir: Path):
    assert last_autostart_launch(home_dir) is None, "没有日志时不该猜"
    log = autostart_log(home_dir)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8", newline="\n")
    assert last_autostart_launch(home_dir) is None, "空日志也没有可报告的内容"
    log.write_text("旧的一行\n2026-10-12 08:00:01.1 autostart launched\n\n", encoding="utf-8", newline="\n")
    assert last_autostart_launch(home_dir) == "2026-10-12 08:00:01.1 autostart launched"


def test_autostart_启用会写启动目录并打印日志去向(tmp_path: Path, monkeypatch, capsys):
    startup = tmp_path / "Startup"
    monkeypatch.setattr("timetrace.cli.startup_dir", lambda: startup)
    home = tmp_path / "home"
    args = build_parser().parse_args(["autostart", "--enable", "--home", str(home)])
    assert cmd_autostart(args) == EXIT_OK
    target = startup / "timetrace-track.cmd"
    assert target.exists()
    assert "autostart launched" in target.read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "启动日志" in out and "autostart.log" in out

    # --show 要能回答"上次到底起来过没有"
    autostart_log(home).parent.mkdir(parents=True, exist_ok=True)
    autostart_log(home).write_text("2026-10-12 08:00:01.1 autostart launched\r\n", encoding="utf-8")
    args = build_parser().parse_args(["autostart", "--show", "--home", str(home)])
    assert cmd_autostart(args) == EXIT_OK
    assert "上次自启 2026-10-12 08:00:01.1 autostart launched" in capsys.readouterr().out


def test_doctor_range_没有数据库时如实说(tmp_path: Path, capsys):
    home = tmp_path / "empty-home"
    args = build_parser().parse_args(["doctor", "--range", "today", "--home", str(home)])
    assert cmd_doctor(args) == EXIT_OK
    assert "还没有数据库" in capsys.readouterr().out


def test_doctor_range_给出覆盖率与最大采样间隔(tmp_path: Path, capsys):
    home = tmp_path / "home"
    paths = Paths.resolve(home).ensure()
    store = Store(paths.db).initialize()
    base = local_day_start(local_date(now_epoch())) + 3600.0  # 今天 01:00 起，避开"跨天"边界
    for index in range(60):
        store.insert_sample(
            Sample(ts=base + index * 5.0, pid=1, exe="code.exe", title="", idle_s=0.0, kind="active", note="")
        )
    store.close()

    args = build_parser().parse_args(["doctor", "--range", "today", "--home", str(home)])
    assert cmd_doctor(args) == EXIT_OK
    out = capsys.readouterr().out
    assert "覆盖率" in out
    assert "最大间隔" in out
    assert "异常采样" in out
    assert "60 条采样" in out


def test_doctor_range_坏区间报用法错误(tmp_path: Path):
    home = tmp_path / "home"
    Paths.resolve(home).ensure()
    args = build_parser().parse_args(["doctor", "--range", "not-a-date", "--home", str(home)])
    assert cmd_doctor(args) == EXIT_OK, "体检失败只降级成一条 fail 检查，不该让整个 doctor 崩掉"

"""cli：自启脚本留痕、自启日志读取、doctor --range 体检。

这些是规格书之外补的"可验证性"：开机自启最怕静默失败，所以让它自己留证据。
"""

from __future__ import annotations

import json
from pathlib import Path

from timetrace.cli import (
    autostart_log,
    build_parser,
    cmd_autostart,
    cmd_doctor,
    cmd_export,
    cmd_purge,
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


def test_export_然后_purge_对暂停也无损(tmp_path: Path, capsys):
    """ "export 之后可以放心 purge" 是 README 的承诺，这里把它变成可执行的断言。"""
    home = tmp_path / "home"
    paths = Paths.resolve(home).ensure()
    store = Store(paths.db).initialize()
    store.insert_sample(
        Sample(ts=now_epoch(), pid=1, exe="code.exe", title="", idle_s=0.0, kind="active", note="")
    )
    store.open_pause(now_epoch() - 60, "开会")
    store.close_pause(now_epoch() - 30)
    store.close()

    args = build_parser().parse_args(["export", "--format", "json", "--home", str(home)])
    assert cmd_export(args) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["pauses"][0]["note"] == "开会"
    assert payload["pauses"][0]["open"] is False
    assert payload["rows"][0]["kind"] == "active"

    args = build_parser().parse_args(["purge", "--all", "--yes", "--home", str(home)])
    assert cmd_purge(args) == EXIT_OK
    assert "1 段暂停" in capsys.readouterr().out
    reopened = Store(paths.db).open()
    try:
        assert reopened.count_samples() == 0
        assert reopened.pauses() == [], "purge --all 之后不该还剩暂停区间"
    finally:
        reopened.close()


def test_export_csv_带上备注列(tmp_path: Path, capsys):
    home = tmp_path / "home"
    paths = Paths.resolve(home).ensure()
    store = Store(paths.db).initialize()
    store.insert_sample(
        Sample(ts=now_epoch(), pid=0, exe="", title="", idle_s=0.0, kind="paused", note="开会")
    )
    store.close()
    args = build_parser().parse_args(["export", "--format", "csv", "--home", str(home)])
    assert cmd_export(args) == EXIT_OK
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "ts,exe,title,idle_s,kind,note"
    assert lines[1].endswith(",paused,开会")


def test_export_csv_pauses_out_单独存暂停区间(tmp_path: Path, capsys):
    home = tmp_path / "home"
    paths = Paths.resolve(home).ensure()
    store = Store(paths.db).initialize()
    store.insert_sample(
        Sample(ts=now_epoch(), pid=0, exe="", title="", idle_s=0.0, kind="paused", note="开会")
    )
    store.open_pause(now_epoch() - 60, "开会")
    store.close_pause(now_epoch() - 30)
    store.close()

    pauses_csv = tmp_path / "pauses.csv"
    args = build_parser().parse_args(
        ["export", "--format", "csv", "--pauses-out", str(pauses_csv), "--home", str(home)]
    )
    assert cmd_export(args) == EXIT_OK
    out = capsys.readouterr().out
    assert "已导出 1 段暂停区间" in out
    assert "! CSV 里只有" not in out, "已经给了 --pauses-out 就不必再警告"
    lines = pauses_csv.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "start,end,note,open"
    assert lines[1].split(",")[2] == "开会"
    assert lines[1].split(",")[3] == "False"


def test_export_csv_没有_pauses_out_会明说可能丢(tmp_path: Path, capsys):
    home = tmp_path / "home"
    paths = Paths.resolve(home).ensure()
    store = Store(paths.db).initialize()
    store.open_pause(now_epoch() - 60, "开会")
    store.close_pause(now_epoch() - 30)
    store.close()
    args = build_parser().parse_args(["export", "--format", "csv", "--home", str(home)])
    assert cmd_export(args) == EXIT_OK
    assert "精确起止会随 purge 一起消失" in capsys.readouterr().out


def test_export_写文件不产生双回车(tmp_path: Path, capsys):
    """Windows 文本模式会把 \n 翻成 \r\n；如果先翻一次再翻一次，别的工具读出全是空行。"""
    home = tmp_path / "home"
    paths = Paths.resolve(home).ensure()
    store = Store(paths.db).initialize()
    for offset in range(3):
        store.insert_sample(
            Sample(
                ts=now_epoch() + offset,
                pid=1,
                exe="code.exe",
                title="",
                idle_s=0.0,
                kind="active",
                note="",
            )
        )
    store.close()

    csv_out = tmp_path / "rows.csv"
    json_out = tmp_path / "rows.json"
    assert (
        cmd_export(
            build_parser().parse_args(
                ["export", "--format", "csv", "--out", str(csv_out), "--home", str(home)]
            )
        )
        == EXIT_OK
    )
    assert (
        cmd_export(
            build_parser().parse_args(
                ["export", "--format", "json", "--out", str(json_out), "--home", str(home)]
            )
        )
        == EXIT_OK
    )
    capsys.readouterr()

    raw = csv_out.read_bytes()
    assert b"\r\r" not in raw, f"CSV 出现双回车：{raw[:80]!r}"
    lines = csv_out.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "ts,exe,title,idle_s,kind,note"
    assert len(lines) == 4, f"表头 + 3 行，中间不能有空行，实际 {len(lines)} 行"
    assert all(line.startswith("2") for line in lines[1:]), "数据行都该以 ISO 时间戳开头"

    json_raw = json_out.read_bytes()
    assert b"\r\r" not in json_raw, f"JSON 出现双回车：{json_raw[:80]!r}"
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert len(payload["rows"]) == 3

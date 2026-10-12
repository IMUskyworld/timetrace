"""cli：自启脚本留痕、自启日志读取、doctor --range 体检。

这些是规格书之外补的"可验证性"：开机自启最怕静默失败，所以让它自己留证据。
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from timetrace.cli import (
    autostart_log,
    build_parser,
    cmd_autostart,
    cmd_doctor,
    cmd_export,
    cmd_purge,
    cmd_report,
    cmd_status,
    cmd_track,
    last_autostart_launch,
    launcher_lines,
)
from timetrace.domain import Sample
from timetrace.errors import EXIT_OK
from timetrace.paths import Paths
from timetrace.store import Store
from timetrace.timeutil import format_local, iso_to_epoch, local_date, local_day_start, now_epoch


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


def test_report_真实库多天_每日明细贯通(tmp_path: Path, capsys):
    """从真 SQLite 到三种报告输出走一遍：证明 daily 不只是单测里的合成数据。"""
    home = tmp_path / "home"
    paths = Paths.resolve(home).ensure()
    store = Store(paths.db).initialize()
    today = local_date(now_epoch())
    per_day = (120, 60, 180)
    exes = ("code.exe", "msedge.exe", "code.exe")
    for offset, (minutes, exe) in enumerate(zip(reversed(per_day), reversed(exes), strict=True)):
        day = today - datetime.timedelta(days=offset)
        start = local_day_start(day) + 9 * 3600
        for index in range(minutes):
            store.insert_sample(
                Sample(
                    ts=start + index * 60.0,
                    pid=100,
                    exe=exe,
                    title="",
                    idle_s=0.0,
                    kind="active",
                    note="",
                )
            )
    # 录制时的 interval=60 必须落库：否则报告会按默认 5s 判定，60s 采样全被当成断档。
    store.meta_set("last_config", json.dumps({"interval": 60.0, "idle_threshold": 300.0}))
    store.close()

    args = build_parser().parse_args(["report", "--range", "all", "--json", "--home", str(home)])
    assert cmd_report(args) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["daily"]) == 3
    assert [row["day"] for row in payload["daily"]] == [
        (today - datetime.timedelta(days=2)).isoformat(),
        (today - datetime.timedelta(days=1)).isoformat(),
        today.isoformat(),
    ]
    # 逐日守恒：每行五个桶之和 == 该行被记录的时长
    for row in payload["daily"]:
        assert sum(row["totals"].values()) == pytest.approx(row["covered"], abs=1e-6)
    # 每天最后一分钟会被"下一采样在 22 小时后"的断档吃掉（采样法既定口径），
    # 所以只断言"差不超过一个采样间隔"，而不是硬钉一个漂亮的整数。
    for row, expected in zip(payload["daily"], (7200.0, 3600.0, 10800.0), strict=True):
        assert expected - 60.0 <= row["totals"]["app"] <= expected
    assert sum(row["totals"]["app"] for row in payload["daily"]) == pytest.approx(payload["totals"]["app"])
    assert [row["switches"] for row in payload["daily"]] == [0, 1, 1], "逐日切换能对上总数"
    assert payload["switches"] == 2

    html_path = tmp_path / "report.html"
    args = build_parser().parse_args(
        ["report", "--range", "all", "--html", "--out", str(html_path), "--home", str(home)]
    )
    assert cmd_report(args) == EXIT_OK
    capsys.readouterr()
    html = html_path.read_text(encoding="utf-8")
    assert "每日趋势" in html
    assert "daily-table" in html
    assert "<script" not in html and "<link" not in html


# ------------------------------------------------------------------ track 文件日志


def test_track_log_只记生命周期与出错不记每条采样(tmp_path: Path):
    from timetrace.cli import _TrackLog

    path = tmp_path / "logs" / "track.log"
    log = _TrackLog(path)
    log.open("interval=5s idle=300s")
    log.write("采样失败：boom")
    log.close("=== track 结束 · 写入 3 条采样 ===")

    text = path.read_text(encoding="utf-8")
    assert "track 启动" in text and "interval=5s" in text
    assert "采样失败：boom" in text
    assert "track 结束" in text
    assert path.parent.is_dir(), "日志目录要自动建出来"


def test_track_log_超封顶就轮转(tmp_path: Path):
    from timetrace.cli import _TrackLog

    path = tmp_path / "track.log"
    path.write_text("x" * (_TrackLog.MAX_BYTES + 1), encoding="utf-8")
    log = _TrackLog(path)
    log.open("reason")
    log.close("=== 结束 ===")
    old = path.with_suffix(".log.old")
    assert old.exists(), "超过 2MB 应该轮转成 .old"
    assert old.stat().st_size > _TrackLog.MAX_BYTES
    assert "track 启动" in path.read_text(encoding="utf-8")


def test_track_log_写不进去也不能把记录带崩(tmp_path: Path):
    """日志路径是个目录 → open 必然失败；记录必须照常完成。"""
    from timetrace.cli import _TrackLog

    blocker = tmp_path / "track.log"
    blocker.mkdir()  # 让它变成"打不开的文件"
    log = _TrackLog(blocker)
    log.open("reason")  # 不该抛
    log.write("任何东西")  # 不该抛
    log.close("=== 结束 ===")  # 不该抛


def test_track_log_关闭后写入是空操作(tmp_path: Path):
    from timetrace.cli import _TrackLog

    path = tmp_path / "track.log"
    log = _TrackLog(path)
    log.open("reason")
    log.close("=== 结束 ===")
    before = path.read_text(encoding="utf-8")
    log.write("不该出现")
    assert path.read_text(encoding="utf-8") == before


def test_自启脚本默认打开日志(home_dir: Path):
    """自启的 tracker 没有控制台可看，日志是唯一线索 —— 必须默认带上 --log。"""
    joined = "\n".join(launcher_lines(home_dir))
    assert "-m timetrace track --log" in joined


def test_track_log_由配置打开(tmp_path: Path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text('{"log": true}', encoding="utf-8")
    monkeypatch.setattr("timetrace.cli.startup_dir", lambda: tmp_path / "Startup")
    args = build_parser().parse_args(["track", "--once", "--quiet", "--home", str(home)])
    assert cmd_track(args) == EXIT_OK
    assert (home / "logs" / "track.log").exists()
    assert "日志" in capsys.readouterr().out


# ------------------------------------------------------------------ status 的时间显示


def _write_run_meta(home, started, stopped=None):
    paths = Paths.resolve(home).ensure()
    store = Store(paths.db).initialize()
    store.meta_set("last_start", started)
    if stopped is not None:
        store.meta_set("last_stop", stopped)
    store.close()
    return paths


def test_status_正常起止给出完整区间(tmp_path: Path, capsys):
    _write_run_meta(tmp_path / "h", "2026-10-12T02:00:00.000Z", "2026-10-12T03:00:00.000Z")
    args = build_parser().parse_args(["status", "--home", str(tmp_path / "h")])
    assert cmd_status(args) == EXIT_OK
    out = capsys.readouterr().out
    assert "上次记录" in out and "→" in out
    assert "结束时间未知" not in out


def test_status_结束早于开始时不给倒过来的时间对(tmp_path: Path, capsys):
    """正在跑的 tracker：last_start 是本次的，last_stop 还是上次的。

    直接拼成 A → B 会显示"18:31 → 18:28"这种结束早于开始的胡话。
    没有锁（说明没在跑）且 stop < start → 只能是被杀掉过，老实说结束时间未知。
    """
    _write_run_meta(tmp_path / "h", "2026-10-12T10:31:00.000Z", "2026-10-12T10:28:00.000Z")
    args = build_parser().parse_args(["status", "--home", str(tmp_path / "h")])
    assert cmd_status(args) == EXIT_OK
    out = capsys.readouterr().out
    assert "结束时间未知" in out
    assert "10:28" not in out, "不该把倒过来的时间对显示出来"


def test_status_没有结束时间时明说(tmp_path: Path, capsys):
    _write_run_meta(tmp_path / "h", "2026-10-12T10:31:00.000Z")
    args = build_parser().parse_args(["status", "--home", str(tmp_path / "h")])
    assert cmd_status(args) == EXIT_OK
    out = capsys.readouterr().out
    assert "没有结束时间" in out


def test_status_正在跑时只说开始时间(tmp_path: Path, monkeypatch, capsys):
    _write_run_meta(tmp_path / "h", "2026-10-12T10:31:00.000Z", "2026-10-12T10:28:00.000Z")
    # 伪造一把"有人持有且进程活着"的锁
    lock = tmp_path / "h" / "lock"
    lock.write_text(
        json.dumps(
            {
                "pid": 999999,
                "started_at": "2026-10-12T10:31:00.000Z",
                "exe_path": "C:/python.exe",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("timetrace.winapi.pid_alive", lambda pid: True)
    args = build_parser().parse_args(["status", "--home", str(tmp_path / "h")])
    assert cmd_status(args) == EXIT_OK
    out = capsys.readouterr().out
    # meta 里是 UTC（Z 结尾），status 显示的是本地时间 —— 这里固定用同一套换算。
    expected = format_local(iso_to_epoch("2026-10-12T10:31:00.000Z"))
    assert "正在记录" in out and f"从 {expected}" in out
    assert "上次记录" not in out

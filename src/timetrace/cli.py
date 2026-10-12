"""命令行入口：参数解析、命令分发、退出码契约（DEVELOPMENT.md §4）。

退出码：0 正常 · 1 运行时错误 · 2 用法错误 · 3 已有实例在跑 · 130 被中断。
所有"可预期失败"都在这里转成人话 + 退出码；意外异常不吞（那是 bug，应该带栈崩）。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import signal
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from timetrace import __version__, html_template, winapi
from timetrace.config import Config, from_env, load_config
from timetrace.domain import Pause, Range, Sample
from timetrace.errors import (
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_RUNTIME,
    PlatformError,
    StoreError,
    TimetraceError,
    UsageError,
)
from timetrace.paths import Paths
from timetrace.report import build_report, render_console, render_json
from timetrace.sampler import MAX_CONSECUTIVE_WRITE_FAILURES, Sampler, make_provider
from timetrace.segment import build_segments
from timetrace.store import (
    META_LAST_CONFIG,
    META_LAST_START,
    META_LAST_STOP,
    InstanceLock,
    Store,
)
from timetrace.timeutil import (
    describe_range,
    epoch_to_iso,
    format_duration,
    format_local,
    format_percent,
    now_epoch,
    parse_range,
)

AUTOSTART_NAME = "timetrace-track.cmd"
PROGRAM = "timetrace"


# ------------------------------------------------------------------ 参数


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="本机屏幕时间账本：本地优先、永不联网、只报账不打分。",
        epilog="退出码：0 正常 · 1 运行时错误 · 2 用法错误 · 3 已有实例在跑 · 130 被中断",
    )
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="命令")

    track = sub.add_parser("track", help="常驻采样（前台窗口 + 空闲时间）")
    _home(track)
    track.add_argument("--interval", type=float, default=None, help="采样间隔秒（默认 5）")
    track.add_argument("--idle-threshold", type=float, default=None, help="空闲判定阈值秒（默认 300）")
    track.add_argument("--focus-min", type=float, default=None, help="专注块最小分钟（默认 10）")
    track.add_argument("--no-titles", action="store_true", help="不记录窗口标题，只记进程名")
    track.add_argument("--exclude", default=None, help="不计入排名的进程，逗号分隔")
    track.add_argument("--force", action="store_true", help="无视已有实例强行接管（会重复计数）")
    track.add_argument("--once", action="store_true", help="只采一次就退出（自检用）")
    track.add_argument("--max-samples", type=int, default=None, help="采够 N 条就退出（验收用）")
    track.add_argument("--quiet", action="store_true", help="不逐条打印采样")
    track.add_argument(
        "--log",
        action="store_true",
        help="把出错与生命周期写进 <home>/logs/track.log（配置里 log:true 同效）",
    )
    track.add_argument(
        "--tray",
        action="store_true",
        help="挂一个系统托盘图标（可选；起不来就继续用控制台记录，绝不影响采样）",
    )

    pause = sub.add_parser("pause", help="暂停记录（开会/演示/隐私时刻）")
    _home(pause)
    pause.add_argument("--note", default="", help="备注，会显示在报告里")

    resume = sub.add_parser("resume", help="恢复记录")
    _home(resume)

    status = sub.add_parser("status", help="现在在记吗、数据在哪、今天记了多少")
    _home(status)

    report = sub.add_parser("report", help="出报告（console / HTML / JSON）")
    _home(report)
    report.add_argument(
        "--range", dest="range_spec", default="today", help="today|yesterday|week|month|all|YYYY-MM-DD|A..B"
    )
    report.add_argument("--html", action="store_true", help="写单文件 HTML 报告")
    report.add_argument("--out", default=None, help="HTML 输出路径（默认 <home>/report.html）")
    report.add_argument("--json", action="store_true", help="stdout 只输出 JSON")
    report.add_argument("--titles", action="store_true", help="按窗口标题细分")
    report.add_argument("--top", type=int, default=15, help="console 里最多列几个应用")
    report.add_argument("--idle-threshold", type=float, default=None, help="覆盖录制时的空闲阈值")
    report.add_argument("--focus-min", type=float, default=None, help="覆盖专注块阈值（分钟）")

    purge = sub.add_parser("purge", help="真删数据（DELETE + VACUUM）")
    _home(purge)
    purge.add_argument("--range", dest="range_spec", default=None, help="删这个区间")
    purge.add_argument("--all", action="store_true", help="删掉全部")
    purge.add_argument("--yes", action="store_true", help="不确认直接删（脚本里用）")

    export = sub.add_parser("export", help="导出采样（json / csv）")
    _home(export)
    export.add_argument("--range", dest="range_spec", default="all")
    export.add_argument("--format", dest="fmt", choices=("json", "csv"), default="json")
    export.add_argument("--out", default=None, help="输出文件（默认 stdout）")
    export.add_argument("--no-titles", action="store_true", help="导出时去掉窗口标题")
    export.add_argument(
        "--pauses-out",
        default=None,
        help="另存一份暂停区间 CSV（JSON 已内含，CSV 格式才需要）",
    )

    autostart = sub.add_parser("autostart", help="开机自启（只写当前用户的启动目录）")
    _home(autostart)
    group = autostart.add_mutually_exclusive_group(required=True)
    group.add_argument("--enable", action="store_true")
    group.add_argument("--disable", action="store_true")
    group.add_argument("--show", action="store_true")
    autostart.add_argument(
        "--tray", action="store_true", help="生成的启动脚本带上 --tray（开机就挂托盘图标）"
    )

    version = sub.add_parser("version", help="打印版本号（与 --version 同义）")
    # 也收 --home：其余九个子命令全都收，唯独它不收的话，
    # `for c in ...; timetrace $c --home $DIR` 这种统一调用会在 version 上炸。
    _home(version)

    doctor = sub.add_parser("doctor", help="自检：能不能采样、DB 可写、锁、开销")
    _home(doctor)
    doctor.add_argument(
        "--range", dest="range_spec", default=None, help="顺带体检某个区间的记录质量（如 today）"
    )

    return parser


def _home(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", default=None, help="数据目录（默认 %%LOCALAPPDATA%%\\timetrace）")


# ------------------------------------------------------------------ 入口


def _install_signal_handlers() -> None:
    """Ctrl+C / Ctrl+Break / terminate 都走同一条优雅退出路径。

    Windows 上 Ctrl+Break 的默认行为是**直接终止进程**（退出码 0xC000013A），
    那样 meta 里的 last_stop 和锁文件都来不及收尾。这里统一转成 KeyboardInterrupt。
    """

    def raise_interrupt(_number: int, _frame: object) -> None:
        raise KeyboardInterrupt

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, raise_interrupt)
        except (ValueError, OSError, RuntimeError):  # 非主线程 / 平台不支持
            continue


def main(argv: list[str] | None = None) -> int:
    _prepare_console()
    _install_signal_handlers()
    parser = build_parser()
    args = parser.parse_args(argv)
    bare = args.command is None  # §4：不带参数 = status + 一行提示
    command = args.command or "status"
    handlers = {
        "track": cmd_track,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "status": cmd_status,
        "report": cmd_report,
        "purge": cmd_purge,
        "export": cmd_export,
        "autostart": cmd_autostart,
        "doctor": cmd_doctor,
        "version": cmd_version,
    }
    try:
        code = handlers[command](args)
        if bare:
            _out("")
            _out(f"用 `{PROGRAM} --help` 看全部命令，`{PROGRAM} report --range today` 看今天的账。")
        return code
    except TimetraceError as error:
        # 退出码由错误类自己声明（`errors.EXIT_*` 是唯一的合同）。
        # 以前这里按类分了 5 个分支、各自写死一个常量 —— 改了 `UsageError.exit_code`
        # 什么都不会发生，合同和实现就这么悄悄劈成两半。
        _fail(error)
        return error.exit_code
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return EXIT_INTERRUPTED


def _prepare_console() -> None:
    """cp936 控制台上也要能打中文（继承前几个项目的教训，§22.3）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def cmd_version(args: argparse.Namespace) -> int:
    _out(f"{PROGRAM} {__version__}")
    return EXIT_OK


def _fail(error: TimetraceError) -> None:
    print(f"x {error.message}", file=sys.stderr)
    if error.hint:
        print(f"  → {error.hint}", file=sys.stderr)


def _out(text: str = "") -> None:
    print(text)


# ------------------------------------------------------------------ 公共装配


def _paths(args: argparse.Namespace) -> Paths:
    return Paths.resolve(getattr(args, "home", None))


def _effective_config(args: argparse.Namespace, paths: Paths) -> Config:
    config, warnings = load_config(paths.config)
    # §14 的优先级是 命令行 > 环境变量 > config.json > 内置默认：
    # 文件读完先叠环境变量，再叠命令行 —— 少一层都会让 doctor 念出错的来源。
    config, env_warnings = from_env(config)
    for warning in [*warnings, *env_warnings]:
        print(f"! {warning}", file=sys.stderr)
    config = config.merged(
        interval=getattr(args, "interval", None),
        idle_threshold=getattr(args, "idle_threshold", None),
        focus_min=getattr(args, "focus_min", None),
        exclude=_split_exclude(getattr(args, "exclude", None)) or None,
        titles=False if getattr(args, "no_titles", False) else None,
        log=True if getattr(args, "log", False) else None,
    )
    config.validate()
    return config


def _split_exclude(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


def _recorded_config(store: Store) -> Config | None:
    """录制时生效的切分参数：用它出报告，历史就不会因为后来改了设置而变样。"""
    raw = store.meta_get(META_LAST_CONFIG)
    if not raw:
        return None
    try:
        from timetrace.config import from_dict

        config, _ = from_dict(json.loads(raw), source="录制时")
        return config
    except (ValueError, TypeError, UsageError):
        return None


def _config_for_report(store: Store, args: argparse.Namespace, paths: Paths) -> Config:
    config = _effective_config(args, paths)
    recorded = _recorded_config(store)
    if recorded is None:
        return config
    return replace(
        config,
        interval=args.interval if getattr(args, "interval", None) else recorded.interval,
        idle_threshold=(args.idle_threshold if args.idle_threshold else recorded.idle_threshold),
    )


def _open_store(paths: Paths, *, create: bool = True) -> Store:
    if not create and not paths.db.exists():
        raise StoreError(f"还没有数据：{paths.db} 不存在", hint="先跑 `timetrace track` 记录一点时间")
    store = Store(paths.db)
    return store.initialize() if create else store.open()


def _lock_state(paths: Paths) -> str:
    holder = InstanceLock(paths.lock).holder()
    if holder is None:
        return "空闲（没有实例在跑）"
    alive = winapi.pid_alive(holder.pid) if sys.platform == "win32" else True
    return f"{holder.describe()}，{'在跑' if alive else '已退出（陈旧锁，下次启动会自动接管）'}"


# ------------------------------------------------------------------ track


def cmd_track(args: argparse.Namespace) -> int:
    supported, reason = winapi.platform_supported()
    if not supported:
        raise PlatformError(reason, hint="这个 MVP 只做 Windows：前台窗口信息来自 Win32")
    paths = _paths(args).ensure()
    config = _effective_config(args, paths)
    store = _open_store(paths)
    lock = InstanceLock(paths.lock)
    lock.acquire(force=args.force)
    # 先建好（可能是个"什么都不做"的空日志器）：Ctrl+C 那条路径也要能安全地 close 它，
    # 而不是靠 locals() 去猜它有没有被赋值。
    tracker_log = _TrackLog(paths.log_file if config.log else None)
    tray = None  # 先置空：except/finally 里要能安全地看到它，哪怕 try 都没进
    try:
        store.meta_set(META_LAST_START, epoch_to_iso(now_epoch()))
        store.meta_set(META_LAST_CONFIG, json.dumps(config.to_dict(), ensure_ascii=False))
        _remember_tracker_pid(store)
        tracker_log.open(
            f"interval={config.interval:g}s idle={config.idle_threshold:g}s{' force' if args.force else ''}"
        )
        sampler = Sampler(
            provider=make_provider(),
            sink=store,
            config=config,
            on_event=None if args.quiet else _print_sample,
            on_error=tracker_log.write,
        )
        limit = 1 if args.once else args.max_samples
        stop_requested = threading.Event()
        tray = _start_tray(args, paths, config, stop_requested, tracker_log) if args.tray else None
        _out(f"开始记录（每隔 {config.interval:g}s 一次，空闲阈值 {config.idle_threshold:g}s）")
        _out(f"数据目录 {paths.home}；Ctrl+C 停止")
        if config.log:
            _out(f"日志     {paths.log_file}")
        if tray is not None:
            _out("托盘     已挂上（右键可暂停/看报告/退出）")
        summary = sampler.run(max_samples=limit, should_stop=stop_requested.is_set)
        if tray is not None:
            tray.stop()
        store.meta_set(META_LAST_STOP, epoch_to_iso(summary.stopped_at))
        _out(summary.describe())
        tracker_log.close(
            f"=== track 结束 · {summary.describe()} ==="
            if summary.stopped_by != "write-error"
            else f"!!! track 因写入失败停止 · {summary.describe()} ==="
        )
        if summary.stopped_by == "write-error":
            # 不是"记了一半"，是"根本没记进去"：给非零退出码，别让脚本以为一切正常。
            raise StoreError(
                f"连续 {MAX_CONSECUTIVE_WRITE_FAILURES} 次写入失败，已停止记录",
                hint="检查磁盘剩余空间与数据库是否被占用；之前写进去的数据没有被动过",
            )
        return EXIT_OK
    except KeyboardInterrupt:
        store.meta_set(META_LAST_STOP, epoch_to_iso(now_epoch()))
        if tray is not None:
            tray.stop()
        tracker_log.close("=== track 结束（Ctrl+C） ===")
        print(f"\n{'已停止（Ctrl+C）'}：数据都在 {paths.db}，随时 `{PROGRAM} report` 看结果。")
        return EXIT_INTERRUPTED
    finally:
        if tray is not None:
            tray.stop()  # 幂等：正常路径已经停过，这里只是兜底
        store.close()
        lock.release()


META_TRACKER_PIDS = "tracker_pids"
MAX_REMEMBERED_PIDS = 32


def _remember_tracker_pid(store: Store) -> None:
    """记下"我自己"的 PID：报告期把"盯着 timetrace 控制台看"那段从排名里剔除。

    按 PID 而不是按进程名，因为 tracker 就是以 `python.exe -m timetrace` 在跑，
    按名字排除会误伤用户自己的 python 进程。
    """
    existing = [item for item in (store.meta_get(META_TRACKER_PIDS) or "").split(",") if item.strip()]
    pids = [*existing, str(os.getpid())][-MAX_REMEMBERED_PIDS:]
    store.meta_set(META_TRACKER_PIDS, ",".join(dict.fromkeys(pids)))


def _tracker_pids(store: Store) -> tuple[int, ...]:
    raw = store.meta_get(META_TRACKER_PIDS) or ""
    out = []
    for item in raw.split(","):
        item = item.strip()
        if item.isdigit():
            out.append(int(item))
    return tuple(out)


def _print_sample(line: str) -> None:
    stamp = format_local(now_epoch(), "%H:%M:%S")
    print(f"  {stamp}  {line}", flush=True)


class _TrackLog:
    """track 的文件日志。

    只记**屏幕上看不见、数据库里也没有**的东西：生命周期、异常、写入失败。
    逐条采样不进日志 —— 那是数据库的活，每 5 秒一行会把文件撑到无用。

    写日志绝不能把记录本身带崩：所有 IO 异常一律吞掉。日志是旁证，不是主产物。
    """

    MAX_BYTES = 2 * 1024 * 1024  # 2MB 封顶：tracker 是常驻进程，不能让它把磁盘写满

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._handle = None

    def open(self, reason: str) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > self.MAX_BYTES:
                # 轮转成 .old，只留一份：日志的价值在"最近发生了什么"，不在考古。
                old = self.path.with_suffix(".log.old")
                old.unlink(missing_ok=True)
                self.path.rename(old)
            self._handle = self.path.open("a", encoding="utf-8", newline="\n")
            self.write(f"=== track 启动（pid {os.getpid()}）· {reason} ===")
        except OSError:
            self._handle = None

    def write(self, message: str) -> None:
        if self._handle is None:
            return
        try:
            self._handle.write(f"{format_local(now_epoch(), '%Y-%m-%d %H:%M:%S')}  {message}\n")
            self._handle.flush()
        except OSError:
            self._handle = None  # 日志盘挂了就别每条都重试，记录本身照常

    def close(self, message: str) -> None:
        self.write(message)
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


# ------------------------------------------------------------------ 托盘（M4）


def _start_tray(
    args: argparse.Namespace,
    paths: Paths,
    config: Config,
    stop_requested: threading.Event,
    tracker_log: _TrackLog,
):
    """挂托盘图标。**起不来就记一行日志继续采样** —— 图标是旁证，账才是主产物。

    每个回调都在托盘线程里跑，所以一律新开一个 SQLite 连接（WAL 允许并发），
    绝不碰主循环手里那个连接。
    """
    # 延迟 import：托盘是可选能力，不该让它的加载失败拖垮整个 CLI。
    from timetrace.tray import TrayActions, TrayIcon, TrayUnavailable

    def log(message: str) -> None:
        tracker_log.write(f"[tray] {message}")

    def open_report() -> None:
        report_store = _open_store(paths, create=False)
        try:
            data = _report_for(report_store, paths, parse_range("today"), config)
        finally:
            report_store.close()
        target = paths.report
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html_template.render_html(data), encoding="utf-8")
        os.startfile(str(target))
        log(f"打开了报告 {target}")

    def toggle_pause(paused: bool) -> str:
        pause_store = _open_store(paths)
        try:
            if paused:
                opened = pause_store.open_pause(now_epoch(), note="从托盘暂停")
                return f"已暂停（{format_local(opened.start_ts, '%H:%M:%S')} 起）"
            closed = pause_store.close_pause(now_epoch())
            if closed is None:
                return "本来就没有在暂停"
            return f"已恢复（暂停 {format_local(closed.start_ts, '%H:%M:%S')} 起）"
        finally:
            pause_store.close()

    def quit_tracking() -> None:
        stop_requested.set()
        log("收到托盘的退出指令，正在停止采样")

    tip = f"timetrace · 正在记录（每 {config.interval:g}s）"
    tray = TrayIcon(
        tip=tip,
        actions=TrayActions(open_report=open_report, toggle_pause=toggle_pause, quit=quit_tracking, log=log),
    )
    try:
        tray.start()
    except TrayUnavailable as cause:
        log(f"托盘没挂上：{cause}（继续用控制台记录）")
        _out(f"! 托盘没挂上（{cause}），继续用控制台记录")
        return None
    return tray


# ------------------------------------------------------------------ pause / resume


def cmd_pause(args: argparse.Namespace) -> int:
    paths = _paths(args).ensure()
    store = _open_store(paths)
    try:
        pause = store.open_pause(now_epoch(), args.note)
        note = f"（{pause.note}）" if pause.note else ""
        _out(f"已暂停记录{note}：从 {format_local(pause.start_ts, '%Y-%m-%d %H:%M:%S')} 起不再记录窗口。")
        _out(f"恢复：{PROGRAM} resume")
        return EXIT_OK
    finally:
        store.close()


def cmd_resume(args: argparse.Namespace) -> int:
    paths = _paths(args).ensure()
    store = _open_store(paths)
    try:
        closed = store.close_pause(now_epoch())
        if closed is None:
            _out("本来就没有在暂停。")
            return EXIT_OK
        start = format_local(closed.start_ts, "%H:%M:%S")
        end = format_local(closed.end_ts or now_epoch(), "%H:%M:%S")
        _out(f"已恢复记录（暂停区间 {start}–{end} 在报告里显示为「已暂停」）。")
        return EXIT_OK
    finally:
        store.close()


# ------------------------------------------------------------------ status


def _tracker_is_running(paths: Paths) -> bool:
    holder = InstanceLock(paths.lock).holder()
    if holder is None:
        return False
    return winapi.pid_alive(holder.pid) if sys.platform == "win32" else True


def cmd_status(args: argparse.Namespace) -> int:
    paths = _paths(args)
    _out(f"{PROGRAM} {__version__}")
    _out(f"数据目录 {paths.home}（{'存在' if paths.home.exists() else '还不存在'}）")
    _out(f"锁       {_lock_state(paths)}")
    if not paths.db.exists():
        _out("数据库   还没有。先跑 `timetrace track`。")
        return EXIT_OK
    store = Store(paths.db).open()
    try:
        stats = store.stats()
        first = format_local(stats["first_ts"]) if stats["first_ts"] else "—"
        last = format_local(stats["last_ts"]) if stats["last_ts"] else "—"
        size_kb = stats["size_bytes"] / 1024
        _out(f"数据库   {stats['path']}")
        _out(f"         采样 {stats['samples']} 条，{size_kb:.1f} KB，schema v{stats['schema_version']}")
        _out(f"         范围 {first} → {last}")
        # 这两个 meta 属于**不同的一次运行**：last_start 在本次 track 起来时就写了，
        # last_stop 还留着上一次的收尾时间。正在跑的时候把它们拼成"A → B"会得到
        # "18:31 → 18:28"这种结束早于开始的胡话 —— 所以先分清在不在跑。
        started_epoch = _epoch_of(store.meta_get(META_LAST_START))
        stopped_raw = store.meta_get(META_LAST_STOP)
        if _tracker_is_running(paths):
            _out(f"正在记录  从 {format_local(started_epoch)} 起（本进程还没写过 last_stop）")
        elif stopped_raw:
            stopped_epoch = _epoch_of(stopped_raw)
            if stopped_epoch < started_epoch:
                # 上次是被杀掉的（没来得及写 last_stop），别给出倒过来的时间对。
                _out(f"上次记录  {format_local(started_epoch)} 起，结束时间未知（进程没正常收尾）")
            else:
                _out(f"上次记录  {format_local(started_epoch)} → {format_local(stopped_epoch)}")
        else:
            _out(f"上次记录  {format_local(started_epoch)} 起，没有结束时间（可能被杀掉过）")
        pause = store.active_pause(now_epoch())
        _out(f"暂停状态 {'正在暂停中' if pause else '未暂停'}")
        _out("")
        _out(render_console(_today_report(store, paths)))
        return EXIT_OK
    finally:
        store.close()


def _epoch_of(iso_text: str | None) -> float:
    from timetrace.timeutil import iso_to_epoch

    return iso_to_epoch(iso_text) if iso_text else now_epoch()


def _report_for(store: Store, paths: Paths, rng: Range, config: Config, *, include_titles: bool = False):
    pauses = store.pauses(rng.start, rng.end)
    samples = list(store.iter_samples(rng.start, rng.end))
    segments = build_segments(
        samples,
        config=config,
        pauses=pauses,
        own_pids=_tracker_pids(store),
        excluded=config.exclude,
    )
    reference = now_epoch()
    return build_report(segments, rng=rng, config=config, now=reference, include_titles=include_titles)


def _today_report(store: Store, paths: Paths):
    config = _recorded_config(store) or Config()
    rng = parse_range("today")
    return _report_for(store, paths, rng, config)


# ------------------------------------------------------------------ report


def cmd_report(args: argparse.Namespace) -> int:
    paths = _paths(args)
    store = _open_store(paths, create=False)
    try:
        rng = parse_range(args.range_spec)
        config = _config_for_report(store, args, paths)
        data = _report_for(store, paths, rng, config, include_titles=args.titles)
        if args.json:
            print(render_json(data))
            return EXIT_OK
        if args.html:
            target = Path(args.out) if args.out else paths.report
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(html_template.render_html(data), encoding="utf-8")
            _out(f"HTML 报告：{target}（单文件，可直接发给别人看）")
        console_data = data
        if args.top and len(data.apps) > args.top:
            # 只截断 console 的列表：HTML/JSON 要保持完整
            head = list(data.apps[: args.top])
            # 尾部要保住两类行：专用桶（空闲/未记录…）和被排除的进程 ——
            # 少了它们，console 就跟 HTML/JSON 对不上了。
            tail = [row for row in data.apps[args.top :] if row.label or row.excluded]
            keep = head + tail
            console_data = replace(data, apps=keep)
        # 标题细分现在就在 data 里（三态同源），不再由 console 单独去数采样条数。
        _out(render_console(console_data))
        return EXIT_OK
    finally:
        store.close()


# ------------------------------------------------------------------ purge / export


def cmd_purge(args: argparse.Namespace) -> int:
    if not args.all and not args.range_spec:
        raise UsageError("要说清删什么", hint="用 --all 或 --range today|2026-09-01..2026-09-10")
    paths = _paths(args)
    store = _open_store(paths, create=False)
    try:
        rng = parse_range(args.range_spec) if not args.all else Range(None, None, "全部")
        if not args.yes:
            if not sys.stdin.isatty():
                raise UsageError(
                    "非交互环境下 purge 必须显式 --yes",
                    hint="这是故意的：删数据不能靠默认值",
                )
            preview = store.count_samples() if args.all else _count_in_range(store, rng)
            print(f"将永久删除 {preview} 条采样（{describe_range(rng)}），没有备份。")
            answer = input("确认请输入 yes：").strip().lower()
            if answer != "yes":
                _out("已取消，什么都没删。")
                return EXIT_OK
        removed = store.purge_all() if args.all else store.purge(rng.start, rng.end)
        pause_note = f"、{removed['pauses']} 段暂停" if removed.get("pauses") else ""
        _out(f"已删除 {removed['samples']} 条采样{pause_note}（VACUUM 完成，空间已归还）。")
        return EXIT_OK
    finally:
        store.close()


def _count_in_range(store: Store, rng: Range) -> int:
    return sum(1 for _ in store.iter_samples(rng.start, rng.end))


def cmd_export(args: argparse.Namespace) -> int:
    paths = _paths(args)
    store = _open_store(paths, create=False)
    try:
        rng = parse_range(args.range_spec)
        rows = list(store.export_rows(rng.start, rng.end, titles=not args.no_titles))
        pauses = store.export_pauses(rng.start, rng.end)
        # newline="" 先别翻译成 CRLF，write_text(newline="\\n") 再别翻译一次 ——
        # 否则 Windows 上每行会变成 \\r\\r\\n，用别的工具读出来行与行之间全是空行。
        buffer = io.StringIO(newline="")
        if args.fmt == "json":
            json.dump(
                {
                    "app": "timetrace",
                    "schema_version": 1,
                    "exported_at": epoch_to_iso(now_epoch()),
                    "range": {
                        "start": epoch_to_iso(rng.start) if rng.start else None,
                        "end": epoch_to_iso(rng.end) if rng.end else None,
                    },
                    "titles_included": not args.no_titles,
                    "rows": rows,
                    # 暂停区间是独立的一张表，采样行只能近似它 —— 导出必须两边都带，
                    # 才担得起 README 里那句"export 之后可以放心 purge"。
                    "pauses": pauses,
                },
                buffer,
                ensure_ascii=False,
                indent=2,
            )
        else:
            # CSV 是扁平的采样表，暂停以 kind=paused 的心跳行存在（备注在 note 列）。
            writer = csv.DictWriter(buffer, fieldnames=["ts", "exe", "title", "idle_s", "kind", "note"])
            writer.writeheader()
            writer.writerows(rows)
        text = buffer.getvalue()
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8", newline="\n")
            pause_note = f"、暂停区间 {len(pauses)} 段" if args.fmt == "json" else ""
            _out(
                f"已导出 {len(rows)} 行到 {args.out}"
                f"（标题{'已去掉' if args.no_titles else '包含在内'}{pause_note}）"
            )
        else:
            print(text)
        if args.pauses_out:
            _write_pauses_csv(args.pauses_out, pauses)
            _out(f"已导出 {len(pauses)} 段暂停区间到 {args.pauses_out}")
        elif args.fmt == "csv" and pauses:
            _out(
                f"! CSV 里只有 {len(pauses)} 段暂停的采样心跳（备注在 note 列），"
                "精确起止会随 purge 一起消失 —— 要完整保留请用 --pauses-out 或 --format json"
            )
        return EXIT_OK
    finally:
        store.close()


def _write_pauses_csv(path: str, pauses: list[dict[str, object]]) -> None:
    """暂停区间单独一份 CSV：采样表是扁平的，塞不下"一段有起止和备注的时间"。"""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=["start", "end", "note", "open"])
    writer.writeheader()
    for pause in pauses:
        writer.writerow(pause)
    Path(path).write_text(buffer.getvalue(), encoding="utf-8", newline="\n")


# ------------------------------------------------------------------ autostart


def startup_dir() -> Path:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    raise PlatformError("autostart 目前只实现了 Windows", hint="Linux 可以自己写一个 systemd --user 单元")


def autostart_log(home: Path) -> Path:
    return home / "logs" / "autostart.log"


def last_autostart_launch(home: Path) -> str | None:
    """读自启日志的最后一行：这是"上次开机到底拉起来没有"的唯一客观证据。"""
    try:
        lines = [line.strip() for line in autostart_log(home).read_text(encoding="utf-8").splitlines()]
    except OSError:
        return None
    # 反向找第一条非空行：日志尾部多几个空行（或 CRLF 被文本模式翻倍）不该让证据消失。
    return next((line for line in reversed(lines) if line), None)


def launcher_lines(home: Path, *, tray: bool = False) -> list[str]:
    """生成启动脚本内容：只写当前用户的启动目录，不动注册表、不需要管理员。

    启动前先往 `logs/autostart.log` 追加一行：开机自启最常见的失败方式是**静默地什么都没做**，
    有了这行日志，`autostart --show` 和 `doctor` 就能回答"上次开机它到底起来过没有"。
    """
    import timetrace as package

    package_dir = Path(package.__file__).resolve()
    log_dir = home / "logs"
    log_file = autostart_log(home)
    lines = ["@echo off", "rem 由 timetrace 生成：开机自动开始记录。删除本文件即可取消。"]
    if "site-packages" not in str(package_dir):
        src = package_dir.parents[1]
        lines.append(f'set "PYTHONPATH={src};%PYTHONPATH%"')
    lines.append(f'if not exist "{log_dir}" mkdir "{log_dir}"')
    lines.append(f'>> "{log_file}" echo %date% %time% autostart launched')
    # 自启的 tracker 没有控制台可看，日志是唯一能回答"它到底怎么了"的地方 —— 一律打开。
    tray_flag = " --tray" if tray else ""
    lines.append(
        f'start "timetrace" /min "{sys.executable}" -m timetrace track --log{tray_flag} --home "{home}"'
    )
    return lines


def cmd_autostart(args: argparse.Namespace) -> int:
    paths = _paths(args).ensure()
    directory = startup_dir()
    target = directory / AUTOSTART_NAME
    if args.show:
        _out(f"启动目录 {directory}")
        _out(f"文件     {target}（{'存在' if target.exists() else '不存在'}）")
        if target.exists():
            _out("内容：")
            for line in target.read_text(encoding="utf-8").splitlines():
                _out(f"  {line}")
        launch = last_autostart_launch(paths.home)
        _out(f"上次自启 {launch or '还没有记录（要么没开机自启过，要么还没写日志）'}")
        return EXIT_OK
    if args.disable:
        if not target.exists():
            _out("自启本来就没有配置。")
            return EXIT_OK
        target.unlink()
        _out(f"已删除 {target}（下次开机不会再自动记录）。")
        return EXIT_OK
    directory.mkdir(parents=True, exist_ok=True)
    lines = launcher_lines(paths.home, tray=args.tray)
    target.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    _out(f"已启用开机自启：{target}")
    for line in lines:
        _out(f"  {line}")
    if args.tray:
        _out("托盘     开机后会挂上一个图标（右键可暂停/看报告/退出）")
    _out(f"启动日志 {autostart_log(paths.home)}（每次开机拉起都会追加一行，下次开机后用 --show 验证）")
    _out("查看：autostart --show；取消：autostart --disable")
    return EXIT_OK


# ------------------------------------------------------------------ doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    paths = _paths(args)
    checks: list[tuple[str, str, str]] = []  # (级别, 项目, 说明)

    supported, reason = winapi.platform_supported()
    checks.append(("ok" if supported else "fail", "平台", "Windows" if supported else reason))

    if supported:
        probe = Sampler(provider=make_provider(), sink=_NullSink(), config=Config())
        try:
            warm = probe.sample_once()
            started = time.perf_counter()
            for _ in range(20):
                probe.sample_once()
            per_ms = (time.perf_counter() - started) * 1000 / 20
            checks.append(
                ("ok", "前台窗口", f"{warm.exe or '（无窗口）'} · {warm.title[:40] or '（无标题）'}")
            )
            checks.append(("ok", "空闲秒数", f"{warm.idle_s:.1f}s"))
            level = "ok" if per_ms < 5 else "warn"
            checks.append((level, "采样开销", f"平均 {per_ms:.2f}ms/次（目标 < 2ms，警戒线 5ms）"))
        except TimetraceError as error:
            checks.append(("fail", "前台窗口", error.message))

    config = _effective_config(args, paths)
    checks.append(("ok", "采样间隔", f"{config.interval:g}s（来源：{'、'.join(config.sources)}）"))
    checks.append(("ok", "空闲阈值", f"{config.idle_threshold:g}s"))
    checks.append(("ok", "记录标题", "是" if config.titles else "否（--no-titles）"))
    checks.append(
        (
            "ok",
            "排除进程",
            ", ".join(config.exclude) if config.exclude else "无",
        )
    )
    # 目标与规则是"写在 config.json 里、跑起来才知道读没读到"的东西 ——
    # doctor 的职责就是回答"为什么没按我写的来"，所以这两行必须打出来。
    if config.goals:
        rendered = ", ".join(f"{exe} {hours:g}h" for exe, hours in config.goals)
        checks.append(("ok", "目标", rendered))
    else:
        checks.append(("ok", "目标", "未配置（报告里不出现目标区）"))
    if config.max_stretch_minutes > 0:
        checks.append(("ok", "规则提醒", f"连续没离开 ≥ {config.max_stretch_minutes:g} 分钟"))
    else:
        checks.append(("ok", "规则提醒", "未配置（报告里不出现提醒区）"))

    if paths.db.exists():
        try:
            store = Store(paths.db).open()
            try:
                stats = store.stats()
                checks.append(
                    (
                        "ok",
                        "数据库",
                        f"{stats['samples']} 条 · {stats['size_bytes'] / 1024:.1f} KB · {paths.db}",
                    )
                )
            finally:
                store.close()
        except StoreError as error:
            checks.append(("fail", "数据库", error.message))
    else:
        checks.append(("warn", "数据库", f"还没有：{paths.db}"))

    holder = InstanceLock(paths.lock).holder()
    alive = holder is not None and (winapi.pid_alive(holder.pid) if sys.platform == "win32" else True)
    checks.append(("ok" if not alive else "warn", "实例锁", _lock_state(paths)))

    if supported:
        target = startup_dir() / AUTOSTART_NAME
        launch = last_autostart_launch(paths.home)
        if target.exists():
            detail = f"已配置（{target}）"
            detail += f"；上次拉起 {launch}" if launch else "；还没有拉起记录（重启一次即可验证）"
            checks.append(("ok", "开机自启", detail))
        else:
            checks.append(("warn", "开机自启", f"未配置（{target}）"))

    if args.range_spec:
        _append_range_health(checks, paths, args.range_spec, config)

    _out(f"{PROGRAM} doctor · 数据目录 {paths.home}")
    for level, name, detail in checks:
        mark = {"ok": "√", "warn": "!", "fail": "x"}[level]
        _out(f"  {mark} {name:<10} {detail}")
    failed = sum(1 for level, _, _ in checks if level == "fail")
    warned = sum(1 for level, _, _ in checks if level == "warn")
    _out("")
    _out(f"{len(checks) - failed - warned} 项正常，{warned} 项需注意，{failed} 项失败")
    return EXIT_RUNTIME if failed else EXIT_OK


def _append_range_health(checks: list[tuple[str, str, str]], paths: Paths, spec: str, config: Config) -> None:
    """把某个区间的记录质量也体检一遍。

    `doctor --range today` 就是规格书 §3 场景 D 要的那件事：怀疑"未记录 40 分钟"时，
    一条命令同时给出覆盖率、最大采样间隔与异常采样数。
    """
    if not paths.db.exists():
        checks.append(("warn", "区间体检", "还没有数据库，先跑 timetrace track"))
        return
    try:
        rng = parse_range(spec)
        store = Store(paths.db).open()
        try:
            samples = list(store.iter_samples(rng.start, rng.end))
            segments = build_segments(
                samples,
                config=config,
                pauses=store.pauses(rng.start, rng.end),
                own_pids=_tracker_pids(store),
                excluded=config.exclude,
            )
            data = build_report(segments, rng=rng, config=config, now=now_epoch())
        finally:
            store.close()
    except TimetraceError as error:
        checks.append(("fail", "区间体检", error.message))
        return
    if not samples:
        checks.append(("warn", rng.label, "区间内没有采样：tracker 那时可能没在跑"))
        return
    coverage_level = "ok" if data.coverage >= 0.5 else "warn"
    checks.append(
        (coverage_level, f"{rng.label}覆盖率", f"{format_percent(data.coverage)}（{len(samples)} 条采样）")
    )
    max_interval = data.quality["max_interval"]
    checks.append(
        (
            "ok" if max_interval <= config.gap_limit else "warn",
            f"{rng.label}最大间隔",
            f"{format_duration(max_interval)}（超过 {format_duration(config.gap_limit)} 算断档）",
        )
    )
    errors = int(data.quality["errors"])
    discarded = int(data.quality["discarded"])
    checks.append(
        (
            "ok" if errors + discarded == 0 else "warn",
            f"{rng.label}异常采样",
            f"{errors + discarded} 条（拿不到窗口 {errors}、时间倒退 {discarded}）",
        )
    )


class _NullSink:
    """doctor 用：只测"能不能采样"，不落库。签名必须与 SampleSink 一致。"""

    def insert_sample(self, sample: Sample) -> None:
        return None

    def active_pause(self, now: float) -> Pause | None:
        return None


if __name__ == "__main__":
    raise SystemExit(main())

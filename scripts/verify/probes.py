#!/usr/bin/env python3
"""验收探针：只做 pytest 做不到的事（真 Windows API、真子进程、真退出码、真计时）。

每个子命令输出一行 JSON：`{"ok": true, ...}` 或 `{"ok": false, "error": "..."}`。
verify_mvp.py 负责调用与判定，这里只负责"动手做、如实报"。

    python scripts/verify/probes.py windows|store|lock|privacy|e2e|exitcodes
"""

from __future__ import annotations

import ast
import json
import os
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover
        pass

NETWORK_MODULES = (
    "socket",
    "urllib",
    "http",
    "requests",
    "httpx",
    "ftplib",
    "smtplib",
    "telnetlib",
    "xmlrpc",
)


# ------------------------------------------------------------------ 公共


def cli(
    args: list[str], *, home: Path | None = None, timeout: float = 60.0, **kwargs
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "timetrace", *args]
    if home is not None:
        command += ["--home", str(home)]
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONPATH"] = str(SRC)
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=timeout,
        **kwargs,
    )


# ------------------------------------------------------------------ 20-1 windows


def probe_windows() -> dict[str, object]:
    from timetrace import winapi

    supported, reason = winapi.platform_supported()
    if not supported:
        return {"skipped": True, "reason": reason}
    winapi.foreground_window()  # 预热（首次加载 DLL）
    winapi.idle_seconds()
    durations = []
    for _ in range(150):
        started = time.perf_counter()
        info = winapi.foreground_window()
        idle = winapi.idle_seconds()
        durations.append((time.perf_counter() - started) * 1000)
    ordered = sorted(durations)
    # 回绕算术：纯函数，这里顺手在真环境里再验一次
    wrapped = winapi.tick_delta(597, 0xFFFFFFFF - 400)
    return {
        "pid": info.pid,
        "exe": info.exe,
        "title_len": len(info.title),
        "idle_s": round(idle, 3),
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[int(len(ordered) * 0.95) - 1], 3),
        "tick_wrap_delta": wrapped,
    }


# ------------------------------------------------------------------ 20-2 store


def probe_store() -> dict[str, object]:
    from timetrace.domain import Sample
    from timetrace.paths import Paths
    from timetrace.store import Store

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix="timetrace-store-") as tmp:
        paths = Paths(Path(tmp)).ensure()
        store = Store(paths.db).initialize()
        try:
            now = time.time()
            rows = [
                Sample(
                    ts=now + index * 5,
                    pid=100 + index % 3,
                    exe=f"app{index % 4}.exe",
                    title=f"窗口 {index}",
                    idle_s=float(index % 7),
                )
                for index in range(500)
            ]
            written = store.insert_samples(rows)
            read_back = store.samples()
            schema_version = store.meta_get("schema_version")
            exported = list(store.export_rows(None, None, titles=True))
            removed = store.purge()
            after = store.count_samples()
        finally:
            store.close()

        # 损坏的文件必须被拒绝，而且不能被改动
        broken = paths.home / "broken.db"
        payload = b"not a database at all" * 20
        broken.write_bytes(payload)
        try:
            Store(broken).initialize()
            rejected = False
        except Exception:
            rejected = True
        untouched = broken.read_bytes() == payload

        return {
            "written": written,
            "read_back": len(read_back),
            "roundtrip_ok": read_back[0].exe == rows[0].exe and abs(read_back[0].ts - rows[0].ts) < 0.002,
            "schema_version": schema_version,
            "exported_rows": len(exported),
            "purged": removed["samples"],
            "after_purge": after,
            "corrupt_rejected": rejected,
            "corrupt_untouched": untouched,
        }


# ------------------------------------------------------------------ 20-3 lock


def probe_lock() -> dict[str, object]:
    from timetrace.store import InstanceLock

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix="timetrace-lock-") as tmp:
        home = Path(tmp)
        log = (home / "first.log").open("w", encoding="utf-8")
        first = subprocess.Popen(
            [sys.executable, "-m", "timetrace", "track", "--quiet", "--interval", "1", "--home", str(home)],
            env={**os.environ, "PYTHONPATH": str(SRC), "PYTHONIOENCODING": "utf-8"},
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.time() + 20
            lock_file = home / "lock"
            while time.time() < deadline and not lock_file.exists():
                time.sleep(0.1)
            holder = InstanceLock(lock_file).holder()
            second = cli(["track", "--quiet"], home=home, timeout=30)
            forced = cli(["track", "--once", "--quiet", "--force"], home=home, timeout=30)
        finally:
            first.terminate()
            try:
                first.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                first.kill()
            log.close()
            first_log = (home / "first.log").read_text(encoding="utf-8", errors="replace")[:400]

        # 陈旧锁：写一个不存在的 PID，应该被自动接管
        lock_file.write_text(
            json.dumps({"pid": 999_999, "started_at": "昨天", "exe_path": "py"}), encoding="utf-8"
        )
        stale = cli(["track", "--once", "--quiet"], home=home, timeout=30)
        return {
            "holder_pid": holder.pid if holder else None,
            "second_exit": second.returncode,
            "second_mentions_pid": str(holder.pid) in (second.stderr or "") if holder else False,
            "force_exit": forced.returncode,
            "stale_exit": stale.returncode,
            "first_log": first_log,
            "second_stderr": (second.stderr or "")[:200],
        }


# ------------------------------------------------------------------ 20-7 privacy


def probe_privacy() -> dict[str, object]:
    imported: list[str] = []
    for path in sorted((SRC / "timetrace").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
    offenders = sorted({name for name in imported if name.split(".")[0] in NETWORK_MODULES})

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix="timetrace-privacy-") as tmp:
        home = Path(tmp)
        cli(["track", "--once", "--quiet", "--no-titles"], home=home, timeout=30)
        import sqlite3

        connection = sqlite3.connect(home / "timetrace.db")
        titles = [row[0] for row in connection.execute("SELECT title FROM samples")]
        connection.close()
        report = cli(["report", "--range", "all", "--json"], home=home, timeout=60)
        payload = report.stdout or ""
        leaked = [
            needle for needle in (str(home), os.environ.get("USERNAME", "\0")) if needle and needle in payload
        ]
        exported = cli(["export", "--range", "all", "--no-titles"], home=home, timeout=60)
        exported_titles = [
            row.get("title", "") for row in json.loads(exported.stdout or "{}").get("rows", [])
        ]
    return {
        "imported_modules": len(set(imported)),
        "network_offenders": offenders,
        "no_titles_all_empty": all(title == "" for title in titles) and len(titles) > 0,
        "title_rows": len(titles),
        "export_titles_empty": all(title == "" for title in exported_titles),
        "leaked_in_report": leaked,
        "report_rows": len(json.loads(payload or "{}").get("apps", [])),
    }


# ------------------------------------------------------------------ 20-8 e2e


def probe_e2e() -> dict[str, object]:
    from timetrace.cli import META_TRACKER_PIDS
    from timetrace.config import Config
    from timetrace.report import build_report
    from timetrace.segment import build_segments
    from timetrace.store import Store
    from timetrace.timeutil import parse_range

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix="timetrace-e2e-") as tmp:
        home = Path(tmp)
        started = time.time()
        result = cli(
            ["track", "--quiet", "--interval", "1", "--max-samples", "7"],
            home=home,
            timeout=90,
        )
        elapsed = time.time() - started
        if result.returncode not in (0,):
            return {"ok": False, "error": f"track 退出码 {result.returncode}: {result.stderr[:200]}"}

        store = Store(home / "timetrace.db").open()
        try:
            samples = list(store.iter_samples(None, None))
            tracker_pids = [
                int(item) for item in (store.meta_get(META_TRACKER_PIDS) or "").split(",") if item.isdigit()
            ]
        finally:
            store.close()

        config = Config(interval=1.0)
        segments = build_segments(samples, config=config, own_pids=tuple(tracker_pids))
        data = build_report(segments, rng=parse_range("all"), config=config, now=time.time())
        conservation = sum(data.totals.values())
        console = cli(["report", "--range", "all"], home=home, timeout=60)
        cli(
            ["report", "--range", "all", "--html", "--out", str(home / "r.html")],
            home=home,
            timeout=60,
        )
        html_text = (home / "r.html").read_text(encoding="utf-8") if (home / "r.html").exists() else ""
    return {
        "track_exit": result.returncode,
        "track_seconds": round(elapsed, 1),
        "samples": len(samples),
        "distinct_exe": sorted({item.exe for item in samples if item.exe}),
        "all_exe_nonempty": all(item.exe for item in samples),
        "segments": len(segments.segments),
        "conservation_delta": round(abs(conservation - data.range_seconds), 3),
        "coverage": round(data.coverage, 4),
        "console_ok": "timetrace ·" in (console.stdout or ""),
        "html_ok": html_text.startswith("<!doctype html>") and "<script" not in html_text,
    }


# ------------------------------------------------------------------ 20-9 exit codes


def probe_exitcodes() -> dict[str, object]:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True, prefix="timetrace-exit-") as tmp:
        home = Path(tmp)
        ok = cli(["track", "--once", "--quiet"], home=home, timeout=60)
        usage = cli(["report", "--range", "前天"], home=home, timeout=60)
        missing_data = cli(["report", "--range", "all"], home=Path(tmp) / "nope", timeout=60)
        version = cli(["--version"], timeout=30)
        bad_command = cli(["nonsense"], timeout=30)

        # 130：真发 Ctrl+Break 事件（Windows）或 SIGINT
        tracker = subprocess.Popen(
            [sys.executable, "-m", "timetrace", "track", "--quiet", "--interval", "1", "--home", str(home)],
            env={**os.environ, "PYTHONPATH": str(SRC), "PYTHONIOENCODING": "utf-8"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
        time.sleep(3.5)
        try:
            if sys.platform == "win32":
                tracker.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                tracker.send_signal(signal.SIGINT)
        except (OSError, ValueError):
            tracker.terminate()
        try:
            tracker.wait(timeout=20)
        except subprocess.TimeoutExpired:
            tracker.kill()
            tracker.wait()

    return {
        "ok_exit": ok.returncode,
        "usage_exit": usage.returncode,
        "usage_mentions_range": "区间" in (usage.stderr or ""),
        "missing_data_exit": missing_data.returncode,
        "missing_data_hint": "还没有数据" in (missing_data.stderr or ""),
        "version_exit": version.returncode,
        "version_text": (version.stdout or "").strip(),
        "bad_command_exit": bad_command.returncode,
        "interrupt_exit": tracker.returncode,
    }


# ------------------------------------------------------------------ 入口

RUNNERS = {
    "windows": probe_windows,
    "store": probe_store,
    "lock": probe_lock,
    "privacy": probe_privacy,
    "e2e": probe_e2e,
    "exitcodes": probe_exitcodes,
}


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    runner = RUNNERS.get(name)
    if runner is None:
        print(f"未知探针：{name}（可选：{', '.join(RUNNERS)}）", file=sys.stderr)
        return 2
    try:
        payload = runner() or {}
        payload.pop("skipped", None)
        print(json.dumps({"ok": True, **payload}, ensure_ascii=False))
        return 0
    except Exception as cause:
        print(json.dumps({"ok": False, "error": f"{type(cause).__name__}: {cause}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

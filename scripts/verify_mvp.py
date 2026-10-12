#!/usr/bin/env python3
"""timetrace · MVP 完成定义逐条核验（DEVELOPMENT.md §20）。

    python scripts/verify_mvp.py [--skip-tests] [--quick]

每条标准打印 PASS / SKIP / FAIL，最后一行是总计。退出码：全绿 0，有 FAIL 1。
20-11（真实 dogfood）不自动跑：它需要人真的用一段时间，脚本只读 §24.6 的登记，
绝不拿别的东西冒充它通过了。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PYTHON = sys.executable


def dev_python() -> str:
    """ruff / pyright 装在 venv 里，别指望"正在跑脚本的那个解释器"也有。"""
    for candidate in (ROOT / ".venv" / "Scripts" / "python.exe", ROOT / ".venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate)
    return PYTHON


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover
        pass

RESULTS: list[tuple[str, str, str]] = []

# §15 软目标（2026-09-23 按实测校准过；见 §24.4 的说明）
TARGET = {
    "p95_ms": 5.0,
    "tracker_rss_mb": 40.0,
    "day_write_s": 8.0,
    "month_report_s": 2.0,
    "bytes_per_row": 200.0,
}


def record(status: str, name: str, detail: str = "") -> None:
    RESULTS.append((status, name, detail))
    print(f"{status:4} {name:36} {detail}")


def run(
    command: list[str], *, timeout: float = 600.0, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONPATH"] = str(SRC)
    if env_extra:
        environment.update(env_extra)
    return subprocess.run(
        command,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=environment,
    )


def pytest_file(name: str) -> tuple[str, str]:
    result = run([PYTHON, "-m", "pytest", f"tests/{name}"], timeout=300)
    text = result.stdout + result.stderr
    passed = int(match.group(1)) if (match := re.search(r"(\d+) passed", text)) else 0
    failed = int(match.group(1)) if (match := re.search(r"(\d+) failed", text)) else 0
    skipped = int(match.group(1)) if (match := re.search(r"(\d+) skipped", text)) else 0
    timing = f"{float(match.group(1)):.2f}s" if (match := re.search(r"in ([\d.]+)s", text)) else "?"
    if result.returncode != 0 or failed:
        first_failure = next((line for line in text.splitlines() if line.startswith("FAILED")), "")
        return "FAIL", f"{failed} 个用例失败 {first_failure[:140]}"
    suffix = f"（另有 {skipped} 个跳过）" if skipped else ""
    return "PASS", f"{passed} 个用例全绿（{timing}）{suffix}"


def probe(name: str) -> dict[str, object]:
    result = run([PYTHON, str(ROOT / "scripts" / "verify" / "probes.py"), name], timeout=600)
    line = (result.stdout or "").strip().splitlines()[-1] if (result.stdout or "").strip() else ""
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"探针 {name} 没有输出 JSON：{result.stdout[:200]} {result.stderr[:200]}"
        ) from None
    if not payload.get("ok"):
        raise RuntimeError(f"探针 {name} 自报失败：{payload.get('error')}")
    return payload


def guard(name: str, action) -> None:
    try:
        status, detail = action()
    except Exception as cause:
        status, detail = "FAIL", f"{type(cause).__name__}: {cause}"
    record(status, name, detail)


# ------------------------------------------------------------------ 各项


def check_windows() -> tuple[str, str]:
    payload = probe("windows")
    if payload.get("skipped"):
        return "SKIP", f"非 Windows：{payload.get('reason')}"
    if payload.get("tick_wrap_delta") != 998:
        return "FAIL", f"tick 回绕算错：{payload.get('tick_wrap_delta')}（应为 998）"
    timing = f"P50 {payload.get('p50_ms')}ms / P95 {payload.get('p95_ms')}ms"
    return (
        "PASS",
        f"真取到 {payload.get('exe') or '（无窗口）'}（pid {payload.get('pid')}）· "
        f"空闲 {payload.get('idle_s')}s · {timing} · 回绕→998",
    )


def check_store() -> tuple[str, str]:
    payload = probe("store")
    problems = []
    if payload.get("written") != payload.get("read_back"):
        problems.append("写入与读回条数不一致")
    if not payload.get("roundtrip_ok"):
        problems.append("字段往返不一致")
    if payload.get("exported_rows") != payload.get("written"):
        problems.append("导出条数与写入不一致")
    if payload.get("after_purge") != 0 or payload.get("purged") != payload.get("written"):
        problems.append("purge 没有真删干净")
    if not payload.get("corrupt_rejected"):
        problems.append("损坏的库没被拒绝")
    if not payload.get("corrupt_untouched"):
        problems.append("损坏的库被改动了")
    if problems:
        return "FAIL", "；".join(problems)
    return (
        "PASS",
        f"{payload.get('written')} 条往返一致 · VACUUM 后剩 {payload.get('after_purge')} 条 · "
        f"坏库被拒且原文件未动 · schema v{payload.get('schema_version')}",
    )


def check_lock() -> tuple[str, str]:
    payload = probe("lock")
    if payload.get("second_exit") != 3:
        stderr = str(payload.get("second_stderr"))[:120]
        return "FAIL", f"第二个实例退出码 {payload.get('second_exit')}，应为 3；stderr={stderr}"
    if not payload.get("second_mentions_pid"):
        return "FAIL", "拒绝时没有告诉用户是谁占着锁"
    if payload.get("force_exit") != 0:
        return "FAIL", "--force 没能接管"
    if payload.get("stale_exit") != 0:
        return "FAIL", "陈旧锁没有被自动回收"
    pid = payload.get("holder_pid")
    return "PASS", f"第二个实例被拒（退出 {payload['second_exit']}，报出 PID {pid}）· stale/force 都能接管"


def check_privacy() -> tuple[str, str]:
    payload = probe("privacy")
    if payload.get("network_offenders"):
        return "FAIL", f"import 图里出现网络模块：{payload['network_offenders']}"
    if not payload.get("no_titles_all_empty"):
        return "FAIL", "--no-titles 仍然写了标题"
    if not payload.get("export_titles_empty"):
        return "FAIL", "export --no-titles 仍然带标题"
    if payload.get("leaked_in_report"):
        return "FAIL", f"报告里出现了本机信息：{payload['leaked_in_report']}"
    return (
        "PASS",
        f"{payload.get('imported_modules')} 个 import 里 0 个网络模块 · --no-titles 全空 · "
        f"报告不含路径与用户名",
    )


def check_e2e() -> tuple[str, str]:
    payload = probe("e2e")
    problems = []
    if payload.get("samples", 0) < 5:
        problems.append(f"只采到 {payload.get('samples')} 条")
    if not payload.get("all_exe_nonempty"):
        problems.append("有采样拿不到进程名")
    if payload.get("conservation_delta", 1) > 0.01:
        problems.append(f"保恒律偏差 {payload.get('conservation_delta')}s")
    if not payload.get("console_ok"):
        problems.append("console 报告没生成")
    if not payload.get("html_ok"):
        problems.append("HTML 报告没生成或带了脚本")
    if problems:
        return "FAIL", "；".join(problems)
    apps = ",".join(payload.get("distinct_exe") or [])
    return (
        "PASS",
        f"真跑 {payload.get('track_seconds')}s 采到 {payload.get('samples')} 条（{apps}）"
        f"· 守恒偏差 {payload.get('conservation_delta')}s · console+HTML 均可读",
    )


def check_exitcodes() -> tuple[str, str]:
    payload = probe("exitcodes")
    expected = {
        "ok_exit": 0,
        "usage_exit": 2,
        "missing_data_exit": 1,
        "version_exit": 0,
        "bad_command_exit": 2,
        "interrupt_exit": 130,
    }
    wrong = {key: payload.get(key) for key, value in expected.items() if payload.get(key) != value}
    if wrong:
        return "FAIL", f"退出码不符：{wrong}（期望 {expected}）"
    if not payload.get("usage_mentions_range"):
        return "FAIL", "用法错误没提示可用区间"
    if not payload.get("missing_data_hint"):
        return "FAIL", "缺数据时没给出下一步"
    return "PASS", "0/1/2/3/130 全部符合契约（含真 Ctrl+Break → 130）"


def check_perf(quick: bool) -> tuple[str, str]:
    args = [PYTHON, str(ROOT / "scripts" / "bench_overhead.py"), "--json"]
    if quick:
        args.append("--quick")
    result = run(args, timeout=900)
    metrics = json.loads(result.stdout)
    sampling = metrics.get("sampling") or {}
    write = metrics["write"]
    problems = []

    p95 = float(sampling.get("p95_ms", 0.0))
    if sampling and p95 > TARGET["p95_ms"]:
        problems.append(f"采样 P95 {p95}ms > {TARGET['p95_ms']}ms")
    rows = float(write["rows"])
    day_seconds = float(write["per_row_ms"]) * 17280 / 1000
    if day_seconds > TARGET["day_write_s"]:
        problems.append(f"一天写入推算 {day_seconds:.2f}s > {TARGET['day_write_s']}s")
    bytes_per_row = float(write["disk_kb"]) * 1024 / rows if rows else 9999
    if bytes_per_row > TARGET["bytes_per_row"]:
        problems.append(f"{bytes_per_row:.0f} 字节/行 > {TARGET['bytes_per_row']} 字节/行")
    rss = float(metrics.get("tracker_rss_mb", 0.0))
    if rss > TARGET["tracker_rss_mb"]:
        problems.append(f"tracker 内存 {rss}MB > {TARGET['tracker_rss_mb']}MB")
    month = metrics.get("report_thirty_days")
    if month:
        month_total = float(month["month_total_s"])
        if month_total > TARGET["month_report_s"]:
            problems.append(f"30 天报告 {month_total}s > {TARGET['month_report_s']}s")
    # 最坏情况：段数 == 采样数（标题/应用频繁变，_merge 合不掉）。
    # 常规合成数据的段会大量合并，只测它会漏掉 build_report 真正的热路径。
    worst = metrics.get("report_worst_case")
    if worst:
        worst_build = float(worst["worst_build_s"])
        if worst_build > TARGET["month_report_s"]:
            problems.append(f"30 天最坏情况 build_report {worst_build}s > {TARGET['month_report_s']}s")

    summary = (
        f"采样 P95 {sampling.get('p95_ms', '—')}ms · 写入 {day_seconds:.2f}s/天 · "
        f"{bytes_per_row:.0f} 字节/行 · tracker {rss}MB · "
        + (
            f"30 天报告 {month['month_total_s']}s/{int(month['month_rows'])} 行"
            if month
            else "30 天报告：--quick 跳过"
        )
        + (f" · 最坏 {worst['worst_build_s']}s/{int(worst['worst_rows'])} 段" if worst else "")
    )
    if problems:
        return "FAIL", "；".join(problems) + f"（{summary}）"
    return "PASS", summary


def check_dogfood() -> tuple[str, str]:
    spec = ROOT / "DEVELOPMENT.md"
    text = spec.read_text(encoding="utf-8") if spec.exists() else ""
    # §24.1 的登记表里有 20-11 这一行，§24.6 是它的详细实录
    section = text.split("### 24.1", 1)[1].split("### 24.2", 1)[0] if "### 24.1" in text else ""
    row = re.search(r"^\|\s*20-11\s*\|(.*)\|", section, re.MULTILINE)
    cell = row.group(1).strip() if row else ""
    recorded = "☑" in cell and "### 24.6" in text
    if recorded:
        return "SKIP", "真实 dogfood 已登记在 §24.1 + §24.6（本脚本不代替人使用软件）"
    return "SKIP", "待 §24.6 登记：需要真人用一段时间，脚本无法代替"


def check_gate() -> tuple[str, str]:
    interpreter = dev_python()
    steps = [
        ([interpreter, "-m", "ruff", "check", "src", "tests", "scripts"], "ruff check"),
        ([interpreter, "-m", "ruff", "format", "--check", "src", "tests", "scripts"], "ruff format"),
        ([interpreter, "-m", "pyright"], "pyright"),
    ]
    failures = []
    for command, label in steps:
        try:
            result = run(command, timeout=300)
        except FileNotFoundError:
            failures.append(f"{label} 没装（dev extras 缺失）")
            continue
        if result.returncode != 0:
            tail = (result.stdout + result.stderr).strip().splitlines()
            failures.append(f"{label}: {tail[0][:120] if tail else '失败'}")
    if failures:
        return "FAIL", "；".join(failures)
    return "PASS", "ruff check / ruff format --check / pyright 全绿（pytest 由 20-1…20-5 覆盖）"


# ------------------------------------------------------------------ 主流程


def main() -> int:
    parser = argparse.ArgumentParser(description="timetrace MVP 验收 §20")
    parser.add_argument("--skip-tests", action="store_true", help="跳过 pytest（check.ps1 已单独跑过）")
    parser.add_argument("--quick", action="store_true", help="跳过 30 天报告那段 bench")
    args = parser.parse_args()

    print(f"timetrace · §20 验收（{ROOT}）")
    print(f"python={sys.version.split()[0]}")

    units = [
        ("20-1 winapi", "test_winapi.py", check_windows),
        ("20-2 store", "test_store.py", check_store),
        ("20-3 lock", "test_store.py", check_lock),
        ("20-4 segment", "test_segment.py", None),
        ("20-5 report", "test_report.py", None),
        ("20-6 html", "test_report.py", None),
    ]
    for name, file, extra in units:
        if args.skip_tests and extra is None:
            record("SKIP", name, "check.ps1 已单独跑过 pytest")
            continue
        status, detail = pytest_file(file)
        if status == "FAIL" or extra is None:
            record(status, name, detail)
            continue
        try:
            extra_status, extra_detail = extra()
        except Exception as cause:
            extra_status, extra_detail = "FAIL", f"{type(cause).__name__}: {cause}"
        record(extra_status, f"{name} + 真机", f"{detail}；{extra_detail}")

    guard("20-7 privacy", check_privacy)
    guard("20-8 e2e_real", check_e2e)
    guard("20-9 exit_codes", check_exitcodes)
    guard("20-10 perf", lambda: check_perf(args.quick))
    guard("20-11 dogfood", check_dogfood)
    guard("20-12 quality_gate", check_gate)

    passed = sum(1 for status, _, _ in RESULTS if status == "PASS")
    skipped = sum(1 for status, _, _ in RESULTS if status == "SKIP")
    failed = sum(1 for status, _, _ in RESULTS if status == "FAIL")
    print()
    print(f"{passed} PASS / {skipped} SKIP / {failed} FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

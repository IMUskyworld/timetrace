"""Visible control window opened from the tray icon."""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from timetrace.paths import Paths
from timetrace.store import InstanceLock


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    home = parser.parse_args(argv).home
    paths = Paths.resolve(home).ensure()
    root = tk.Tk()
    root.title("timetrace 控制面板")
    root.geometry("360x210")
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=20)
    frame.pack(fill="both", expand=True)
    state = tk.StringVar(value="正在记录")
    ttk.Label(frame, text="timetrace", font=("Segoe UI", 16, "bold")).pack(anchor="w")
    ttk.Label(frame, textvariable=state).pack(anchor="w", pady=(8, 14))

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--worker", *args, "--home", str(paths.home)]
        else:
            command = [sys.executable, "-m", "timetrace", *args, "--home", str(paths.home)]
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def action(name: str) -> None:
        result = cli(name)
        if result.returncode:
            messagebox.showerror("timetrace", result.stderr or result.stdout or "操作失败", parent=root)
        else:
            state.set((result.stdout or "").strip().splitlines()[0])

    buttons = ttk.Frame(frame)
    buttons.pack(anchor="w")
    ttk.Button(buttons, text="暂停", command=lambda: action("pause")).pack(side="left", padx=(0, 6))
    ttk.Button(buttons, text="恢复", command=lambda: action("resume")).pack(side="left", padx=(0, 6))

    def report() -> None:
        result = cli("report", "--range", "today", "--html")
        if result.returncode:
            messagebox.showerror("报告生成失败", result.stderr or result.stdout, parent=root)
        else:
            os.startfile(str(paths.report))

    ttk.Button(buttons, text="今日报告", command=report).pack(side="left", padx=(0, 6))

    def stop() -> None:
        paths.stop_request.write_text("stop\n", encoding="ascii")
        state.set("正在保存并停止记录…")
        root.after(300, wait_for_stop)

    def wait_for_stop() -> None:
        holder = InstanceLock(paths.lock).holder()
        alive = holder is not None and InstanceLock(paths.lock)._pid_alive(holder.pid)
        if alive:
            root.after(300, wait_for_stop)
        else:
            state.set("已停止")

    ttk.Button(buttons, text="停止记录", command=stop).pack(side="left")
    ttk.Label(frame, text=str(Path(paths.home)), foreground="#666").pack(anchor="w", pady=(14, 0))
    root.mainloop()

"""数据目录与文件路径。

开发约束 §22.4：所有路径都从这里来，不要依赖 CWD —— 常驻进程的 CWD 可能是任何地方。
优先级：`--home` > `$TIMETRACE_HOME` > 平台默认。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

HOME_ENV = "TIMETRACE_HOME"
DB_FILENAME = "timetrace.db"
LOCK_FILENAME = "lock"
CONFIG_FILENAME = "config.json"
REPORT_FILENAME = "report.html"
LOG_DIRNAME = "logs"
LOG_FILENAME = "track.log"


def default_home() -> Path:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        root = Path(local) if local else Path.home() / "AppData" / "Local"
        return root / "timetrace"
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / "timetrace"


def resolve_home(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    from_env = os.environ.get(HOME_ENV)
    if from_env:
        return Path(from_env).expanduser()
    return default_home()


@dataclass(frozen=True)
class Paths:
    home: Path

    @classmethod
    def resolve(cls, explicit: str | os.PathLike[str] | None = None) -> Paths:
        return cls(resolve_home(explicit))

    @property
    def db(self) -> Path:
        return self.home / DB_FILENAME

    @property
    def lock(self) -> Path:
        return self.home / LOCK_FILENAME

    @property
    def config(self) -> Path:
        return self.home / CONFIG_FILENAME

    @property
    def report(self) -> Path:
        return self.home / REPORT_FILENAME

    @property
    def log_dir(self) -> Path:
        return self.home / LOG_DIRNAME

    @property
    def log_file(self) -> Path:
        return self.log_dir / LOG_FILENAME

    def ensure(self) -> Paths:
        self.home.mkdir(parents=True, exist_ok=True)
        return self

    def describe(self) -> str:
        return str(self.home)

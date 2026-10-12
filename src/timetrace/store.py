"""SQLite 存储层：schema、写入、查询、暂停区间、清理、导出，以及单实例锁。

只有这个模块碰数据库；上层（segment / report）拿到的永远是 dataclass。
两条纪律：
1. **打不开/版本不对/文件损坏时，绝不覆盖** —— 数据是用户的，宁可报错让他自己处置；
2. **每条采样一次 INSERT**（autocommit），进程被杀不会留下半条记录。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from timetrace.domain import Pause, Sample
from timetrace.errors import LockBusy, StoreError
from timetrace.timeutil import epoch_to_iso, iso_to_epoch, now_epoch

SCHEMA_VERSION = 2
# v1 早期曾把单调钟 mono 落库，后来确认"写进来从来没人读"就删了字段（见 known-deviations #4），
# 但当时忘了动 schema 版本 —— 于是同为 v1 却有两种表形状，老库一写就报
# NOT NULL constraint failed: samples.mono。v2 起把这个历史包袱真正清掉。
LEGACY_SAMPLE_COLUMNS = ("mono",)


class StoreStats(TypedDict):
    path: str
    samples: int
    pauses: int
    first_ts: float | None
    last_ts: float | None
    size_bytes: int
    schema_version: str | None


META_SCHEMA_VERSION = "schema_version"
META_CREATED_AT = "created_at"
META_LAST_START = "last_start"
META_LAST_STOP = "last_stop"
META_LAST_CONFIG = "last_config"

_SAMPLE_COLUMNS = ("ts", "pid", "exe", "title", "idle_s", "kind", "note")
_INSERT_SAMPLE_SQL = (
    f"INSERT INTO samples({', '.join(_SAMPLE_COLUMNS)}) VALUES({', '.join('?' * len(_SAMPLE_COLUMNS))})"
)
_OPEN_PAUSE_SQL = (
    "SELECT id, start_ts, end_ts, note FROM pauses WHERE end_ts IS NULL ORDER BY start_ts DESC LIMIT 1"
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT    NOT NULL,
    pid    INTEGER NOT NULL,
    exe    TEXT    NOT NULL,
    title  TEXT    NOT NULL,
    idle_s REAL    NOT NULL,
    kind   TEXT    NOT NULL,
    note   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE TABLE IF NOT EXISTS pauses (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts TEXT NOT NULL,
    end_ts   TEXT,
    note     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pauses_start ON pauses(start_ts);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None

    # -------------------------------------------------------------- 生命周期

    def open(self) -> Store:
        if self._connection is not None:
            return self
        try:
            connection = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        except sqlite3.Error as cause:
            raise StoreError(f"打不开数据库 {self.path}：{cause}") from cause
        connection.row_factory = sqlite3.Row
        for pragma in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL", "PRAGMA busy_timeout=5000"):
            try:
                connection.execute(pragma)
            except sqlite3.DatabaseError as cause:
                connection.close()
                raise StoreError(
                    f"{self.path} 不像是 timetrace 的数据库（{cause}）",
                    hint="文件没有被改动；换一个 --home，或先把它手工移走",
                ) from cause
        self._connection = connection
        return self

    def initialize(self) -> Store:
        """建表 / 校验版本。版本比我们新就拒绝启动（不猜、不迁移）。"""
        self.open()
        connection = self.connection
        try:
            connection.executescript(_SCHEMA_SQL)
            row = connection.execute(
                "SELECT value FROM meta WHERE key = ?", (META_SCHEMA_VERSION,)
            ).fetchone()
        except sqlite3.DatabaseError as cause:
            raise StoreError(f"初始化失败：{cause}") from cause
        if row is not None and int(row["value"]) > SCHEMA_VERSION:
            raise StoreError(
                f"数据库版本 {row['value']} 比这个 timetrace（{SCHEMA_VERSION}）新",
                hint="升级 timetrace 再打开；数据没有被改动",
            )
        # v1 是唯一比我们旧的版本，而它相对 v2 的差别只有"多了个 mono 列"。
        # 我们不修任意旧 schema —— 只做一件已知无损的事：删掉那列（没有就什么都不做）。
        if row is not None and int(row["value"]) < SCHEMA_VERSION and int(row["value"]) != 1:
            raise StoreError(
                f"数据库版本 {row['value']} 太旧，这个版本只认 {SCHEMA_VERSION}",
                hint="先 export 再换数据目录；不做猜测式迁移",
            )
        migrated = self._drop_legacy_sample_columns(connection)
        if row is None:
            self.meta_set(META_CREATED_AT, epoch_to_iso(now_epoch()))
        if migrated or row is None or int(row["value"]) != SCHEMA_VERSION:
            self.meta_set(META_SCHEMA_VERSION, str(SCHEMA_VERSION))
        return self

    def _drop_legacy_sample_columns(self, connection: sqlite3.Connection) -> bool:
        """清掉 v1 早期残留的 `mono`：这一列从没被读过，删掉零信息损失。

        只处理这一个已知列；表里出现别的意外列一律不动（那属于"不猜"的边界）。
        """
        found = {item[1] for item in connection.execute("PRAGMA table_info(samples)")}
        targets = [name for name in LEGACY_SAMPLE_COLUMNS if name in found]
        if not targets:
            return False
        try:
            for name in targets:
                connection.execute(f"ALTER TABLE samples DROP COLUMN {name}")
        except sqlite3.DatabaseError as cause:
            raise StoreError(
                f"清理旧字段失败：{cause}",
                hint="这个 SQLite 不支持 DROP COLUMN；先 export 再换一个数据目录",
            ) from cause
        return True

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> Store:
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StoreError("数据库还没打开", hint="先调用 open() / initialize()")
        return self._connection

    # -------------------------------------------------------------- meta

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -------------------------------------------------------------- 采样

    def insert_sample(self, sample: Sample) -> None:
        self.connection.execute(_INSERT_SAMPLE_SQL, _sample_row(sample))

    def insert_samples(self, samples: Iterable[Sample]) -> int:
        rows = [_sample_row(sample) for sample in samples]
        if not rows:
            return 0
        with self.connection:
            self.connection.executemany(_INSERT_SAMPLE_SQL, rows)
        return len(rows)

    def count_samples(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) AS n FROM samples").fetchone()["n"])

    def time_span(self) -> tuple[float | None, float | None]:
        row = self.connection.execute("SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM samples").fetchone()
        if not row or row["lo"] is None:
            return None, None
        return iso_to_epoch(row["lo"]), iso_to_epoch(row["hi"])

    def iter_samples(self, start: float | None = None, end: float | None = None) -> Iterator[Sample]:
        """流式读采样：30 天有 50 万行，全量 materialize 会吃掉几十 MB。"""
        sql = f"SELECT {', '.join(_SAMPLE_COLUMNS)} FROM samples"
        clauses, params = [], []
        if start is not None:
            clauses.append("ts >= ?")
            params.append(epoch_to_iso(start))
        if end is not None:
            clauses.append("ts < ?")
            params.append(epoch_to_iso(end))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts"
        for row in self.connection.execute(sql, params):
            yield _row_to_sample(row)

    def samples(self, start: float | None = None, end: float | None = None) -> list[Sample]:
        return list(self.iter_samples(start, end))

    # -------------------------------------------------------------- 暂停

    def active_pause(self, now: float) -> Pause | None:
        row = self.connection.execute(_OPEN_PAUSE_SQL).fetchone()
        if not row:
            return None
        pause = _row_to_pause(row)
        return pause if pause.covers(now) else None

    def open_pause(self, now: float, note: str = "") -> Pause:
        """开始暂停。已经在暂停中就原样返回（幂等，不堆重复行）。"""
        existing = self.active_pause(now)
        if existing is not None:
            return existing
        self.connection.execute(
            "INSERT INTO pauses(start_ts, end_ts, note) VALUES(?, NULL, ?)", (epoch_to_iso(now), note)
        )
        return Pause(start_ts=now, end_ts=None, note=note)

    def close_pause(self, now: float) -> Pause | None:
        row = self.connection.execute(_OPEN_PAUSE_SQL).fetchone()
        if not row:
            return None
        self.connection.execute("UPDATE pauses SET end_ts = ? WHERE id = ?", (epoch_to_iso(now), row["id"]))
        return Pause(start_ts=iso_to_epoch(row["start_ts"]), end_ts=now, note=row["note"])

    def pauses(self, start: float | None = None, end: float | None = None) -> list[Pause]:
        rows = self.connection.execute(
            "SELECT start_ts, end_ts, note FROM pauses ORDER BY start_ts"
        ).fetchall()
        out = []
        for row in rows:
            pause = _row_to_pause(row)
            if start is not None and (pause.end_ts or pause.start_ts) <= start:
                continue
            if end is not None and pause.start_ts >= end:
                continue
            out.append(pause)
        return out

    # -------------------------------------------------------------- 清理与导出

    def purge(
        self, start: float | None = None, end: float | None = None, *, vacuum: bool = True
    ) -> dict[str, int]:
        """真删（DELETE + VACUUM），不做软删、不留备份。"""
        if start is None and end is None:
            return self.purge_all(vacuum=vacuum)
        clauses, params = [], []
        if start is not None:
            clauses.append("ts >= ?")
            params.append(epoch_to_iso(start))
        if end is not None:
            clauses.append("ts < ?")
            params.append(epoch_to_iso(end))
        where = " AND ".join(clauses)
        removed = self.connection.execute(f"DELETE FROM samples WHERE {where}", params).rowcount
        # 暂停区间也要一起删：只删采样会留下孤儿 pauses，报告里还会凭空出现「已暂停」。
        # 判据与 `pauses()` 的筛选口径一致：pause 落在区间内或跨在边界上都算重叠。
        pause_clauses, pause_params = [], []
        if start is not None:
            pause_clauses.append("(end_ts IS NULL OR end_ts > ?)")
            pause_params.append(epoch_to_iso(start))
        if end is not None:
            pause_clauses.append("start_ts < ?")
            pause_params.append(epoch_to_iso(end))
        pause_where = " AND ".join(pause_clauses)
        removed_pauses = self.connection.execute(
            f"DELETE FROM pauses WHERE {pause_where}", pause_params
        ).rowcount
        if vacuum:
            self.vacuum()
        return {"samples": int(removed or 0), "pauses": int(removed_pauses or 0)}

    def purge_all(self, *, vacuum: bool = True) -> dict[str, int]:
        samples = self.connection.execute("DELETE FROM samples").rowcount or 0
        pauses = self.connection.execute("DELETE FROM pauses").rowcount or 0
        if vacuum:
            self.vacuum()
        return {"samples": int(samples), "pauses": int(pauses)}

    def vacuum(self) -> None:
        # VACUUM 不能在事务里跑；autocommit 模式下直接执行即可。
        self.connection.execute("VACUUM")

    def export_rows(
        self, start: float | None, end: float | None, *, titles: bool = True
    ) -> Iterator[dict[str, object]]:
        for sample in self.iter_samples(start, end):
            yield {
                "ts": epoch_to_iso(sample.ts),
                "exe": sample.exe,
                "title": sample.title if titles else "",
                "idle_s": round(sample.idle_s, 3),
                "kind": sample.kind,
                # 暂停备注在 kind=paused 的采样行上，导出必须带上 —— 否则
                # "export 之后放心 purge" 对 pause 是假的（备注会丢）。
                "note": sample.note if titles else "",
            }

    def export_pauses(self, start: float | None = None, end: float | None = None) -> list[dict[str, object]]:
        """导出暂停区间：精确的起止 + 备注，采样行只能近似（每 interval 一条心跳）。"""
        return [
            {
                "start": epoch_to_iso(pause.start_ts),
                "end": epoch_to_iso(pause.end_ts) if pause.end_ts is not None else None,
                "note": pause.note,
                "open": pause.is_open,
            }
            for pause in self.pauses(start, end)
        ]

    # -------------------------------------------------------------- 统计

    def stats(self) -> StoreStats:
        first, last = self.time_span()
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        return {
            "path": str(self.path),
            "samples": self.count_samples(),
            "pauses": int(self.connection.execute("SELECT COUNT(*) AS n FROM pauses").fetchone()["n"]),
            "first_ts": first,
            "last_ts": last,
            "size_bytes": size,
            "schema_version": self.meta_get(META_SCHEMA_VERSION),
        }


def _sample_row(sample: Sample) -> tuple[object, ...]:
    return (
        epoch_to_iso(sample.ts),
        int(sample.pid),
        sample.exe,
        sample.title,
        float(sample.idle_s),
        sample.kind,
        sample.note,
    )


def _row_to_sample(row: sqlite3.Row) -> Sample:
    return Sample(
        ts=iso_to_epoch(row["ts"]),
        pid=int(row["pid"]),
        exe=str(row["exe"]),
        title=str(row["title"]),
        idle_s=float(row["idle_s"]),
        kind=str(row["kind"]),
        note=str(row["note"]),
    )


def _row_to_pause(row: sqlite3.Row) -> Pause:
    end = row["end_ts"]
    return Pause(
        start_ts=iso_to_epoch(row["start_ts"]),
        end_ts=iso_to_epoch(end) if end else None,
        note=str(row["note"]),
    )


# ------------------------------------------------------------------ 单实例锁


@dataclass(frozen=True, slots=True)
class LockHolder:
    pid: int
    started_at: str
    exe_path: str

    def describe(self) -> str:
        return f"PID {self.pid}（启动于 {self.started_at}）"


class InstanceLock:
    """`track` 的单实例锁：文件里写自己的 PID，别人的 PID 还活着就拒绝启动。

    "还活着"用 PID 判断，所以 Windows 复用 PID 时理论上会误判 —— 那种情况下
    用户的出路是 `--force`（并接受重复计数的风险）。
    """

    def __init__(self, path: Path, *, pid_alive: Callable[[int], bool] | None = None) -> None:
        self.path = path
        self._pid_alive = pid_alive or _default_pid_alive
        self._mine: LockHolder | None = None

    def holder(self) -> LockHolder | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            data = json.loads(raw)
            return LockHolder(
                pid=int(data["pid"]), started_at=str(data["started_at"]), exe_path=str(data["exe_path"])
            )
        except (ValueError, KeyError, TypeError):
            return None  # 垃圾锁文件当陈旧锁处理

    def acquire(self, *, force: bool = False) -> LockHolder:
        existing = self.holder()
        if existing is not None and not force and self._pid_alive(existing.pid):
            raise LockBusy(
                f"已经有一个 timetrace 在记录了：{existing.describe()}",
                holder_pid=existing.pid,
                hint="先停掉它；确实要接管就加 --force（两个实例同时跑会重复计数）",
            )
        holder = LockHolder(
            pid=os.getpid(),
            started_at=epoch_to_iso(now_epoch()),
            exe_path=sys.executable,
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "pid": holder.pid,
                "started_at": holder.started_at,
                "exe_path": holder.exe_path,
            }
            self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as cause:
            raise StoreError(f"写不了锁文件 {self.path}：{cause}") from cause
        self._mine = holder
        return holder

    def release(self) -> bool:
        """只删自己写的那把锁：--force 接管后不该去删别人的。"""
        mine = self._mine
        if mine is None:
            return False
        current = self.holder()
        self._mine = None
        if current is not None and current.pid != mine.pid:
            return False
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False


def _default_pid_alive(pid: int) -> bool:
    from timetrace import winapi

    try:
        return winapi.pid_alive(pid)
    except Exception:
        # 平台 API 用不了时保守地当作"活着"：宁可让用户加 --force，也不冒重复计数的风险
        return True

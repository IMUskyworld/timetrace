"""store：schema、往返、范围查询、暂停、清理、导出，以及"坏数据绝不被覆盖"。"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from builders import at, sample
from timetrace.domain import Sample
from timetrace.errors import LockBusy, StoreError
from timetrace.store import (
    META_SCHEMA_VERSION,
    SCHEMA_VERSION,
    InstanceLock,
    LockHolder,
    Store,
)
from timetrace.timeutil import epoch_to_iso

DB = "timetrace.db"


def make_store(home: Path) -> Store:
    return Store(home / DB).initialize()


# ------------------------------------------------------------------ schema


def test_初始化建表并写入版本(home_dir: Path):
    with make_store(home_dir) as store:
        assert store.meta_get(META_SCHEMA_VERSION) == str(SCHEMA_VERSION)
        assert store.meta_get("created_at")
        assert store.count_samples() == 0
        tables = {
            row["name"]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"meta", "samples", "pauses"} <= tables


def test_重复初始化是幂等的(home_dir: Path):
    with make_store(home_dir) as store:
        store.insert_sample(sample(at("10:00"), "code.exe"))
    again = make_store(home_dir)
    with again:
        assert again.count_samples() == 1


def test_打开不存在的文件会建空库(home_dir: Path):
    with make_store(home_dir) as store:
        assert store.count_samples() == 0
        assert store.time_span() == (None, None)


def test_损坏的数据库文件被拒绝且不被改动(home_dir: Path):
    path = home_dir / DB
    payload = b"this is definitely not a sqlite database" * 10
    path.write_bytes(payload)
    with pytest.raises(StoreError) as caught:
        Store(path).initialize()
    assert "不像" in str(caught.value)
    assert path.read_bytes() == payload, "原文件必须一动不动"


def test_版本比我们新就拒绝启动(home_dir: Path):
    store = make_store(home_dir)
    store.meta_set(META_SCHEMA_VERSION, str(SCHEMA_VERSION + 1))
    store.insert_sample(sample(at("10:00"), "code.exe"))
    store.close()
    with pytest.raises(StoreError) as caught:
        Store(home_dir / DB).initialize()
    assert "比这个 timetrace" in str(caught.value)
    # 数据还在
    with Store(home_dir / DB).open() as raw:
        assert raw.count_samples() == 1


def test_版本太旧也拒绝而不是猜(home_dir: Path):
    path = home_dir / DB
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO meta(key, value) VALUES('schema_version', '0');"
    )
    connection.commit()
    connection.close()
    with pytest.raises(StoreError, match="太旧"):
        Store(path).initialize()


# ------------------------------------------------------------------ 采样往返


def test_采样往返保留全部字段与毫秒(home_dir: Path):
    original = Sample(
        ts=at("10:00:05") + 0.123,
        pid=4242,
        exe="code.exe",
        title="main.py — 项目",
        idle_s=7.25,
        kind="active",
        note="备注",
    )
    with make_store(home_dir) as store:
        store.insert_sample(original)
        [loaded] = store.samples()
    assert loaded.ts == pytest.approx(original.ts, abs=0.001)
    assert loaded.pid == 4242
    assert loaded.exe == "code.exe"
    assert loaded.title == "main.py — 项目"
    assert loaded.idle_s == pytest.approx(7.25)
    assert loaded.note == "备注"


def test_范围查询左闭右开且按时间排序(home_dir: Path):
    with make_store(home_dir) as store:
        store.insert_samples(
            [
                sample(at("11:00"), "b.exe"),
                sample(at("10:00"), "a.exe"),
                sample(at("12:00"), "c.exe"),
            ]
        )
        rows = store.samples(at("10:00"), at("12:00"))
        assert [row.exe for row in rows] == ["a.exe", "b.exe"]
        assert [row.exe for row in store.samples(None, None)] == ["a.exe", "b.exe", "c.exe"]
        assert [row.exe for row in store.samples(at("11:00"), None)] == ["b.exe", "c.exe"]


def test_count_与_time_span(home_dir: Path):
    with make_store(home_dir) as store:
        store.insert_samples([sample(at("09:00"), "a.exe"), sample(at("17:30"), "b.exe")])
        assert store.count_samples() == 2
        first, last = store.time_span()
        assert first == pytest.approx(at("09:00"), abs=0.001)
        assert last == pytest.approx(at("17:30"), abs=0.001)


def test_insert_samples_支持空列表(home_dir: Path):
    with make_store(home_dir) as store:
        assert store.insert_samples([]) == 0


# ------------------------------------------------------------------ 暂停


def test_pause_开与关(home_dir: Path):
    with make_store(home_dir) as store:
        opened = store.open_pause(at("14:00"), "开会")
        assert opened.is_open and opened.note == "开会"
        assert store.active_pause(at("14:30")) is not None
        assert store.active_pause(at("13:59")) is None, "开始之前不算暂停"

        closed = store.close_pause(at("14:45"))
        assert closed is not None and closed.end_ts == pytest.approx(at("14:45"), abs=0.001)
        assert store.active_pause(at("14:30")) is None
        assert store.active_pause(at("15:00")) is None


def test_pause_重复开始是幂等的(home_dir: Path):
    with make_store(home_dir) as store:
        first = store.open_pause(at("14:00"), "开会")
        second = store.open_pause(at("14:05"), "又点了一次")
        assert second.start_ts == first.start_ts
        assert len(store.pauses()) == 1


def test_pause_没开过就关返回_None(home_dir: Path):
    with make_store(home_dir) as store:
        assert store.close_pause(at("14:00")) is None


def test_pauses_按区间过滤(home_dir: Path):
    with make_store(home_dir) as store:
        store.open_pause(at("10:00"), "上午")
        store.close_pause(at("10:30"))
        store.open_pause(at("15:00"), "下午")
        store.close_pause(at("15:20"))
        hit = store.pauses(at("09:00"), at("12:00"))
        assert [pause.note for pause in hit] == ["上午"]
        assert len(store.pauses()) == 2


# ------------------------------------------------------------------ 清理与导出


def test_purge_按区间真删(home_dir: Path):
    with make_store(home_dir) as store:
        store.insert_samples([sample(at(clock), "a.exe") for clock in ("09:00", "10:00", "11:00", "12:00")])
        removed = store.purge(at("10:00"), at("12:00"))
        assert removed == {"samples": 2, "pauses": 0}
        assert [row.ts for row in store.samples()] == pytest.approx([at("09:00"), at("12:00")], abs=0.001)


def test_purge_区间内的暂停也一起删(home_dir: Path):
    """只删采样会留下孤儿 pauses —— 报告里会凭空出现「已暂停」，"真删"就成了半句真话。"""
    with make_store(home_dir) as store:
        store.insert_samples([sample(at(clock), "a.exe") for clock in ("09:00", "10:00", "11:00", "12:00")])
        store.open_pause(at("10:10"), "开会")
        store.close_pause(at("10:20"))
        store.open_pause(at("08:00"), "区间外")
        store.close_pause(at("08:05"))
        removed = store.purge(at("10:00"), at("12:00"))
        assert removed == {"samples": 2, "pauses": 1}
        assert [pause.note for pause in store.pauses()] == ["区间外"]


def test_purge_跨边界的暂停算重叠(home_dir: Path):
    """区间是 [10:00, 12:00)：跨进来的、跨出去的、整个包住的都算重叠；正好从 12:00 开始的不算。"""
    with make_store(home_dir) as store:
        for left, right, note in (
            ("09:00", "09:30", "完全在外"),
            ("09:50", "10:10", "从前面跨进来"),
            ("09:50", "13:00", "跨过整个区间"),
            ("11:50", "12:10", "向后跨出去"),
            ("12:00", "12:05", "从边界开始"),
        ):
            store.open_pause(at(left), note)
            store.close_pause(at(right))
        removed = store.purge(at("10:00"), at("12:00"))
        assert removed["pauses"] == 3
        assert [pause.note for pause in store.pauses()] == ["完全在外", "从边界开始"]


def test_purge_all_清空并_vacuum(home_dir: Path):
    with make_store(home_dir) as store:
        store.insert_samples([sample(at("10:00"), "a.exe") for _ in range(200)])
        store.open_pause(at("11:00"))
        before = store.path.stat().st_size
        removed = store.purge_all()
        assert removed == {"samples": 200, "pauses": 1}
        assert store.count_samples() == 0
        assert store.pauses() == []
        assert store.path.stat().st_size <= before


def test_export_rows_可以去掉标题(home_dir: Path):
    with make_store(home_dir) as store:
        store.insert_sample(sample(at("10:00"), "code.exe", title="机密项目"))
        with_titles = list(store.export_rows(None, None, titles=True))
        without = list(store.export_rows(None, None, titles=False))
    assert with_titles[0]["title"] == "机密项目"
    assert without[0]["title"] == ""
    assert with_titles[0]["exe"] == without[0]["exe"] == "code.exe"
    assert with_titles[0]["ts"].endswith("Z")


def test_export_带上暂停区间与备注(home_dir: Path):
    """ "export 之后可以放心 purge" 必须对 pause 也成立：备注和精确起止都不能丢。"""
    with make_store(home_dir) as store:
        store.insert_sample(sample(at("10:00"), "code.exe", note=""))
        store.open_pause(at("10:10"), "开会")
        store.close_pause(at("10:20"))
        list(store.export_rows(None, None, titles=True))
        pauses = store.export_pauses()
        assert pauses == [
            {
                "start": epoch_to_iso(at("10:10")),
                "end": epoch_to_iso(at("10:20")),
                "note": "开会",
                "open": False,
            }
        ]
        assert pauses[0]["start"].endswith("Z")


def test_export_暂停备注跟采样行走(home_dir: Path):
    with make_store(home_dir) as store:
        store.insert_sample(sample(at("10:10"), "", kind="paused", note="开会"))
        rows = list(store.export_rows(None, None, titles=True))
        assert rows[0]["kind"] == "paused"
        assert rows[0]["note"] == "开会"
        private = list(store.export_rows(None, None, titles=False))
        assert private[0]["note"] == "", "--no-titles 连备注一起去掉"


def test_stats_给出可读概况(home_dir: Path):
    with make_store(home_dir) as store:
        store.insert_samples([sample(at("10:00"), "a.exe"), sample(at("11:00"), "b.exe")])
        stats = store.stats()
    assert stats["samples"] == 2
    assert stats["schema_version"] == str(SCHEMA_VERSION)
    assert stats["size_bytes"] > 0
    assert stats["first_ts"] == pytest.approx(at("10:00"), abs=0.001)


def test_没打开就查询会报错(home_dir: Path):
    with pytest.raises(StoreError, match="还没打开"):
        Store(home_dir / DB).count_samples()


# ------------------------------------------------------------------ 单实例锁

ALIVE = 4242
DEAD = 999_999


def lock_at(home_dir: Path, alive: set[int] | None = None, *, exe_path: str = "py") -> InstanceLock:
    """造一把锁。默认注入的 exe_path_of 对活着的 PID 返回同一个程序 ——

    不注入的话会打到真 winapi：测试机上万一 4242 真被某个进程占用，
    "活着但程序对不上"会把拒绝启动的测试变成接管，flaky。
    """
    living = alive if alive is not None else {ALIVE}
    return InstanceLock(
        home_dir / "lock",
        pid_alive=lambda pid: pid in living,
        exe_path_of=lambda pid: exe_path if pid in living else "",
    )


def test_拿锁写文件并释放(home_dir: Path):
    lock = lock_at(home_dir)
    holder = lock.acquire()
    assert holder.pid == os.getpid()
    assert (home_dir / "lock").exists()
    assert lock.holder() == holder
    assert lock.release() is True
    assert not (home_dir / "lock").exists()
    assert lock.holder() is None


def test_别人的锁还活着就拒绝启动(home_dir: Path):
    path = home_dir / "lock"
    path.write_text(
        json.dumps({"pid": ALIVE, "started_at": "2026-09-23T01:00:00.000Z", "exe_path": "py"}),
        encoding="utf-8",
    )
    lock = lock_at(home_dir)
    with pytest.raises(LockBusy) as caught:
        lock.acquire()
    assert caught.value.exit_code == 3
    assert caught.value.holder_pid == ALIVE
    assert "--force" in (caught.value.hint or "")
    assert "PID 4242" in str(caught.value)


def test_陈旧锁会被自动接管(home_dir: Path):
    path = home_dir / "lock"
    path.write_text(json.dumps({"pid": DEAD, "started_at": "昨天", "exe_path": "py"}), encoding="utf-8")
    lock = lock_at(home_dir)
    holder = lock.acquire()
    assert holder.pid != DEAD
    assert lock.holder() == holder


def test_force_可以强行接管(home_dir: Path):
    path = home_dir / "lock"
    path.write_text(json.dumps({"pid": ALIVE, "started_at": "刚才", "exe_path": "py"}), encoding="utf-8")
    lock = lock_at(home_dir)
    holder = lock.acquire(force=True)
    assert holder.pid != ALIVE


def test_PID被别的程序回收时接管而不是拒绝(home_dir: Path):
    """2026-10-14 审计修复：PID 活着但程序对不上 = 尸体锁，接管。

    旧行为只看"PID 活着"就拒绝启动 —— 上一个 tracker 崩了之后 PID 被
    回收，用户会被一个尸体锁永远挡在门外，只能靠 --force 自救。
    """
    path = home_dir / "lock"
    path.write_text(
        json.dumps({"pid": ALIVE, "started_at": "2026-09-23T01:00:00.000Z", "exe_path": "py"}),
        encoding="utf-8",
    )
    lock = InstanceLock(
        home_dir / "lock",
        pid_alive=lambda pid: pid in {ALIVE},
        exe_path_of=lambda pid: r"C:\Program Files\Google\Chrome\chrome.exe" if pid == ALIVE else "",
    )
    holder = lock.acquire()
    assert holder.pid != ALIVE
    assert lock.holder() == holder


def test_PID活着但查不到路径时保守拒绝(home_dir: Path):
    """权限不够查不了路径时不猜：宁可让用户 --force，不冒重复计数的风险。"""
    path = home_dir / "lock"
    path.write_text(json.dumps({"pid": ALIVE, "started_at": "刚才", "exe_path": "py"}), encoding="utf-8")
    lock = InstanceLock(
        home_dir / "lock",
        pid_alive=lambda pid: pid in {ALIVE},
        exe_path_of=lambda pid: "",
    )
    with pytest.raises(LockBusy):
        lock.acquire()


def test_PID活着且同一个程序才拒绝_路径大小写不敏感(home_dir: Path):
    """Windows 路径不区分大小写：C:\\App\\PYTHON.EXE 与 c:\\app\\python.exe 是同一个。"""
    path = home_dir / "lock"
    path.write_text(
        json.dumps({"pid": ALIVE, "started_at": "刚才", "exe_path": r"C:\App\PYTHON.EXE"}),
        encoding="utf-8",
    )
    lock = InstanceLock(
        home_dir / "lock",
        pid_alive=lambda pid: pid in {ALIVE},
        exe_path_of=lambda pid: r"c:\app\python.exe" if pid == ALIVE else "",
    )
    with pytest.raises(LockBusy):
        lock.acquire()


def test_venv重定向下同一个解释器仍算在跑(home_dir: Path):
    """venv 的 python.exe 是重定向器：内核镜像是基础解释器，锁里记的是 venv 路径。

    全路径比对会把**在跑的实例**误判成"PID 被回收"直接接管 —— 2026-10-14
    闸门 20-3 真机探针当场抓到过（第二个实例接管了锁，跑满 30 秒超时）。
    所以比基名；这个测试钉住那个教训。
    """
    path = home_dir / "lock"
    path.write_text(
        json.dumps({"pid": ALIVE, "started_at": "刚才", "exe_path": r"C:\proj\.venv\Scripts\python.exe"}),
        encoding="utf-8",
    )
    lock = InstanceLock(
        home_dir / "lock",
        pid_alive=lambda pid: pid in {ALIVE},
        exe_path_of=lambda pid: r"C:\APP\python.exe" if pid == ALIVE else "",
    )
    with pytest.raises(LockBusy):
        lock.acquire()


def test_垃圾锁文件当成陈旧锁(home_dir: Path):
    path = home_dir / "lock"
    path.write_text("{ 这不是 json", encoding="utf-8")
    lock = lock_at(home_dir)
    assert lock.holder() is None
    assert lock.acquire().pid > 0


def test_release_不删别人的锁(home_dir: Path):
    path = home_dir / "lock"
    lock = lock_at(home_dir)
    lock.acquire()
    # 模拟被 --force 接管：文件里的 pid 换成了别人
    path.write_text(json.dumps({"pid": ALIVE, "started_at": "现在", "exe_path": "py"}), encoding="utf-8")
    assert lock.release() is False
    assert path.exists(), "不该去动别人的锁文件"


def test_没拿过锁就释放返回_False(home_dir: Path):
    assert lock_at(home_dir).release() is False


def test_LockHolder_可读描述():
    holder = LockHolder(pid=1, started_at="2026-09-23T01:00:00.000Z", exe_path="py")
    assert "PID 1" in holder.describe()
    assert "2026-09-23" in holder.describe()


def test_v1_带_mono_列的老库被就地清理(home_dir: Path):
    """v1 早期把 mono 落了库，后来字段删了但版本没动 —— 老库必须能就地修好，数据一行不丢。"""
    path = home_dir / DB
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, mono REAL NOT NULL, pid INTEGER NOT NULL,
            exe TEXT NOT NULL, title TEXT NOT NULL, idle_s REAL NOT NULL,
            kind TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO meta(key, value) VALUES('schema_version', '1');
        INSERT INTO samples(ts, mono, pid, exe, title, idle_s, kind, note)
        VALUES('2026-10-12T01:00:00.000Z', 123.5, 7, 'code.exe', 'main.py', 0.0, 'active', '');
        """
    )
    connection.commit()
    connection.close()

    with Store(path).initialize() as store:
        columns = [row[1] for row in store.connection.execute("PRAGMA table_info(samples)")]
        assert "mono" not in columns, "mono 应该被清掉"
        assert columns == ["id", "ts", "pid", "exe", "title", "idle_s", "kind", "note"]
        assert store.meta_get(META_SCHEMA_VERSION) == str(SCHEMA_VERSION)
        # 老数据一行不丢，且现在能正常写入（这正是原来会 NOT NULL 报错的地方）
        store.insert_sample(sample(at("10:00", "2026-10-13"), "msedge.exe"))
        assert store.count_samples() == 2
        # samples() 按 ts 升序：老库那条在 10-12，新写的在 10-13
        assert [row.exe for row in store.samples()] == ["code.exe", "msedge.exe"]
        assert store.samples()[0].pid == 7


def test_v1_没有_mono_列的老库只升版本(home_dir: Path):
    path = home_dir / DB
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, pid INTEGER NOT NULL, exe TEXT NOT NULL,
            title TEXT NOT NULL, idle_s REAL NOT NULL, kind TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO meta(key, value) VALUES('schema_version', '1');
        INSERT INTO samples(ts, pid, exe, title, idle_s, kind, note)
        VALUES('2026-10-12T01:00:00.000Z', 7, 'code.exe', '', 0.0, 'active', '');
        """
    )
    connection.commit()
    connection.close()

    with Store(path).initialize() as store:
        assert store.meta_get(META_SCHEMA_VERSION) == str(SCHEMA_VERSION)
        assert store.count_samples() == 1


def test_老库清理失败时不谎称成功(home_dir: Path):
    """被索引住的列删不掉 —— SQLite 会拒绝，我们要把它翻成人话，别把"没清成"包装成"好了"。"""
    path = home_dir / DB
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, mono REAL NOT NULL, pid INTEGER NOT NULL,
            exe TEXT NOT NULL, title TEXT NOT NULL, idle_s REAL NOT NULL,
            kind TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_samples_mono ON samples(mono);
        INSERT INTO meta(key, value) VALUES('schema_version', '1');
        """
    )
    connection.commit()
    connection.close()

    store = Store(path).open()
    try:
        with pytest.raises(StoreError, match="清理旧字段失败"):
            store.initialize()
    finally:
        store.close()

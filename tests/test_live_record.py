"""Live collection record (modules/live_record.py, epic #5634 S2)."""

from __future__ import annotations

import ast
import multiprocessing
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from modules import live_record
from modules.builder import CollectionBuilder
from tests.conftest import FakeLogger

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── helpers ───────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    conn = live_record.connect(live_record.db_path(str(tmp_path)))
    yield conn
    conn.close()


def snapshot(name="Picks", library="Movies", members=(), wanted=(), previous_members=None, built=True, present=True, id_type="tmdb", source_path="/config/movies/picks.yml", **extra):
    snap = {
        "library": library,
        "name": name,
        "mapping_name": name,
        "source_file": "picks",
        "source_path": source_path,
        "id_type": id_type,
        "built": built,
        "present": present,
        "kind": "list",
        "missing_checked": True,
        "collection_order": None,
        "order_recorded": False,
        "members": None if members is None else list(members),
        "previous_members": previous_members,
        "wanted": list(wanted),
    }
    snap.update(extra)
    return snap


def item(rk):
    return SimpleNamespace(ratingKey=rk, title=f"Item {rk}")


def fake_library(key_map=None, is_movie=True, imdb_map=None, plex_items=()):
    key_map = key_map or {}
    return SimpleNamespace(
        name="Movies" if is_movie else "TV Shows",
        is_movie=is_movie,
        is_show=not is_movie,
        movie_rating_key_map=key_map if is_movie else {},
        show_rating_key_map={} if is_movie else key_map,
        imdb_map=imdb_map or {},
        get_collection_items=MagicMock(return_value=[item(rk) for rk in plex_items]),
    )


def fake_builder(**attrs):
    b = CollectionBuilder.__new__(CollectionBuilder)
    defaults = {
        "name": "Picks",
        "build_collection": True,
        "obj": object(),
        "deleted": False,
        "exists": True,
        "smart_url": None,
        "smart_label_collection": False,
        "blank_collection": False,
        "sync": True,
        "items": [],
        "remove_item_map": {},
        "found_items": [],
        "missing_movies": [],
        "missing_shows": [],
        "missing_filtered_movies": [],
        "missing_filtered_shows": [],
        "missing_passed_movies": [],
        "missing_passed_shows": [],
        "missing_checked": True,
        "held_back_radarr": [],
        "held_back_sonarr": [],
        "custom_sort": None,
        "gathered_ids": [],
        "sort_completed": False,
    }
    defaults.update(attrs)
    for key, value in defaults.items():
        setattr(b, key, value)
    return b


def _age(conn, name, days, library="Movies"):
    """Pretend ``name`` was last synced ``days`` ago."""
    stamp = live_record._stamp(datetime.now(timezone.utc) - timedelta(days=days))
    conn.execute("UPDATE collections SET last_synced = ? WHERE library = ? AND name = ?", (stamp, library, name))
    conn.commit()


METADATA = SimpleNamespace(get_file_name=lambda: "picks", path="/config/movies/picks.yml")


V1_SCHEMA = """
CREATE TABLE collections (library TEXT NOT NULL, name TEXT NOT NULL, source_file TEXT, source_path TEXT, id_type TEXT, built INTEGER NOT NULL, present INTEGER NOT NULL, kind TEXT NOT NULL,
    members_tracked INTEGER NOT NULL, missing_checked INTEGER NOT NULL, collection_order TEXT, order_recorded INTEGER NOT NULL, recorded_at TEXT NOT NULL, PRIMARY KEY (library, name));
CREATE TABLE members (library TEXT NOT NULL, name TEXT NOT NULL, rating_key INTEGER NOT NULL, ext_id INTEGER, position INTEGER, PRIMARY KEY (library, name, rating_key));
CREATE TABLE wanted (library TEXT NOT NULL, name TEXT NOT NULL, ext_id INTEGER NOT NULL, filtered INTEGER NOT NULL, held_back INTEGER NOT NULL, position INTEGER, PRIMARY KEY (library, name, ext_id));
CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, library TEXT NOT NULL, name TEXT NOT NULL, recorded_at TEXT NOT NULL, event TEXT NOT NULL, rating_key INTEGER, ext_id INTEGER);
"""


class StackLogger(FakeLogger):
    def __init__(self):
        super().__init__()
        self.stacktraces = 0

    def stacktrace(self):
        self.stacktraces += 1


# ── multi-process workers (top level so the spawn start method can import them) ──


def _proc_writer(path, prefix, count, start, results):
    start.wait()
    total = 0
    conn = live_record.connect(path)
    try:
        for i in range(count):
            name = prefix if prefix.endswith("!") else f"{prefix}{i}"
            events = live_record.write_collection(conn, snapshot(name=name, members=[(i, i, None)], wanted=[(1000 + i, False, False, None)], previous_members=[]))
            total += len(events)
    finally:
        conn.close()
    results.put(total)


def _run_processes(path, jobs):
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    results = ctx.Queue()
    procs = [ctx.Process(target=_proc_writer, args=(path, prefix, count, start, results)) for prefix, count in jobs]
    for proc in procs:
        proc.start()
    start.set()
    totals = [results.get(timeout=120) for _ in procs]
    for proc in procs:
        proc.join(timeout=120)
    assert [proc.exitcode for proc in procs] == [0] * len(procs)
    return totals


# ── storage ───────────────────────────────────────────────────────────


class TestStorage:
    def test_schema_is_wal_and_versioned(self, db):
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA user_version").fetchone()[0] == live_record.SCHEMA_VERSION
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == live_record.BUSY_TIMEOUT_MS

    def test_an_older_schema_is_dropped_and_rebuilt_loudly(self, tmp_path):
        # The v1 shape this branch first shipped (recorded_at, no mapping_name/last_synced).
        path = live_record.db_path(str(tmp_path))
        old = sqlite3.connect(path)
        old.executescript(V1_SCHEMA)
        old.execute("INSERT INTO collections VALUES ('Movies', 'Picks', 'picks', '/config/picks.yml', 'tmdb', 1, 1, 'manual', 1, 1, NULL, 0, '2026-11-01T00:00:00+00:00')")
        old.execute("PRAGMA user_version = 1")
        old.commit()
        old.close()
        log = StackLogger()
        conn = live_record.connect(path, log=log)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == live_record.SCHEMA_VERSION == 5
        assert live_record.get_collection(conn, "Movies", "Picks") is None
        assert any("schema v1 is out of date" in m for m in log.warning_messages)
        live_record.write_collection(conn, snapshot(wanted=[(5, False, False, None)]))
        assert [r["name"] for r in live_record.find_waiting(conn, "tmdb", 5)] == ["Picks"]
        conn.close()

    def test_the_recorder_survives_an_older_schema(self, tmp_path):
        path = live_record.db_path(str(tmp_path))
        old = sqlite3.connect(path)
        old.executescript(V1_SCHEMA)
        old.execute("PRAGMA user_version = 1")
        old.commit()
        old.close()
        log = StackLogger()
        recorder = live_record.RunRecorder(str(tmp_path), log)
        assert recorder.record(fake_library(), METADATA, fake_builder(), "picks", False, False) is True
        assert recorder.failures == 0
        assert any("rebuilt it as v5" in m for m in log.warning_messages)

    def test_a_current_record_is_not_rebuilt(self, tmp_path):
        path = live_record.db_path(str(tmp_path))
        conn = live_record.connect(path)
        live_record.write_collection(conn, snapshot())
        conn.close()
        log = StackLogger()
        conn = live_record.connect(path, log=log)
        assert live_record.get_collection(conn, "Movies", "Picks") is not None
        assert not log.warning_messages
        conn.close()

    def test_reopen_keeps_data(self, tmp_path):
        path = live_record.db_path(str(tmp_path))
        conn = live_record.connect(path)
        live_record.write_collection(conn, snapshot(members=[(1, 10, None)]))
        conn.close()
        conn = live_record.connect(path, timeout_ms=500)
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 500
        assert [m["rating_key"] for m in live_record.get_members(conn, "Movies", "Picks")] == [1]
        conn.close()

    def test_wal_switch_retries_a_locked_race(self):
        class RacingConn:
            def __init__(self):
                self.calls = 0

            def execute(self, sql):
                self.calls += 1
                if sql == "PRAGMA journal_mode":
                    return SimpleNamespace(fetchone=lambda: ["delete"])
                if self.calls < 6:
                    raise sqlite3.OperationalError("database is locked")
                return SimpleNamespace(fetchone=lambda: ["wal"])

        conn = RacingConn()
        live_record._set_wal(conn, 5000)
        assert conn.calls == 6

    def test_wal_switch_gives_up_at_the_deadline(self):
        class LockedConn:
            def execute(self, sql):
                if sql == "PRAGMA journal_mode":
                    return SimpleNamespace(fetchone=lambda: ["delete"])
                raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.OperationalError, match="locked"):
            live_record._set_wal(LockedConn(), 0)

    def test_first_record_diffs_against_the_runs_starting_membership(self, db):
        # Before this run Plex held 1 and 2; the run added 3 and removed 2.
        events = live_record.write_collection(db, snapshot(members=[(1, 10, None), (3, 30, None)], previous_members=[(1, 10), (2, 20)]))
        assert set(events) == {(live_record.MEMBER_ADDED, 3, 30), (live_record.MEMBER_REMOVED, 2, 20)}

    def test_first_record_without_known_start_writes_no_member_events(self, db):
        assert live_record.write_collection(db, snapshot(members=[(1, 10, None)], previous_members=None)) == []

    def test_second_record_diffs_against_the_first(self, db):
        live_record.write_collection(db, snapshot(members=[(1, 10, None), (2, 20, None)], wanted=[(40, False, False, None)], previous_members=[(1, 10), (2, 20)]))
        events = live_record.write_collection(db, snapshot(members=[(2, 20, None), (4, 40, None)], wanted=[(50, False, True, None)]))
        assert set(events) == {
            (live_record.MEMBER_ADDED, 4, 40),
            (live_record.MEMBER_REMOVED, 1, 10),
            (live_record.WANTED_ADDED, None, 50),
            (live_record.WANTED_REMOVED, None, 40),
        }
        history = live_record.get_history(db, "Movies", "Picks")
        assert {(h["event"], h["rating_key"], h["ext_id"]) for h in history} == set(events)

    def test_history_is_bounded_per_collection(self, db, monkeypatch):
        monkeypatch.setattr(live_record, "HISTORY_KEEP", 5)
        live_record.write_collection(db, snapshot(name="Other", members=[(1, 1, None)], previous_members=[]))
        for i in range(10):
            live_record.write_collection(db, snapshot(members=[(i, i, None)], previous_members=[]))
        assert len(live_record.get_history(db, "Movies", "Picks", limit=100)) == 5
        newest = live_record.get_history(db, "Movies", "Picks", limit=2)
        assert {(h["event"], h["rating_key"]) for h in newest} == {(live_record.MEMBER_ADDED, 9), (live_record.MEMBER_REMOVED, 8)}
        assert len(live_record.get_history(db, "Movies", "Other")) == 1

    def test_untracked_members_keep_no_member_rows(self, db):
        live_record.write_collection(db, snapshot(members=[(1, 10, None)], previous_members=[]))
        live_record.write_collection(db, snapshot(members=None))
        assert live_record.get_collection(db, "Movies", "Picks")["members_tracked"] == 0
        assert live_record.get_members(db, "Movies", "Picks") == []

    def test_failed_write_rolls_back(self, db):
        live_record.write_collection(db, snapshot(members=[(1, 10, None)], previous_members=[]))
        bad = snapshot(members=[(2, 20, None)])
        bad["wanted"] = [("not", "a", "tuple", "of-four", "!")]
        with pytest.raises(ValueError):
            live_record.write_collection(db, bad)
        assert [m["rating_key"] for m in live_record.get_members(db, "Movies", "Picks")] == [1]
        assert not db.in_transaction

    def test_failed_sort_keeps_the_last_good_order(self, db):
        live_record.write_collection(db, snapshot(members=[(1, 10, 0), (2, 20, 2)], wanted=[(15, False, False, 1)], collection_order="custom.asc", order_recorded=True))
        first = live_record.get_collection(db, "Movies", "Picks")["order_recorded_at"]
        live_record.write_collection(db, snapshot(members=[(1, 10, None), (2, 20, None), (3, 30, None)], wanted=[(15, False, False, None)], collection_order="custom.asc", order_recorded=False))
        row = live_record.get_collection(db, "Movies", "Picks")
        assert row["order_recorded"] == 1
        assert row["order_recorded_at"] == first
        assert [(m["rating_key"], m["position"]) for m in live_record.get_members(db, "Movies", "Picks")] == [(1, 0), (2, 2), (3, None)]
        assert live_record.get_position(db, "Movies", "Picks", 15) == 1

    def test_order_not_carried_when_the_collection_order_changed(self, db):
        live_record.write_collection(db, snapshot(members=[(1, 10, 0)], collection_order="custom.asc", order_recorded=True))
        live_record.write_collection(db, snapshot(members=[(1, 10, None)], collection_order=None, order_recorded=False))
        assert live_record.get_collection(db, "Movies", "Picks")["order_recorded"] == 0
        assert live_record.get_position(db, "Movies", "Picks", 10) is None


class TestPartialRuns:
    def test_recording_one_collection_leaves_the_others(self, db):
        live_record.write_collection(db, snapshot(name="A", members=[(1, 10, None)], previous_members=[]))
        live_record.write_collection(db, snapshot(name="B", members=[(2, 20, None)], previous_members=[]))
        # A scoped run (--run-collections B) records only B.
        live_record.write_collection(db, snapshot(name="B", members=[(3, 30, None)]))
        assert [m["rating_key"] for m in live_record.get_members(db, "Movies", "A")] == [1]
        assert [m["rating_key"] for m in live_record.get_members(db, "Movies", "B")] == [3]

    def test_a_collection_missing_from_a_run_keeps_its_record(self, db):
        # A file can report "Loaded Successfully" after a dynamic section failed (a transient
        # TMDb/Plex/Trakt error): that run never records the dropped collections. Nothing deletes them.
        live_record.write_collection(db, snapshot(name="Static", wanted=[(8, False, False, None)]))
        live_record.write_collection(db, snapshot(name="Dynamic 1999", mapping_name="dyn_1999", wanted=[(9, False, False, 0)], collection_order="custom.asc", order_recorded=True))
        # Next sweep: the dynamic section failed, only Static ran; then the janitor runs.
        live_record.write_collection(db, snapshot(name="Static", wanted=[(8, False, False, None)]))
        assert live_record.expire(db) == []
        assert [r["name"] for r in live_record.find_waiting(db, "tmdb", 9)] == ["Dynamic 1999"]
        assert live_record.get_position(db, "Movies", "Dynamic 1999", 9) == 0

    def test_renamed_collection_goes_stale_then_expires(self, db):
        now = datetime.now(timezone.utc)
        live_record.write_collection(db, snapshot(name="Old Title", mapping_name="picks", members=[(1, 10, None)], wanted=[(5, False, False, None)], previous_members=[]))
        _age(db, "Old Title", days=1)
        # The `name:` changed, the mapping key did not: the new title gets its own row.
        live_record.write_collection(db, snapshot(name="New Title", mapping_name="picks", wanted=[(5, False, False, None)]))
        assert [r["name"] for r in live_record.find_waiting(db, "tmdb", 5)] == ["New Title", "Old Title"]
        # Within STALE_AFTER the old row stops being returned; New Title keeps syncing.
        later = now + live_record.STALE_AFTER
        live_record.write_collection(db, snapshot(name="New Title", mapping_name="picks", wanted=[(5, False, False, None)]))
        _age(db, "New Title", days=0)
        assert [r["name"] for r in live_record.find_waiting(db, "tmdb", 5, now=later)] == ["New Title"]
        assert live_record.get_collection(db, "Movies", "Old Title") is not None
        # Past EXPIRE_AFTER the janitor deletes it, history included.
        assert live_record.expire(db, now=now + live_record.EXPIRE_AFTER) == [("Movies", "Old Title")]
        assert live_record.get_collection(db, "Movies", "Old Title") is None
        assert live_record.get_history(db, "Movies", "Old Title") == []
        assert live_record.get_collection(db, "Movies", "New Title") is not None

    def test_schedule_skip_keeps_only_the_current_title_fresh(self, db):
        live_record.write_collection(db, snapshot(name="Old Title", mapping_name="weekly", wanted=[(5, False, False, None)]))
        _age(db, "Old Title", days=10)
        live_record.write_collection(db, snapshot(name="New Title", mapping_name="weekly", wanted=[(5, False, False, None)]))
        _age(db, "New Title", days=6)
        # Off-schedule days touch the newest row for the mapping key, nothing else.
        assert live_record.touch(db, "Movies", "weekly") == "New Title"
        assert [r["name"] for r in live_record.find_waiting(db, "tmdb", 5)] == ["New Title"]
        assert live_record.touch(db, "Movies", "never_recorded") is None
        assert not db.in_transaction

    def test_a_failed_sync_is_not_refreshed_by_schedule_skips(self, db):
        live_record.write_collection(db, snapshot(name="Weekly", mapping_name="weekly", wanted=[(5, False, False, None)]))
        _age(db, "Weekly", days=5)
        assert live_record.mark_failed(db, "Movies", "weekly") == "Weekly"
        assert live_record.touch(db, "Movies", "weekly") is None
        assert live_record.find_waiting(db, "tmdb", 5) == []
        assert live_record.get_collection(db, "Movies", "Weekly")["last_status"] == "failed"
        # The next real sync that succeeds clears it.
        live_record.write_collection(db, snapshot(name="Weekly", mapping_name="weekly", wanted=[(5, False, False, None)]))
        assert live_record.get_collection(db, "Movies", "Weekly")["last_status"] == "ok"
        _age(db, "Weekly", days=5)
        assert live_record.touch(db, "Movies", "weekly") == "Weekly"
        assert live_record.mark_failed(db, "Movies", "never_recorded") is None
        assert not db.in_transaction

    def test_a_sync_that_errored_after_snapshot_is_written_as_failed(self, db):
        live_record.write_collection(db, snapshot(name="Weekly", mapping_name="weekly", sync_ok=False))
        assert live_record.get_collection(db, "Movies", "Weekly")["last_status"] == "failed"
        assert live_record.touch(db, "Movies", "weekly") is None

    def test_find_waiting_reports_age_and_honours_stale_after(self, db):
        live_record.write_collection(db, snapshot(wanted=[(5, False, False, None)]))
        _age(db, "Picks", days=2)
        (row,) = live_record.find_waiting(db, "tmdb", 5)
        assert 2 * 86400 - 60 <= row["age_seconds"] <= 2 * 86400 + 60
        assert row["last_synced"]
        assert live_record.find_waiting(db, "tmdb", 5, stale_after=timedelta(days=1)) == []

    def test_expire_only_touches_old_rows(self, db):
        live_record.write_collection(db, snapshot(name="Fresh", wanted=[(1, False, False, None)]))
        live_record.write_collection(db, snapshot(name="Month", members=[(2, 20, None)], wanted=[(3, False, False, None)], previous_members=[]))
        _age(db, "Fresh", days=29)
        _age(db, "Month", days=31)
        assert live_record.expire(db) == [("Movies", "Month")]
        for table in ("members", "wanted", "history"):
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE name = 'Month'").fetchone()[0] == 0  # nosec B608
        assert live_record.get_collection(db, "Movies", "Fresh") is not None

    def test_nothing_but_the_janitor_deletes_records(self):
        tree = ast.parse((REPO_ROOT / "modules" / "live_record.py").read_text(encoding="utf-8"))
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
        deleters = {n.name for n in functions if "DELETE FROM {table}" in ast.unparse(n) or "DELETE FROM collections" in ast.unparse(n)}
        assert deleters == {"expire"}

    def test_mark_deleted_empties_the_record(self, db):
        live_record.write_collection(db, snapshot(name="Xmas", mapping_name="xmas", members=[(1, 10, None)], wanted=[(20, False, False, None)], previous_members=[]))
        assert live_record.mark_deleted(db, "Movies", "xmas") == ["Xmas"]
        row = live_record.get_collection(db, "Movies", "Xmas")
        assert row["present"] == 0
        assert live_record.get_members(db, "Movies", "Xmas") == []
        assert live_record.find_waiting(db, "tmdb", 20) == []
        events = {(h["event"], h["rating_key"], h["ext_id"]) for h in live_record.get_history(db, "Movies", "Xmas")}
        assert {(live_record.MEMBER_REMOVED, 1, 10), (live_record.WANTED_REMOVED, None, 20)} <= events


class TestConcurrentWriters:
    def test_processes_racing_on_a_fresh_file_all_succeed(self, tmp_path):
        # Each trial starts from no file at all: the WAL switch and schema creation race.
        for trial in range(3):
            path = str(tmp_path / f"race{trial}.db")
            _run_processes(path, [(f"p{n}-", 15) for n in range(4)])
            conn = live_record.connect(path)
            assert conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0] == 60
            assert conn.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 60
            conn.close()

    def test_two_processes_writing_the_same_collection_stay_consistent(self, tmp_path):
        path = live_record.db_path(str(tmp_path))
        totals = _run_processes(path, [("Shared!", 25), ("Shared!", 25)])
        conn = live_record.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0] == 1
        members = live_record.get_members(conn, "Movies", "Shared!")
        assert len(members) == 1
        assert [r["ext_id"] for r in conn.execute("SELECT ext_id FROM wanted")] == [1000 + members[0]["rating_key"]]
        # Every diff each writer computed landed in history: no lost or interleaved transaction.
        assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == sum(totals)
        conn.close()

    def test_held_write_lock_blocks_then_releases(self, tmp_path):
        path = live_record.db_path(str(tmp_path))
        holder = live_record.connect(path)
        holder.execute("BEGIN IMMEDIATE")
        writer = live_record.connect(path, timeout_ms=50)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            live_record.write_collection(writer, snapshot(members=[(1, 10, None)], previous_members=[]))
        assert not writer.in_transaction
        holder.execute("COMMIT")
        holder.close()
        assert live_record.write_collection(writer, snapshot(members=[(1, 10, None)], previous_members=[])) == [(live_record.MEMBER_ADDED, 1, 10)]
        writer.close()

    def test_reader_sees_last_commit_while_a_write_is_open(self, tmp_path):
        path = live_record.db_path(str(tmp_path))
        writer = live_record.connect(path)
        live_record.write_collection(writer, snapshot(wanted=[(5, False, False, None)]))
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("DELETE FROM wanted")
        reader = live_record.connect(path)
        assert [r["name"] for r in live_record.find_waiting(reader, "tmdb", 5)] == ["Picks"]
        writer.execute("ROLLBACK")
        writer.close()
        reader.close()


# ── read API ──────────────────────────────────────────────────────────


class TestReadApi:
    def test_find_waiting_uses_post_filter_set_and_id_type(self, db):
        live_record.write_collection(db, snapshot(name="A", wanted=[(7, False, True, 3)], collection_order="custom.asc", order_recorded=True))
        live_record.write_collection(db, snapshot(name="Filtered", wanted=[(7, True, False, None)]))
        live_record.write_collection(db, snapshot(name="NotBuilt", built=False, wanted=[(7, False, False, None)]))
        live_record.write_collection(db, snapshot(name="Shows", library="TV Shows", id_type="tvdb", wanted=[(7, False, False, None)]))
        live_record.write_collection(db, snapshot(name="New", present=False, members=[], wanted=[(7, None, False, None)]))
        rows = live_record.find_waiting(db, "tmdb", 7, include_unknown=True)
        assert [(r["name"], r["present"], r["held_back"], r["position"], r["filter_state"]) for r in rows] == [("A", 1, 1, 3, "passed"), ("New", 0, 0, None, "unknown")]
        # By default only ids run_missing passed: a sync run would remove an unevaluated one.
        assert [r["name"] for r in live_record.find_waiting(db, "tmdb", 7)] == ["A"]
        assert rows[0]["source_file"] == "picks"
        assert rows[0]["mapping_name"] == "A"
        assert [r["name"] for r in live_record.find_waiting(db, "tmdb", 7, include_unknown=False)] == ["A"]
        assert [r["name"] for r in live_record.find_waiting(db, "tvdb", 7)] == ["Shows"]

    def test_position_and_slot(self, db):
        live_record.write_collection(
            db,
            snapshot(members=[(100, 1, 0), (300, 3, 2), (900, 9, None)], wanted=[(2, False, False, 1), (4, False, False, 3)], order_recorded=True),
        )
        assert live_record.get_position(db, "Movies", "Picks", 2) == 1
        assert live_record.get_position(db, "Movies", "Picks", 3) == 2
        assert live_record.get_position(db, "Movies", "Picks", 99) is None
        assert live_record.slot_after(db, "Movies", "Picks", 1) == 100
        assert live_record.slot_after(db, "Movies", "Picks", 3) == 300
        assert live_record.slot_after(db, "Movies", "Picks", 0) is None
        assert [m["rating_key"] for m in live_record.get_members(db, "Movies", "Picks")] == [100, 300, 900]


# ── snapshot from a builder ───────────────────────────────────────────


class TestSnapshot:
    def test_sync_mode_derives_members_without_plex(self):
        library = fake_library({1: 10, 2: 20, 3: 30})
        # Collection started with 1 and 2; 1 matched (map value cleared), 2 was removed, 3 added.
        builder = fake_builder(remove_item_map={1: None, 2: item(2)}, found_items=[item(1), item(3)])
        snap = live_record.build_snapshot(builder, library, METADATA, add_ran=True, sync_ran=True, mapping_name="picks_key")
        assert snap["members"] == [(1, 10, None), (3, 30, None)]
        assert snap["previous_members"] == [(1, 10), (2, 20)]
        assert snap["mapping_name"] == "picks_key"
        library.get_collection_items.assert_not_called()

    def test_sync_not_run_keeps_original_members(self):
        library = fake_library({1: 10, 2: 20})
        builder = fake_builder(remove_item_map={1: None, 2: item(2)}, found_items=[item(1)])
        snap = live_record.build_snapshot(builder, library, METADATA, add_ran=True, sync_ran=False)
        assert [m[0] for m in snap["members"]] == [1, 2]

    def test_new_collection_starts_empty(self):
        library = fake_library({5: 50})
        builder = fake_builder(exists=False, found_items=[item(5)])
        snap = live_record.build_snapshot(builder, library, METADATA, add_ran=True, sync_ran=False)
        assert snap["members"] == [(5, 50, None)]
        assert snap["previous_members"] == []

    def test_nothing_in_plex_yet_still_records_what_it_wants(self):
        # The NonExisting path: no items found, no Plex collection, every film still missing.
        builder = fake_builder(obj=None, exists=False, missing_movies=[11, 12], missing_checked=False)
        snap = live_record.build_snapshot(builder, fake_library(), METADATA, add_ran=False, sync_ran=False)
        assert snap["present"] is False
        assert snap["members"] == []
        assert snap["wanted"] == [(11, None, False, None), (12, None, False, None)]

    def test_loaded_items_are_the_truth(self):
        library = fake_library({1: 10, 2: 20})
        builder = fake_builder(items=[item(2), item(1)], remove_item_map={9: item(9)})
        snap = live_record.build_snapshot(builder, library, METADATA, add_ran=False, sync_ran=True)
        assert [m[0] for m in snap["members"]] == [2, 1]

    def test_append_mode_reads_membership_once(self):
        library = fake_library({1: 10, 4: 40}, plex_items=[1, 4])
        builder = fake_builder(sync=False, found_items=[item(4)])
        snap = live_record.build_snapshot(builder, library, METADATA, add_ran=True, sync_ran=False)
        assert [m[0] for m in snap["members"]] == [1, 4]
        assert snap["previous_members"] is None
        library.get_collection_items.assert_called_once()

    def test_deleted_collection_has_no_members(self):
        builder = fake_builder(deleted=True, remove_item_map={1: item(1)})
        snap = live_record.build_snapshot(builder, fake_library({1: 10}), METADATA, add_ran=False, sync_ran=False)
        assert snap["present"] is False
        assert snap["members"] == []

    def test_smart_collection_members_untracked(self):
        builder = fake_builder(smart_url="/library/sections/1/all?x=1", missing_movies=[5])
        snap = live_record.build_snapshot(builder, fake_library(), METADATA, add_ran=False, sync_ran=False)
        assert snap["kind"] == "smart"
        assert snap["members"] is None
        assert snap["wanted"] == []

    def test_wanted_filter_state_and_held_back(self):
        builder = fake_builder(
            missing_movies=[7, 8, 9, 6, 7],
            missing_filtered_movies=[8],
            missing_passed_movies=[7, 9],
            held_back_radarr=[{"title": "Nine", "id": 9}],
        )
        snap = live_record.build_snapshot(builder, fake_library(), METADATA, add_ran=False, sync_ran=False)
        # 6 was never evaluated (its lookup failed): unknown, not "passed".
        assert snap["wanted"] == [(7, False, False, None), (8, True, False, None), (9, False, True, None), (6, None, False, None)]

    def test_show_library_uses_tvdb(self):
        builder = fake_builder(missing_movies=[1], missing_shows=[2], missing_passed_shows=[2], remove_item_map={5: None})
        snap = live_record.build_snapshot(builder, fake_library({5: 55}, is_movie=False), METADATA, add_ran=False, sync_ran=False)
        assert snap["id_type"] == "tvdb"
        assert snap["members"] == [(5, 55, None)]
        assert snap["wanted"] == [(2, False, False, None)]

    def test_custom_order_positions_members_and_wanted(self):
        library = fake_library({1: 10, 3: 30}, imdb_map={"tt3": [3]})
        builder = fake_builder(
            custom_sort="custom.asc",
            sort_completed=True,
            gathered_ids=[(10, "tmdb"), ([20, 10], "tmdb"), ("tt3", "imdb"), (40, "tmdb"), ("x", "tvdb")],
            items=[item(1), item(3)],
            missing_movies=[20, 40],
            missing_passed_movies=[20, 40],
        )
        snap = live_record.build_snapshot(builder, library, METADATA, add_ran=False, sync_ran=False)
        assert snap["order_recorded"] is True
        assert snap["collection_order"] == "custom.asc"
        assert snap["members"] == [(1, 10, 0), (3, 30, 2)]
        assert snap["wanted"] == [(20, False, False, 1), (40, False, False, 3)]

    def test_custom_desc_reverses(self):
        builder = fake_builder(custom_sort="custom.desc", sort_completed=True, gathered_ids=[(10, "tmdb"), (20, "tmdb")], items=[item(1)], missing_movies=[20])
        snap = live_record.build_snapshot(builder, fake_library({1: 10}), METADATA, add_ran=False, sync_ran=False)
        assert snap["members"] == [(1, 10, 1)]
        assert snap["wanted"] == [(20, None, False, 0)]

    def test_order_not_recorded_when_sort_did_not_finish(self):
        builder = fake_builder(custom_sort="custom.asc", sort_completed=False, gathered_ids=[(10, "tmdb")], items=[item(1)])
        snap = live_record.build_snapshot(builder, fake_library({1: 10}), METADATA, add_ran=False, sync_ran=False)
        assert snap["order_recorded"] is False
        assert snap["members"] == [(1, 10, None)]

    def test_field_sort_is_not_a_custom_order(self):
        builder = fake_builder(custom_sort=["release"], sort_completed=True, items=[item(1)])
        snap = live_record.build_snapshot(builder, fake_library({1: 10}), METADATA, add_ran=False, sync_ran=False)
        assert snap["order_recorded"] is False
        assert snap["collection_order"] is None

    def test_snapshot_round_trips(self, db):
        builder = fake_builder(remove_item_map={1: None}, found_items=[item(1), item(2)], missing_movies=[30], missing_passed_movies=[30], held_back_radarr=[{"title": "T", "id": 30}])
        snap = live_record.build_snapshot(builder, fake_library({1: 10, 2: 20}), METADATA, add_ran=True, sync_ran=True)
        events = live_record.write_collection(db, snap)
        assert events == [(live_record.MEMBER_ADDED, 2, 20)]
        assert live_record.find_waiting(db, "tmdb", 30)[0]["held_back"] == 1


# ── the run integration actually running ──────────────────────────────


def _kometa_functions(*names, **namespace):
    """Compile the named functions out of kometa.py (importing it parses argv) into ``namespace``."""
    tree = ast.parse((REPO_ROOT / "kometa.py").read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert sorted(n.name for n in nodes) == sorted(names)
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(module, "kometa.py", "exec"), namespace)  # nosec B102 - our own source, under test
    return namespace


class TestRunIntegration:
    def _ns(self, tmp_path, run_items=False, **recorder_kwargs):
        log = StackLogger()
        recorder = live_record.RunRecorder(str(tmp_path), log, **recorder_kwargs)
        ns = _kometa_functions("record_live_collection", "record_live_deleted", "record_live_skipped", "record_live_failed", "expire_live_record", live_recorder=recorder, live_record=live_record)
        config = SimpleNamespace(run_items=run_items, default_dir=str(tmp_path))
        return ns, config, log, recorder

    def test_record_writes_through_kometa_wrapper(self, tmp_path):
        ns, config, log, _ = self._ns(tmp_path)
        builder = fake_builder(remove_item_map={1: None}, found_items=[item(1)], missing_movies=[5])
        ns["record_live_collection"](config, fake_library({1: 10}), METADATA, builder, "picks_key", True, True)
        conn = live_record.connect(live_record.db_path(str(tmp_path)))
        assert live_record.get_collection(conn, "Movies", "Picks")["mapping_name"] == "picks_key"
        conn.close()
        assert any(m.startswith("Live Record: Picks | members: 1 | wanted: 1") for m in log.info_messages)

    def test_run_items_never_opens_the_record(self, tmp_path):
        ns, config, log, _ = self._ns(tmp_path, run_items=True)
        ns["record_live_collection"](config, fake_library(), METADATA, fake_builder(), "picks", False, False)
        ns["record_live_deleted"](config, fake_library(), "picks")
        ns["record_live_skipped"](config, fake_library(), "picks")
        ns["record_live_failed"](config, fake_library(), "picks")
        ns["expire_live_record"](config)
        assert not Path(live_record.db_path(str(tmp_path))).exists()

    def test_broken_snapshot_is_logged_and_the_run_continues(self, tmp_path):
        ns, config, log, recorder = self._ns(tmp_path)
        broken = fake_builder()
        del broken.missing_passed_movies
        assert ns["record_live_collection"](config, fake_library(), METADATA, broken, "picks", False, False) is None
        assert log.stacktraces == 1
        assert any("Live Record Error: picks" in m for m in log.error_messages)
        assert recorder.record(fake_library(), METADATA, fake_builder(), "picks", False, False) is True

    def test_unopenable_record_is_logged_and_the_run_continues(self, tmp_path):
        Path(live_record.db_path(str(tmp_path))).write_bytes(b"not a database at all, just bytes" * 10)
        ns, config, log, _ = self._ns(tmp_path)
        ns["record_live_collection"](config, fake_library(), METADATA, fake_builder(), "picks", False, False)
        assert any("Live Record Error" in m for m in log.error_messages)

    def test_held_lock_costs_a_short_timeout_then_recording_stops_for_the_run(self, tmp_path):
        path = live_record.db_path(str(tmp_path))
        holder = live_record.connect(path)
        holder.execute("BEGIN IMMEDIATE")
        ns, config, log, recorder = self._ns(tmp_path, timeout_ms=20, failure_limit=2)
        for _ in range(5):
            ns["record_live_collection"](config, fake_library(), METADATA, fake_builder(), "picks", False, False)
        holder.execute("ROLLBACK")
        holder.close()
        # Two attempts hit the lock, then the breaker skipped the rest without touching SQLite.
        assert log.stacktraces == 2
        assert any("not recording the rest of this run" in m for m in log.error_messages)
        assert recorder.record(fake_library(), METADATA, fake_builder(), "picks", False, False) is False

    def test_success_resets_the_failure_count(self, tmp_path):
        _, _, log, recorder = self._ns(tmp_path, failure_limit=2)
        broken_path = recorder.path
        recorder.path = str(tmp_path / "missing-dir" / "x.db")
        assert recorder.record(fake_library(), METADATA, fake_builder(), "picks", False, False) is False
        recorder.path = broken_path
        assert recorder.record(fake_library(), METADATA, fake_builder(), "picks", False, False) is True
        assert recorder.failures == 0

    def test_expire_wrapper_keeps_rows_absent_from_this_run(self, tmp_path):
        ns, config, log, _ = self._ns(tmp_path)
        conn = live_record.connect(live_record.db_path(str(tmp_path)))
        live_record.write_collection(conn, snapshot(name="Skipped This Run"))
        live_record.write_collection(conn, snapshot(name="Ancient"))
        _age(conn, "Ancient", days=45)
        conn.close()
        ns["expire_live_record"](config)
        conn = live_record.connect(live_record.db_path(str(tmp_path)))
        assert live_record.get_collection(conn, "Movies", "Skipped This Run") is not None
        assert live_record.get_collection(conn, "Movies", "Ancient") is None
        conn.close()
        assert "Live Record: expired Ancient (Movies), not synced for 30 days" in log.info_messages

    def test_skipped_wrapper_refreshes_a_weekly_collection(self, tmp_path):
        ns, config, log, recorder = self._ns(tmp_path)
        conn = live_record.connect(live_record.db_path(str(tmp_path)))
        live_record.write_collection(conn, snapshot(name="Weekly", mapping_name="weekly", wanted=[(5, False, False, None)]))
        _age(conn, "Weekly", days=5)
        conn.close()
        ns["record_live_skipped"](config, SimpleNamespace(name="Movies"), "weekly")
        ns["record_live_skipped"](config, SimpleNamespace(name="Movies"), "not_yet_recorded")
        assert recorder.failures == 0
        conn = live_record.connect(live_record.db_path(str(tmp_path)))
        assert [r["name"] for r in live_record.find_waiting(conn, "tmdb", 5)] == ["Weekly"]
        conn.close()

    def test_failed_run_stops_skips_refreshing_through_the_wrappers(self, tmp_path):
        ns, config, log, recorder = self._ns(tmp_path)
        movies = SimpleNamespace(name="Movies")
        builder = fake_builder(missing_movies=[7])
        ns["record_live_collection"](config, fake_library(), METADATA, builder, "picks", True, True, sync_ok=False)
        conn = live_record.connect(live_record.db_path(str(tmp_path)))
        assert live_record.get_collection(conn, "Movies", "Picks")["last_status"] == "failed"
        live_record.write_collection(conn, snapshot(name="Weekly", mapping_name="weekly", wanted=[(5, False, False, None)]))
        _age(conn, "Weekly", days=4)
        conn.close()
        ns["record_live_failed"](config, movies, "weekly")
        ns["record_live_skipped"](config, movies, "weekly")
        assert recorder.failures == 0
        conn = live_record.connect(live_record.db_path(str(tmp_path)))
        weekly = live_record.get_collection(conn, "Movies", "Weekly")
        assert weekly["last_status"] == "failed"
        assert live_record.find_waiting(conn, "tmdb", 5) == []
        conn.close()

    def test_deleted_wrapper_marks_absent(self, tmp_path):
        ns, config, log, _ = self._ns(tmp_path)
        conn = live_record.connect(live_record.db_path(str(tmp_path)))
        live_record.write_collection(conn, snapshot(name="Xmas", mapping_name="xmas", wanted=[(3, False, False, None)]))
        conn.close()
        ns["record_live_deleted"](config, SimpleNamespace(name="Movies"), "xmas")
        conn = live_record.connect(live_record.db_path(str(tmp_path)))
        assert live_record.get_collection(conn, "Movies", "Xmas")["present"] == 0
        conn.close()


# ── builder capture points ────────────────────────────────────────────


class TestBuilderCapture:
    def test_run_missing_records_filtered_and_passed_ids(self, monkeypatch):
        import modules.builder as builder_module

        monkeypatch.setattr(builder_module, "logger", FakeLogger())
        movies = {1: SimpleNamespace(title="Kept", release_date=None), 2: SimpleNamespace(title="Dropped", release_date=None)}
        builder = fake_builder(
            library=SimpleNamespace(is_movie=True, is_show=False, Radarr=None, Sonarr=None),
            config=SimpleNamespace(TMDb=SimpleNamespace(get_movie=lambda tmdb_id: movies[tmdb_id])),
            is_playlist=False,
            missing_movies=[1, 2, 3],
            missing_parts=[],
            details={"show_missing": False, "show_filtered": False, "missing_only_released": False},
            do_report=False,
            run_again=False,
            missing_checked=False,
        )
        builder._safe_tmdb_lookup = lambda getter, tmdb_id, kind: movies.get(tmdb_id)
        builder.check_missing_filters = lambda missing_id, is_movie, **kwargs: missing_id != 2
        builder.run_missing()
        assert builder.missing_checked is True
        assert builder.missing_filtered_movies == [2]
        assert builder.missing_passed_movies == [1]
        # 3 failed its TMDb lookup: it is in neither list, so the record marks it unknown.

    def test_gather_keeps_order_only_for_custom_sorts(self):
        builder = fake_builder(custom_sort="custom.asc", budget_blockers=[], gathered_ids=[])
        builder.gather_ids = lambda method, value: [(1, "tmdb"), (2, "tmdb")]
        builder.filter_and_save_items = MagicMock()
        builder.gather_and_save_items("tmdb_movie", None)
        assert builder.gathered_ids == [(1, "tmdb"), (2, "tmdb")]
        other = fake_builder(custom_sort=["release"], budget_blockers=[], gathered_ids=[])
        other.gather_ids = builder.gather_ids
        other.filter_and_save_items = MagicMock()
        other.gather_and_save_items("tmdb_movie", None)
        assert other.gathered_ids == []

    def test_sort_completed_only_without_move_failures(self, monkeypatch):
        import modules.builder as builder_module
        from modules.util import Failed

        monkeypatch.setattr(builder_module, "logger", FakeLogger())

        def move(obj, it, previous):
            if it.ratingKey == 2:
                raise Failed("nope")

        builder = fake_builder(Type="Collection", custom_sort="custom.asc", found_items=[item(1), item(2)], items=[], library=SimpleNamespace(moveItem=move))
        builder.sort_collection()
        assert builder.sort_completed is False
        builder.items = [item(1), item(2)]
        builder.sort_collection()
        assert builder.sort_completed is True


# ── kometa.py wiring (structural; the wrappers themselves run above) ──


def _function(name):
    tree = ast.parse((REPO_ROOT / "kometa.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in kometa.py")


def _calls(node, name):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name]


def _parents(root):
    parents = {}
    for node in ast.walk(root):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _ancestors(node, parents):
    while node in parents:
        node = parents[node]
        yield node


def _live_ran_assignments(func):
    return [n for n in ast.walk(func) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "live_ran" for t in n.targets) and isinstance(n.value, ast.Tuple)]


class TestWiring:
    def test_record_runs_after_the_except_chain_for_every_collection(self):
        loop = next(n for n in ast.walk(_function("run_collection")) if isinstance(n, ast.For))
        try_index = next(i for i, stmt in enumerate(loop.body) if isinstance(stmt, ast.Try))
        record_ifs = [stmt for stmt in loop.body[try_index + 1 :] if isinstance(stmt, ast.If) and _calls(stmt, "record_live_collection")]
        assert record_ifs, "record_live_collection must run after the try/except, not inside a success path"

    def test_record_is_not_gated_on_missing_items(self):
        func = _function("run_collection")
        parents = _parents(func)
        assignments = _live_ran_assignments(func)
        assert len(assignments) == 2
        for assign in assignments:
            for ancestor in _ancestors(assign, parents):
                if isinstance(ancestor, ast.If):
                    assert "do_missing" not in ast.unparse(ancestor.test)
        # One snapshot point sits directly in the try body, unconditional.
        assert any(isinstance(parents[a], ast.Try) for a in assignments)

    def test_no_items_found_records_before_raising(self):
        func = _function("run_collection")
        raising = [n for n in ast.walk(func) if isinstance(n, ast.If) and any(isinstance(s, ast.Raise) and "NonExisting" in ast.unparse(s) for s in n.body)]
        assert len(raising) == 1
        kinds = [type(s).__name__ for s in raising[0].body]
        assert kinds == ["Assign", "Raise"]
        assert ast.unparse(raising[0].body[0].targets[0]) == "live_ran"

    def test_deleted_not_scheduled_marks_the_record(self):
        func = _function("run_collection")
        handler = next(h for h in ast.walk(func) if isinstance(h, ast.ExceptHandler) and h.type is not None and ast.unparse(h.type) == "NotScheduled")
        deleted_if = next(n for n in ast.walk(handler) if isinstance(n, ast.If) and "was deleted" in ast.unparse(n.test))
        assert _calls(deleted_if, "record_live_deleted")

    def test_schedule_skips_refresh_the_record(self):
        func = _function("run_collection")
        handler = next(h for h in ast.walk(func) if isinstance(h, ast.ExceptHandler) and h.type is not None and ast.unparse(h.type) == "NotScheduled")
        touch_if = next(n for n in ast.walk(handler) if isinstance(n, ast.If) and any(isinstance(stmt, ast.Expr) and _calls(stmt, "record_live_skipped") for stmt in n.body))
        assert ast.unparse(touch_if.test) == "isinstance(e, ScheduleSkip)"
        gone_if = next(n for n in ast.walk(handler) if isinstance(n, ast.If) and "could not be found to delete" in ast.unparse(n.test))
        assert _calls(gone_if, "record_live_deleted")

    def test_builder_raises_schedule_skip_only_for_configured_schedule_skips(self):
        source = (REPO_ROOT / "modules" / "builder.py").read_text(encoding="utf-8")
        raises = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call) and isinstance(n.exc.func, ast.Name)]
        skips = sorted(ast.unparse(n.exc.args[0]) for n in raises if n.exc.func.id == "ScheduleSkip")
        assert skips == sorted(
            [
                "'Skipped because only_run_on_create is True and the collection already exists'",
                "f'{err}\\n\\n{self.Type} {self.name} not scheduled to run{suffix}'",
                "f'{msg}{suffix}'",
                "self.non_existing",
            ]
        )
        # run_definition and a broken tmdb_person setup are not schedule skips: they stay plain NotScheduled.
        plain = " ".join(ast.unparse(n.exc.args[0]) for n in raises if n.exc.func.id == "NotScheduled" and n.exc.args)
        assert "run_definition" in plain and "tmdb_person" in plain

    def test_schedule_skip_is_a_not_scheduled(self):
        from modules.util import NotScheduled, ScheduleSkip

        assert issubclass(ScheduleSkip, NotScheduled)

    def test_errored_collections_are_marked_failed(self):
        loop = next(n for n in ast.walk(_function("run_collection")) if isinstance(n, ast.For))
        try_index = next(i for i, stmt in enumerate(loop.body) if isinstance(stmt, ast.Try))
        after = loop.body[try_index + 1 :]
        assert ast.unparse(after[0]) == "sync_ok = not library.status[str(mapping_name)]['errors']"
        record_if = after[1]
        assert "sync_ok=sync_ok" in ast.unparse(record_if.body[0])
        assert ast.unparse(record_if.orelse[0].test) == "not sync_ok" and _calls(record_if.orelse[0], "record_live_failed")

    def test_item_runs_never_touch_the_record(self):
        for name in ("record_live_collection", "record_live_deleted", "record_live_skipped", "record_live_failed", "expire_live_record"):
            first = _function(name).body[1]
            assert isinstance(first, ast.If) and "config.run_items" in ast.unparse(first.test)
        run_libraries = ast.unparse(_function("run_libraries"))
        assert "live_recorder = None if config.run_items else live_record.RunRecorder(config.default_dir, logger)" in run_libraries

    def _sweep(self, run_collection_clears_resume=True, **overrides):
        """Run the real run_libraries over one fake library; return whether it expired the record."""
        args = {k: False for k in ("tests", "delete-collections", "delete-labels", "playlists-only", "operations-only", "overlays-only", "collections-only", "metadata-only")}
        args["resume"] = overrides.pop("resume", None)
        args["tests"] = overrides.pop("tests", False)
        recorder = MagicMock()
        live_watch = MagicMock()

        def run_collection(config, library, metadata, requested):
            if run_collection_clears_resume:
                args["resume"] = None  # what the real run_collection does on reaching its target

        metadata = MagicMock()
        metadata.get_file_name.return_value = "picks"
        metadata.get_collections.return_value = ["c1"]
        library = MagicMock(skip_library=False, collection_files=[metadata], run_order=["collections"], collection_names=[], hub_priorities={}, auto_sort_hubs=False, is_music=False, timeout=60)
        library.name = "Movies"
        library.cache_items.return_value = []
        config = SimpleNamespace(run_items=False, default_dir="/nowhere", libraries=[library], Cache=None, requested_libraries=None, requested_collections=None, requested_files=None)
        for key, value in overrides.items():
            setattr(config, key, value)
        ns = _kometa_functions(
            "run_libraries",
            "is_partial_run",
            "expire_live_record",
            "push_live_watch",
            "run_arrivals",
            run_args=args,
            live_watch=live_watch,
            arrivals=MagicMock(),
            run_collection=run_collection,
            live_record=SimpleNamespace(RunRecorder=lambda *_: recorder),
            logger=MagicMock(),
            plexapi=MagicMock(),
            timings=MagicMock(),
            os=__import__("os"),
            datetime=datetime,
            Failed=Exception,
            NotFound=Exception,
            log_critical_exception=lambda e: (_ for _ in ()).throw(e),
            get_critical_error_message=str,
        )
        _, collections_ran = ns["run_libraries"](config)
        assert collections_ran
        self.pushed = live_watch.push.called
        return recorder.expire.called

    def test_a_full_sweep_expires(self):
        assert self._sweep() is True

    def test_a_full_sweep_pushes_the_watched_lists_and_a_partial_run_does_not(self):
        assert self._sweep() is True
        assert self.pushed is True
        assert self._sweep(requested_collections=["c1"]) is False
        assert self.pushed is False

    def test_a_resumed_run_never_expires_even_after_resume_is_cleared(self):
        assert self._sweep(resume="c1") is False

    @pytest.mark.parametrize("override", [{"requested_collections": ["c1"]}, {"requested_files": ["picks"]}, {"requested_libraries": ["Movies"]}, {"tests": True}])
    def test_a_partial_run_never_expires(self, override):
        assert self._sweep(**override) is False

    # ── #5634 S3: what the import trigger relies on from a per-item run (run, not grepped) ──

    def _constant(self, name):
        tree = ast.parse((REPO_ROOT / "kometa.py").read_text(encoding="utf-8"))
        return next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and [ast.unparse(t) for t in n.targets] == [name])

    def test_an_item_run_that_finds_the_lock_held_exits_75_and_never_runs(self, tmp_path):
        import fcntl
        import os

        ran = []
        ns = _kometa_functions(
            "process",
            "run_lock_path",
            run_args={"run-items": "10"},
            fcntl=fcntl,
            os=os,
            default_dir=str(tmp_path),
            logger=MagicMock(),
            RUN_LOCK_BUSY=self._constant("RUN_LOCK_BUSY"),
            _process=lambda attrs: ran.append(attrs) or 0,
        )
        with open(tmp_path / ".kometa.run.lock", "a+") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            # 75 = the trigger's EXIT_LOCK_BUSY: it waits for the lock and runs the items again.
            assert ns["process"]({}) == 75
            assert ran == []
        assert ns["process"]({}) == 0 and ran == [{}]

    def _item_run(self, arrived_ok):
        calls = []
        live_watch = MagicMock()
        args = {k: False for k in ("tests", "delete-collections", "delete-labels", "playlists-only", "operations-only", "overlays-only", "collections-only", "metadata-only")}
        args["resume"] = None
        library = MagicMock(skip_library=False, collection_files=[], run_order=["operations"], collection_names=[], hub_priorities={}, auto_sort_hubs=False, is_music=False, timeout=60)
        library.name = library.original_mapping_name = "Movies"
        library.cache_items.return_value = []
        config = SimpleNamespace(run_items=["10"], default_dir="/nowhere", libraries=[library], Cache=None, requested_libraries=None, requested_collections=None, requested_files=None)
        arrivals_module = SimpleNamespace(run=lambda config, library: calls.append("arrivals") or arrived_ok)
        ns = _kometa_functions(
            "run_libraries",
            "is_partial_run",
            "expire_live_record",
            "push_live_watch",
            "run_arrivals",
            run_args=args,
            live_watch=live_watch,
            arrivals=arrivals_module,
            run_collection=MagicMock(),
            live_record=SimpleNamespace(RunRecorder=lambda *_: pytest.fail("an item run opened the recorder")),
            logger=MagicMock(),
            plexapi=MagicMock(),
            timings=MagicMock(),
            os=__import__("os"),
            datetime=datetime,
            Failed=Exception,
            NotFound=Exception,
            log_critical_exception=lambda e: (_ for _ in ()).throw(e),
            get_critical_error_message=str,
            item_run_failed=False,
        )
        library.update_item_from_assets.side_effect = lambda *a, **k: calls.append("operations")
        ns["run_libraries"](config)
        return calls, ns["item_run_failed"]

    def test_an_item_run_does_its_arrival_step(self):
        calls, failed = self._item_run(arrived_ok=True)
        assert calls[:1] == ["arrivals"] and failed is False

    def test_a_failed_arrival_step_fails_the_item_run(self):
        _, failed = self._item_run(arrived_ok=False)
        assert failed is True
        # ...and a failed item run exits RUN_ITEMS_FAILED (1), which the trigger retries.
        start = ast.unparse(_function("start"))
        assert "if run_args['run-items'] and item_run_failed:\n        sys.exit(RUN_ITEMS_FAILED)" in start
        assert self._constant("RUN_ITEMS_FAILED") == 1

    def test_no_prune_left_in_run_collection(self):
        assert not [n for n in ast.walk(_function("run_collection")) if isinstance(n, ast.Name) and "prune" in n.id]


def test_connect_rejects_a_non_database(tmp_path):
    path = tmp_path / "live-collections.db"
    path.write_bytes(b"not a database at all, just bytes" * 10)
    with pytest.raises(sqlite3.DatabaseError):
        live_record.connect(str(path))


# ── #5634 S3: in-place upgrade, arrivals outbox, collection-run queue ──


def _v2_schema():
    schema = live_record.SCHEMA.replace("    source TEXT,\n    added_at TEXT,\n", "").replace(",\n    source TEXT\n", "\n").replace("    watch_urls TEXT,\n", "")
    assert "source TEXT" not in schema and "added_at" not in schema
    return schema


class TestUpgradeInPlace:
    def test_a_v2_record_keeps_its_rows_and_gains_the_v3_columns(self, tmp_path):
        path = live_record.db_path(str(tmp_path))
        old = sqlite3.connect(path)
        old.executescript(_v2_schema())
        old.execute(
            "INSERT INTO collections (library, name, mapping_name, source_file, source_path, id_type, built, present, kind, members_tracked, missing_checked, order_recorded, last_synced) "
            "VALUES ('Movies', 'Picks', 'Picks', 'picks', '/c/p.yml', 'tmdb', 1, 1, 'list', 1, 1, 0, ?)",
            (live_record._now(),),
        )
        old.execute("INSERT INTO wanted VALUES ('Movies', 'Picks', 511, 0, 0, NULL)")
        old.execute("INSERT INTO members VALUES ('Movies', 'Picks', 1, 501, NULL)")
        old.execute("PRAGMA user_version = 2")
        old.commit()
        old.close()
        log = StackLogger()
        conn = live_record.connect(path, log=log)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == live_record.SCHEMA_VERSION
        assert not log.warning_messages  # upgraded, not dropped
        assert [r["name"] for r in live_record.find_waiting(conn, "tmdb", 511)] == ["Picks"]
        assert live_record.record_arrival(conn, "Movies", "Picks", 11, 511) is True
        assert live_record.get_history(conn, "Movies", "Picks")[0]["source"] == live_record.SOURCE_TRIGGER
        assert live_record.queue_arrival(conn, "Movies", 11, {"v": 1}) is True
        conn.close()


class TestOutbox:
    def test_an_unpublished_event_is_replaced_and_a_published_one_is_never_queued_again(self, db):
        assert live_record.queue_arrival(db, "Movies", 11, {"n": 1}) is True
        assert live_record.queue_arrival(db, "Movies", 11, {"n": 2}) is True
        assert [r["payload"] for r in live_record.unpublished_arrivals(db)] == [{"n": 2}]
        live_record.mark_published(db, "Movies", 11)
        assert live_record.queue_arrival(db, "Movies", 11, {"n": 3}) is False
        assert live_record.unpublished_arrivals(db) == []

    def test_expiry_keeps_published_arrivals(self, db):
        live_record.write_collection(db, snapshot())
        live_record.queue_arrival(db, "Movies", 11, {"n": 1})
        live_record.mark_published(db, "Movies", 11)
        _age(db, "Picks", 60)
        assert live_record.expire(db) == [("Movies", "Picks")]
        assert live_record.queue_arrival(db, "Movies", 11, {"n": 2}) is False

    def test_record_arrival_is_idempotent(self, db):
        live_record.write_collection(db, snapshot(wanted=[(511, False, False, None)]))
        assert live_record.record_arrival(db, "Movies", "Picks", 11, 511) is True
        assert live_record.record_arrival(db, "Movies", "Picks", 11, 511) is False
        assert [h["event"] for h in live_record.get_history(db, "Movies", "Picks")].count(live_record.MEMBER_ADDED) == 1

    def test_positions_cover_members_and_wanted(self, db):
        live_record.write_collection(db, snapshot(members=[(1, 501, 0), (2, None, 1)], wanted=[(511, False, False, 2), (512, False, False, None)], collection_order="custom.asc", order_recorded=True))
        assert live_record.positions(db, "Movies", "Picks") == {501: 0, 511: 2}


class TestOutboxLifetime:
    def _old_record_with_an_outbox(self, path, arrivals_columns):
        old = sqlite3.connect(path)
        old.executescript(V1_SCHEMA)
        old.execute(f"CREATE TABLE arrivals ({arrivals_columns})")
        old.execute("CREATE TABLE record_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        old.execute("INSERT INTO record_meta VALUES ('arrivals_since', '2026-11-01T00:00:00Z')")
        old.execute("PRAGMA user_version = 1")
        old.commit()
        return old

    def test_a_rebuild_keeps_the_outbox_so_nothing_is_announced_twice(self, tmp_path):
        path = live_record.db_path(str(tmp_path))
        old = self._old_record_with_an_outbox(path, "library TEXT NOT NULL, rating_key INTEGER NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, published_at TEXT, complete INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (library, rating_key)")
        old.execute("INSERT INTO arrivals VALUES ('Movies', 11, '{}', '2026-11-02T00:00:00Z', '2026-11-02T00:00:01Z', 1)")
        old.execute("INSERT INTO arrivals VALUES ('Movies', 12, '{\"n\": 1}', '2026-11-02T00:00:00Z', NULL, 1)")
        old.commit()
        old.close()
        log = StackLogger()
        conn = live_record.connect(path, log=log)
        assert any("schema v1 is out of date" in m for m in log.warning_messages)  # the rest was rebuilt
        assert live_record.queue_arrival(conn, "Movies", 11, {"added_at": "2026-11-12T00:00:00Z"}) is False
        assert [r["rating_key"] for r in live_record.unpublished_arrivals(conn)] == [12]
        assert live_record.arrivals_since(conn) == datetime(2026, 11, 1, tzinfo=timezone.utc)
        conn.close()

    def test_an_outbox_of_another_shape_is_rebuilt_not_kept(self, tmp_path):
        path = live_record.db_path(str(tmp_path))
        old = self._old_record_with_an_outbox(path, "library TEXT, rating_key INTEGER, payload TEXT")
        old.close()
        conn = live_record.connect(path)
        assert live_record._columns(conn, "arrivals") >= live_record.ARRIVALS_COLUMNS
        assert live_record.queue_arrival(conn, "Movies", 11, {"added_at": live_record._now()}) is True
        conn.close()

    def test_the_first_deploy_does_not_announce_what_plex_added_before_it(self, db):
        since = live_record.arrivals_since(db)
        before = live_record._stamp(since - timedelta(hours=1))
        after = live_record._stamp(since + timedelta(minutes=1))
        assert live_record.queue_arrival(db, "Movies", 11, {"added_at": before}) is False
        assert live_record.queue_arrival(db, "Movies", 12, {"added_at": after}) is True
        assert [r["rating_key"] for r in live_record.unpublished_arrivals(db)] == [12]
        # Recorded as announced: a rerun of the old item never queues it either.
        assert live_record.has_arrival(db, 11) and live_record.queue_arrival(db, "Movies", 11, {"added_at": before}) is False

    def test_an_acked_event_is_announced_even_when_a_rerun_replaced_it_meanwhile(self, db):
        # One Nats-Msg-Id per item: resending the rerun's payload would be dropped as a duplicate
        # inside the window, or announce the item twice after it. The ack is the announcement.
        live_record.queue_arrival(db, "Movies", 11, {"collections": [{"name": "A"}]})
        [sent] = live_record.unpublished_arrivals(db)
        live_record.queue_arrival(db, "Movies", 11, {"collections": [{"name": "B"}]})
        assert live_record.mark_published(db, "Movies", 11, sent["raw"]) is False
        assert live_record.unpublished_arrivals(db) == []
        assert live_record.queue_arrival(db, "Movies", 11, {"collections": [{"name": "C"}]}) is False

    def test_an_unchanged_event_reports_a_clean_mark(self, db):
        live_record.queue_arrival(db, "Movies", 11, {"collections": [{"name": "A"}]})
        [sent] = live_record.unpublished_arrivals(db)
        assert live_record.mark_published(db, "Movies", 11, sent["raw"]) is True


class TestBuildQueue:
    def test_a_repeat_request_is_one_entry_and_failures_are_bounded(self, db):
        live_record.queue_build(db, "movies-key", "Picks", "picks")
        live_record.queue_build(db, "movies-key", "Picks", "picks")
        assert len(live_record.pending_builds(db)) == 1
        assert live_record.fail_build(db, "movies-key", "Picks", 2) is False
        assert live_record.fail_build(db, "movies-key", "Picks", 2) is True
        assert live_record.pending_builds(db) == []


# ── live_watch (epic #5634 S5) ────────────────────────────────────────


LIST_A = "https://letterboxd.com/someone/list/a/"
LIST_B = "https://letterboxd.com/someone/list/b/"


def test_watched_urls_unions_live_watch_collections(db):
    live_record.write_collection(db, snapshot("One", watch_urls=[LIST_B, LIST_A]))
    live_record.write_collection(db, snapshot("Two", watch_urls=[LIST_A]))
    live_record.write_collection(db, snapshot("Plain"))
    assert live_record.watched_urls(db) == [LIST_A, LIST_B]


def test_watched_urls_drops_deleted_and_stale_but_keeps_a_failed_sync(db):
    live_record.write_collection(db, snapshot("Gone", watch_urls=[LIST_A]))
    live_record.mark_deleted(db, "Movies", "Gone")
    live_record.write_collection(db, snapshot("Bad", watch_urls=[LIST_B], sync_ok=False))
    assert live_record.watched_urls(db) == [LIST_B]
    live_record.write_collection(db, snapshot("Old", watch_urls=[LIST_A]))
    later = datetime.now(timezone.utc) + live_record.STALE_AFTER + timedelta(hours=1)
    assert live_record.watched_urls(db, now=later) == []


def test_watch_urls_only_passes_list_urls():
    builder = MagicMock(live_watch=True, builders=[("letterboxd_list", {"url": LIST_A}), ("letterboxd_list", {"url": "https://letterboxd.com/someone/films/"}), ("tmdb_list", {"url": LIST_B})])
    assert live_record._watch_urls(builder) == [LIST_A]


def test_dropping_live_watch_stops_watching_on_the_next_record(db):
    live_record.write_collection(db, snapshot("One", watch_urls=[LIST_A]))
    live_record.write_collection(db, snapshot("One"))
    assert live_record.watched_urls(db) == []


def test_v3_record_upgrades_in_place_with_the_watch_column(tmp_path):
    path = live_record.db_path(str(tmp_path))
    conn = live_record.connect(path)
    live_record.write_collection(conn, snapshot("One", members=[(1, 10, None)]))
    conn.execute("ALTER TABLE collections DROP COLUMN watch_urls")
    conn.execute("PRAGMA user_version = 3")
    conn.close()
    conn = live_record.connect(path)
    try:
        assert "watch_urls" in live_record._columns(conn, "collections")
        assert conn.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 1
    finally:
        conn.close()


def test_watch_urls_only_for_opted_in_letterboxd_list_builders():
    def fake(live, builders):
        return SimpleNamespace(live_watch=live, builders=builders)

    lists = [("letterboxd_list", {"url": LIST_B}), ("tmdb_popular", 5), ("letterboxd_list", {"url": LIST_A})]
    assert live_record._watch_urls(fake(True, lists)) == [LIST_A, LIST_B]
    assert live_record._watch_urls(fake(False, lists)) == []


# ── watched list changes (epic #5634 S6) ──────────────────────────────


def test_a_watched_list_change_queues_every_collection_built_from_it(db):
    live_record.write_collection(db, snapshot("One", watch_urls=[LIST_A, LIST_B]))
    live_record.write_collection(db, snapshot("Two", library="TV", watch_urls=[LIST_B]))
    live_record.write_collection(db, snapshot("Plain"))
    queued = live_record.queue_watch_builds(db, LIST_A, "h1")
    assert [(b["library"], b["mapping_name"]) for b in queued] == [("Movies", "One")]
    assert [(b["library"], b["mapping_name"]) for b in live_record.pending_builds(db)] == [("Movies", "One")]
    live_record.finish_build(db, "Movies", "One")
    both = live_record.queue_watch_builds(db, LIST_B, "h2")
    assert sorted(b["mapping_name"] for b in both) == ["One", "Two"]


def test_the_same_url_and_hash_again_queues_nothing_but_a_new_hash_does(db):
    live_record.write_collection(db, snapshot("One", watch_urls=[LIST_A]))
    assert live_record.queue_watch_builds(db, LIST_A, "h1")
    live_record.finish_build(db, "Movies", "One")
    assert live_record.queue_watch_builds(db, LIST_A, "h1") is None
    assert live_record.pending_builds(db) == []
    assert live_record.queue_watch_builds(db, LIST_A, "h2")
    live_record.finish_build(db, "Movies", "One")
    # The list changed back: that is a real change again.
    assert live_record.queue_watch_builds(db, LIST_A, "h1")


def test_an_unwatched_url_queues_nothing_and_is_not_remembered(db):
    assert live_record.queue_watch_builds(db, LIST_A, "h1") == []
    assert db.execute("SELECT COUNT(*) FROM watch_seen").fetchone()[0] == 0
    live_record.write_collection(db, snapshot("One", watch_urls=[LIST_A]))
    assert live_record.queue_watch_builds(db, LIST_A, "h1")


def test_a_deleted_or_stale_collection_is_not_rebuilt_for_its_list(db):
    live_record.write_collection(db, snapshot("Gone", watch_urls=[LIST_A]))
    live_record.mark_deleted(db, "Movies", "Gone")
    assert live_record.queue_watch_builds(db, LIST_A, "h1") == []
    live_record.write_collection(db, snapshot("Old", watch_urls=[LIST_B]))
    later = datetime.now(timezone.utc) + live_record.STALE_AFTER + timedelta(hours=1)
    assert live_record.queue_watch_builds(db, LIST_B, "h1", now=later) == []


def test_the_record_upgrades_from_v4_in_place_with_watch_seen(tmp_path):
    path = live_record.db_path(str(tmp_path))
    conn = live_record.connect(path)
    conn.execute("DROP TABLE watch_seen")
    conn.execute("ALTER TABLE builds DROP COLUMN gen")
    conn.execute("PRAGMA user_version = 4")
    conn.close()
    conn = live_record.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == live_record.SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM watch_seen").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_request_made_while_its_run_is_in_flight_survives_that_run(db):
    live_record.write_collection(db, snapshot("One", watch_urls=[LIST_A]))
    live_record.queue_watch_builds(db, LIST_A, "h1")
    served = live_record.pending_builds(db)[0]
    # The run for h1 is going; h2 arrives and queues the same collection again.
    assert live_record.queue_watch_builds(db, LIST_A, "h2")
    assert live_record.finish_build(db, "Movies", "One", served["gen"]) is False
    again = live_record.pending_builds(db)
    assert len(again) == 1 and again[0]["gen"] == served["gen"] + 1
    assert live_record.finish_build(db, "Movies", "One", again[0]["gen"]) is True
    assert live_record.pending_builds(db) == []


def test_a_new_request_forgives_failed_attempts_and_a_stale_failure_is_not_charged(db):
    live_record.write_collection(db, snapshot("One", watch_urls=[LIST_A]))
    live_record.queue_watch_builds(db, LIST_A, "h1")
    first = live_record.pending_builds(db)[0]
    assert live_record.fail_build(db, "Movies", "One", 3, first["gen"]) is False
    assert live_record.fail_build(db, "Movies", "One", 3, first["gen"]) is False
    live_record.queue_watch_builds(db, LIST_A, "h2")
    assert live_record.pending_builds(db)[0]["attempts"] == 0
    # The run that served h1 fails now: the h2 request is not charged for it, nor dropped.
    assert live_record.fail_build(db, "Movies", "One", 3, first["gen"]) is False
    assert live_record.pending_builds(db)[0]["attempts"] == 0
    current = live_record.pending_builds(db)[0]
    assert [live_record.fail_build(db, "Movies", "One", 3, current["gen"]) for _ in range(3)] == [False, False, True]
    assert live_record.pending_builds(db) == []


def test_an_arrival_requeue_keeps_attempts_and_generation(db):
    live_record.queue_build(db, "Movies", "One", "one")
    first = live_record.pending_builds(db)[0]
    assert live_record.fail_build(db, "Movies", "One", 3, first["gen"]) is False
    live_record.queue_build(db, "Movies", "One", "one")
    again = live_record.pending_builds(db)[0]
    assert (again["attempts"], again["gen"]) == (1, first["gen"])

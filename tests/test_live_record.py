"""Live collection record (modules/live_record.py, epic #5634 S2)."""

from __future__ import annotations

import ast
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
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


def snapshot(name="Picks", library="Movies", members=(), wanted=(), previous_members=None, built=True, present=True, id_type="tmdb", **extra):
    snap = {
        "library": library,
        "name": name,
        "source_file": "picks",
        "source_path": "/config/movies/picks.yml",
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


METADATA = SimpleNamespace(get_file_name=lambda: "picks", path="/config/movies/picks.yml")


# ── storage ───────────────────────────────────────────────────────────


class TestStorage:
    def test_schema_is_wal_and_versioned(self, db):
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA user_version").fetchone()[0] == live_record.SCHEMA_VERSION
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == live_record.BUSY_TIMEOUT_MS

    def test_reopen_keeps_data(self, tmp_path):
        path = live_record.db_path(str(tmp_path))
        conn = live_record.connect(path)
        live_record.write_collection(conn, snapshot(members=[(1, 10, None)]))
        conn.close()
        conn = live_record.connect(path)
        assert [m["rating_key"] for m in live_record.get_members(conn, "Movies", "Picks")] == [1]
        conn.close()

    def test_first_record_diffs_against_the_runs_starting_membership(self, db):
        # Before this run Plex held 1 and 2; the run added 3 and removed 2.
        events = live_record.write_collection(db, snapshot(members=[(1, 10, None), (3, 30, None)], previous_members=[1, 2]))
        assert sorted(events, key=str) == sorted([(live_record.MEMBER_ADDED, 3, 30), (live_record.MEMBER_REMOVED, 2, None)], key=str)

    def test_first_record_without_known_start_writes_no_member_events(self, db):
        assert live_record.write_collection(db, snapshot(members=[(1, 10, None)], previous_members=None)) == []

    def test_second_record_diffs_against_the_first(self, db):
        live_record.write_collection(db, snapshot(members=[(1, 10, None), (2, 20, None)], wanted=[(40, False, False, None)], previous_members=[1, 2]))
        events = live_record.write_collection(db, snapshot(members=[(2, 20, None), (4, 40, None)], wanted=[(50, False, True, None)]))
        assert set(events) == {
            (live_record.MEMBER_ADDED, 4, 40),
            (live_record.MEMBER_REMOVED, 1, 10),
            (live_record.WANTED_ADDED, None, 50),
            (live_record.WANTED_REMOVED, None, 40),
        }
        history = live_record.get_history(db, "Movies", "Picks")
        assert {(h["event"], h["rating_key"], h["ext_id"]) for h in history} == set(events)

    def test_untracked_members_do_not_wipe_history_diff_base(self, db):
        live_record.write_collection(db, snapshot(members=[(1, 10, None)], previous_members=[]))
        live_record.write_collection(db, snapshot(members=None))
        row = live_record.get_collection(db, "Movies", "Picks")
        assert row["members_tracked"] == 0
        assert live_record.get_members(db, "Movies", "Picks") == []

    def test_failed_write_rolls_back(self, db):
        live_record.write_collection(db, snapshot(members=[(1, 10, None)], previous_members=[]))
        bad = snapshot(members=[(2, 20, None)])
        bad["wanted"] = [("not", "a", "tuple", "of-four", "!")]
        with pytest.raises(ValueError):
            live_record.write_collection(db, bad)
        assert [m["rating_key"] for m in live_record.get_members(db, "Movies", "Picks")] == [1]
        assert not db.in_transaction


class TestPartialRuns:
    def test_recording_one_collection_leaves_the_others(self, db):
        live_record.write_collection(db, snapshot(name="A", members=[(1, 10, None)], previous_members=[]))
        live_record.write_collection(db, snapshot(name="B", members=[(2, 20, None)], previous_members=[]))
        # A scoped run (--run-collections B) records only B.
        live_record.write_collection(db, snapshot(name="B", members=[(3, 30, None)]))
        assert [m["rating_key"] for m in live_record.get_members(db, "Movies", "A")] == [1]
        assert [m["rating_key"] for m in live_record.get_members(db, "Movies", "B")] == [3]

    def test_prune_drops_only_unconfigured_collections_in_that_library(self, db):
        live_record.write_collection(db, snapshot(name="Keep", wanted=[(1, False, False, None)]))
        live_record.write_collection(db, snapshot(name="Gone", wanted=[(2, False, False, None)]))
        live_record.write_collection(db, snapshot(name="Gone", library="TV Shows", id_type="tvdb"))
        assert live_record.prune_library(db, "Movies", ["Keep"]) == ["Gone"]
        assert live_record.get_collection(db, "Movies", "Gone") is None
        assert live_record.find_waiting(db, "tmdb", 2) == []
        assert live_record.get_collection(db, "Movies", "Keep") is not None
        assert live_record.get_collection(db, "TV Shows", "Gone") is not None


class TestConcurrentWriters:
    def test_two_writers_interleave_without_losing_writes(self, tmp_path):
        path = live_record.db_path(str(tmp_path))
        live_record.connect(path).close()

        def writer(prefix):
            conn = live_record.connect(path)
            try:
                for i in range(40):
                    live_record.write_collection(conn, snapshot(name=f"{prefix}{i}", members=[(i, i, None)], wanted=[(1000 + i, False, False, None)], previous_members=[]))
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(writer, ["sweep-", "trigger-"]))
        conn = live_record.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0] == 80
        assert conn.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 80
        conn.close()

    def test_writer_waits_for_a_held_write_lock(self, tmp_path):
        path = live_record.db_path(str(tmp_path))
        holder = live_record.connect(path)
        holder.execute("BEGIN IMMEDIATE")
        started = threading.Event()
        result = {}

        def writer():
            conn = live_record.connect(path)
            started.set()
            try:
                result["events"] = live_record.write_collection(conn, snapshot(members=[(1, 10, None)], previous_members=[]))
            finally:
                conn.close()

        thread = threading.Thread(target=writer)
        thread.start()
        started.wait()
        holder.execute("COMMIT")
        holder.close()
        thread.join()
        assert result["events"] == [(live_record.MEMBER_ADDED, 1, 10)]

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
        live_record.write_collection(db, snapshot(name="New", present=False, members=[], wanted=[(7, False, False, None)]))
        rows = live_record.find_waiting(db, "tmdb", 7)
        assert [(r["name"], r["present"], r["held_back"], r["position"]) for r in rows] == [("A", 1, 1, 3), ("New", 0, 0, None)]
        assert rows[0]["source_file"] == "picks"
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
        snap = live_record.build_snapshot(builder, library, METADATA, add_ran=True, sync_ran=True)
        assert snap["members"] == [(1, 10, None), (3, 30, None)]
        assert snap["previous_members"] == [1, 2]
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

    def test_wanted_flags_filtered_and_held_back(self):
        builder = fake_builder(
            missing_movies=[7, 8, 9, 7],
            missing_filtered_movies=[8],
            held_back_radarr=[{"title": "Nine", "id": 9}],
        )
        snap = live_record.build_snapshot(builder, fake_library(), METADATA, add_ran=False, sync_ran=False)
        assert snap["wanted"] == [(7, False, False, None), (8, True, False, None), (9, False, True, None)]

    def test_show_library_uses_tvdb(self):
        builder = fake_builder(missing_movies=[1], missing_shows=[2], remove_item_map={5: None})
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
        assert snap["wanted"] == [(20, False, False, 0)]

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
        builder = fake_builder(remove_item_map={1: None}, found_items=[item(1), item(2)], missing_movies=[30], held_back_radarr=[{"title": "T", "id": 30}])
        snap = live_record.build_snapshot(builder, fake_library({1: 10, 2: 20}), METADATA, add_ran=True, sync_ran=True)
        events = live_record.write_collection(db, snap)
        assert events == [(live_record.MEMBER_ADDED, 2, 20)]
        assert live_record.find_waiting(db, "tmdb", 30)[0]["held_back"] == 1


# ── builder capture points ────────────────────────────────────────────


class TestBuilderCapture:
    def test_run_missing_records_filtered_ids(self, monkeypatch):
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
        # 3 failed its TMDb lookup: it stays wanted rather than being treated as filtered.
        assert builder.missing_filtered_movies == [2]

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
        moves = []

        def move(obj, it, previous):
            moves.append(it.ratingKey)
            if it.ratingKey == 2:
                raise Failed("nope")

        builder = fake_builder(Type="Collection", custom_sort="custom.asc", found_items=[item(1), item(2)], items=[], library=SimpleNamespace(moveItem=move))
        builder.sort_collection()
        assert builder.sort_completed is False
        builder.items = [item(1), item(2)]
        builder.sort_collection()
        assert builder.sort_completed is True


# ── kometa.py wiring (static: importing kometa.py parses argv) ───────


def _function(name):
    tree = ast.parse((REPO_ROOT / "kometa.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in kometa.py")


def _calls(node, name):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name]


class TestWiring:
    def test_record_runs_after_the_except_chain_for_every_collection(self):
        loop = next(n for n in ast.walk(_function("run_collection")) if isinstance(n, ast.For))
        body_types = [type(stmt).__name__ for stmt in loop.body]
        try_index = body_types.index("Try")
        record_if = [stmt for stmt in loop.body[try_index + 1 :] if isinstance(stmt, ast.If) and _calls(stmt, "record_live_collection")]
        assert record_if, "record_live_collection must run after the try/except, not inside a success path"

    def test_record_is_not_gated_on_missing_items(self):
        source = ast.unparse(_function("run_collection"))
        settled = source.index("live_ran = (builder, add_ran, sync_ran)")
        missing_gate = source.index("builder.do_missing and (len(builder.missing_movies)")
        # The snapshot point sits outside the do_missing block (same indentation as the builder loop's if).
        assert settled > missing_gate
        assert "live_ran = (builder, add_ran, sync_ran)" not in ast.unparse(next(n for n in ast.walk(_function("run_collection")) if isinstance(n, ast.If) and "builder.do_missing" in ast.unparse(n.test)))

    def test_item_runs_never_touch_the_record(self):
        for name in ("record_live_collection", "prune_live_record"):
            first = _function(name).body[1]
            assert isinstance(first, ast.If) and ast.unparse(first.test) == "config.run_items"

    def test_prune_only_on_full_runs(self):
        run_libraries = _function("run_libraries")
        prune_ifs = [n for n in ast.walk(run_libraries) if isinstance(n, ast.If) and any(isinstance(stmt, ast.Expr) and _calls(stmt, "prune_live_record") for stmt in n.body)]
        assert len(prune_ifs) == 1
        assert ast.unparse(prune_ifs[0].test) == "not targeted_run and (not run_args['tests'])"


def test_connect_rejects_a_non_database(tmp_path):
    path = tmp_path / "live-collections.db"
    path.write_bytes(b"not a database at all, just bytes" * 10)
    with pytest.raises(sqlite3.DatabaseError):
        live_record.connect(str(path))

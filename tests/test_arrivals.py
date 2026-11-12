"""Arrival step of a per-item run (modules/arrivals.py, epic #5634 S3), against a fake Plex."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from modules import arrivals, live_record
from modules.util import Failed
from tests.conftest import FakeLogger

NOW = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)


class FakePlex:
    """Collections in Plex: name -> {"sort": collectionSort, "keys": [ratingKey, ...] in Plex's order}."""

    def __init__(self, is_movie=True, ext_ids=None, imdb_ids=None, collections=None, move_lands=True):
        self.name = "Movies" if is_movie else "TV Shows"
        self.original_mapping_name = "movies-config-key" if is_movie else "tv-config-key"
        self.is_movie = is_movie
        ext_ids = ext_ids or {}
        self.movie_rating_key_map = dict(ext_ids) if is_movie else {}
        self.show_rating_key_map = {} if is_movie else dict(ext_ids)
        self.imdb_rating_key_map = imdb_ids or {}
        self.cached_items = {}
        self.collections = collections or {}
        self.items = {rk: SimpleNamespace(ratingKey=rk, title=f"Item {rk}", year=2000 + rk % 100, addedAt=NOW - timedelta(minutes=5)) for rk in range(1, 200)}
        self.move_lands = move_lands
        self.adds = []
        self.moves = []

    def get_collection(self, name, debug=True):
        if name not in self.collections:
            raise Failed(f"Plex Error: Collection {name} not found")
        return SimpleNamespace(title=name, collectionSort=self.collections[name]["sort"])

    def get_collection_items(self, collection, smart_label_collection):
        assert smart_label_collection is False
        return [self.items[rk] for rk in self.collections[collection.title]["keys"]]

    def alter_collection(self, items, collection, smart_label_collection=False, add=True):
        assert add and not smart_label_collection
        for item in items:
            self.adds.append((collection, item.ratingKey))
            keys = self.collections[collection]["keys"]
            if item.ratingKey not in keys:
                keys.append(item.ratingKey)  # Plex appends a new member at the end of a custom order

    def moveItem(self, obj, item, after):
        self.moves.append((obj.title, item.ratingKey, None if after is None else after.ratingKey))
        if not self.move_lands:
            return
        keys = self.collections[obj.title]["keys"]
        keys.remove(item.ratingKey)
        keys.insert(0 if after is None else keys.index(after.ratingKey) + 1, item.ratingKey)

    def fetch_item(self, rating_key):
        return self.items[int(rating_key)]


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    logger = FakeLogger()
    logger.stacktrace = lambda: None
    monkeypatch.setattr(arrivals, "logger", logger)
    return logger


@pytest.fixture
def db(tmp_path):
    conn = live_record.connect(live_record.db_path(str(tmp_path)))
    yield conn
    conn.close()


def record(conn, name="Picks", library="Movies", id_type="tmdb", members=(), wanted=(), order=None, present=True, observed_at=None):
    """Write a sweep's snapshot. members: (rating_key, ext_id, position); wanted: (ext_id, filtered, position)."""
    snap = {
        "library": library,
        "name": name,
        "mapping_name": name,
        "source_file": "picks",
        "source_path": "/config/movies/picks.yml",
        "id_type": id_type,
        "built": True,
        "present": present,
        "kind": "list",
        "missing_checked": True,
        "collection_order": order,
        "order_recorded": order is not None,
        "members": list(members),
        "previous_members": None,
        "wanted": [(ext_id, filtered, False, pos) for ext_id, filtered, pos in wanted],
    }
    if observed_at is not None:
        snap["observed_at"] = observed_at
    live_record.write_collection(conn, snap)


def outbox(conn):
    return live_record.unpublished_arrivals(conn)


# ── add ───────────────────────────────────────────────────────────────


class TestAdd:
    def test_a_waiting_collection_gets_the_film_and_the_record_follows(self, db):
        plex = FakePlex(ext_ids={10: 510, 11: 511}, imdb_ids={11: "tt0000511"}, collections={"Picks": {"sort": 0, "keys": [10]}})
        record(db, members=[(10, 510, None)], wanted=[(511, False, None)])
        assert arrivals.handle_item(db, plex, 11, now=NOW) is True
        assert plex.adds == [("Picks", 11)]
        assert plex.collections["Picks"]["keys"] == [10, 11]
        assert plex.moves == []
        assert {m["rating_key"] for m in live_record.get_members(db, "Movies", "Picks")} == {10, 11}
        assert live_record.find_waiting(db, "tmdb", 511, now=NOW) == []
        history = live_record.get_history(db, "Movies", "Picks")
        assert history[0]["event"] == live_record.MEMBER_ADDED and history[0]["rating_key"] == 11 and history[0]["source"] == live_record.SOURCE_TRIGGER

    def test_the_event_names_the_item_and_its_collections(self, db):
        plex = FakePlex(ext_ids={11: 511}, imdb_ids={11: "tt0000511"}, collections={"Picks": {"sort": 0, "keys": []}})
        record(db, wanted=[(511, False, None)])
        arrivals.handle_item(db, plex, 11, now=NOW)
        [row] = outbox(db)
        assert (row["library"], row["rating_key"]) == ("Movies", 11)
        payload = row["payload"]
        assert payload["v"] == arrivals.PAYLOAD_VERSION == 1
        assert (payload["media_type"], payload["tmdb_id"], payload["tvdb_id"], payload["imdb_id"]) == ("movie", 511, None, "tt0000511")
        assert payload["title"] == "Item 11" and payload["year"] == 2011
        assert payload["rating_key"] == "11"
        assert payload["arrived_at"] == "2026-11-12T18:00:00Z"
        assert payload["added_at"] == "2026-11-12T17:55:00Z"
        assert payload["collections"] == [{"name": "Picks", "mapping_name": "Picks", "source_file": "picks", "added": True, "position": None, "slotted": False}]

    def test_every_waiting_collection_gets_it(self, db):
        plex = FakePlex(ext_ids={11: 511}, collections={"A": {"sort": 0, "keys": []}, "B": {"sort": 0, "keys": []}})
        record(db, name="A", wanted=[(511, False, None)])
        record(db, name="B", wanted=[(511, False, None)])
        assert arrivals.handle_item(db, plex, 11, now=NOW) is True
        assert sorted(plex.adds) == [("A", 11), ("B", 11)]
        assert [c["name"] for c in outbox(db)[0]["payload"]["collections"]] == ["A", "B"]

    def test_an_id_the_filters_never_passed_is_not_added(self, db):
        # run_missing never evaluated it (filter_state unknown): the next scheduled sync could
        # filter it out and remove it, so the trigger leaves it to the sweep.
        plex = FakePlex(ext_ids={11: 511}, collections={"A": {"sort": 0, "keys": []}, "B": {"sort": 0, "keys": []}})
        record(db, name="A", wanted=[(511, False, None)])
        record(db, name="B", wanted=[(511, None, None)])
        assert arrivals.handle_item(db, plex, 11, now=NOW) is True
        assert plex.adds == [("A", 11)]
        assert [c["name"] for c in outbox(db)[0]["payload"]["collections"]] == ["A"]

    def test_only_what_the_record_wants_is_added(self, db):
        plex = FakePlex(ext_ids={11: 511}, collections={"Picks": {"sort": 0, "keys": []}, "Other": {"sort": 0, "keys": []}})
        record(db, name="Picks", wanted=[(999, False, None)])
        record(db, name="Other", wanted=[(511, True, None)])  # run_missing filtered it out
        assert arrivals.handle_item(db, plex, 11, now=NOW) is True
        assert plex.adds == []
        assert outbox(db)[0]["payload"]["collections"] == []

    def test_a_stale_record_adds_nothing(self, db):
        plex = FakePlex(ext_ids={11: 511}, collections={"Picks": {"sort": 0, "keys": []}})
        record(db, wanted=[(511, False, None)])
        later = NOW + live_record.STALE_AFTER + timedelta(days=1)
        assert arrivals.handle_item(db, plex, 11, now=later) is True
        assert plex.adds == []

    def test_another_library_waiting_on_the_same_id_is_not_touched(self, db):
        plex = FakePlex(ext_ids={11: 511}, collections={"Picks": {"sort": 0, "keys": []}})
        record(db, name="Picks", library="Movies 4K", wanted=[(511, False, None)])
        assert arrivals.handle_item(db, plex, 11, now=NOW) is True
        assert plex.adds == []


class TestNothingWaiting:
    def test_no_waiting_collection_changes_nothing_but_still_announces(self, db):
        plex = FakePlex(ext_ids={11: 511}, collections={"Picks": {"sort": 2, "keys": [10]}})
        record(db, members=[(10, 510, 0)], order="custom.asc")
        assert arrivals.handle_item(db, plex, 11, now=NOW) is True
        assert plex.adds == [] and plex.moves == []
        assert live_record.get_history(db, "Movies", "Picks") == []
        [row] = outbox(db)
        assert row["payload"]["collections"] == []

    def test_an_item_with_only_an_imdb_id_is_announced_with_no_collections(self, db):
        plex = FakePlex(ext_ids={}, imdb_ids={11: "tt0000511"}, collections={"Picks": {"sort": 0, "keys": []}})
        record(db, wanted=[(511, False, None)])
        assert arrivals.handle_item(db, plex, 11, now=NOW) is True
        assert plex.adds == []
        [row] = outbox(db)
        assert (row["payload"]["tmdb_id"], row["payload"]["imdb_id"], row["payload"]["collections"]) == (None, "tt0000511", [])

    def test_an_item_kometa_has_no_id_for_fails_loud_so_the_trigger_retries_it(self, db, quiet):
        # Exit 0 here would drop the arrival: the trigger only announces from Plex's own ids when
        # an item runs out of retries.
        plex = FakePlex(ext_ids={}, collections={"Picks": {"sort": 0, "keys": []}})
        record(db, wanted=[(511, False, None)])
        assert arrivals.handle_item(db, plex, 11, now=NOW) is False
        assert plex.adds == [] and outbox(db) == []
        assert any("11" in m for m in quiet.error_messages) and quiet.info_messages == []

    def test_values_the_contract_would_reject_go_out_as_null(self, db):
        plex = FakePlex(ext_ids={11: 511}, imdb_ids={11: "nm0000001"}, collections={})
        plex.items[11].year = 0
        arrivals.handle_item(db, plex, 11, now=NOW)
        [row] = outbox(db)
        assert (row["payload"]["imdb_id"], row["payload"]["year"]) == (None, None)


# ── ids ───────────────────────────────────────────────────────────────


class TestIds:
    def test_a_film_matches_by_tmdb_id_only(self, db):
        plex = FakePlex(ext_ids={11: 511}, collections={"Picks": {"sort": 0, "keys": []}})
        record(db, name="Shows-ish", library="Movies", id_type="tvdb", wanted=[(511, False, None)])
        assert arrivals.handle_item(db, plex, 11, now=NOW) is True
        assert plex.adds == []

    def test_a_show_matches_by_tvdb_id(self, db):
        plex = FakePlex(is_movie=False, ext_ids={40: 81189}, imdb_ids={40: "tt0903747"}, collections={"Crime": {"sort": 0, "keys": []}})
        record(db, name="Crime", library="TV Shows", id_type="tvdb", wanted=[(81189, False, None)])
        assert arrivals.handle_item(db, plex, 40, now=NOW) is True
        assert plex.adds == [("Crime", 40)]
        payload = outbox(db)[0]["payload"]
        assert (payload["media_type"], payload["tmdb_id"], payload["tvdb_id"], payload["imdb_id"]) == ("show", None, 81189, "tt0903747")

    def test_a_show_is_not_matched_by_a_tmdb_wanted_row(self, db):
        plex = FakePlex(is_movie=False, ext_ids={40: 81189}, collections={"Crime": {"sort": 0, "keys": []}})
        record(db, name="Crime", library="TV Shows", id_type="tmdb", wanted=[(81189, False, None)])
        assert arrivals.handle_item(db, plex, 40, now=NOW) is True
        assert plex.adds == []


# ── slotting ──────────────────────────────────────────────────────────


class TestSlot:
    def test_a_custom_ordered_collection_gets_it_in_its_recorded_slot(self, db):
        # Recorded order: 501 (pos 0), 502 (pos 1), wanted 511 (pos 2), 503 (pos 3)
        plex = FakePlex(ext_ids={1: 501, 2: 502, 3: 503, 11: 511}, collections={"Top": {"sort": arrivals.CUSTOM_SORT, "keys": [1, 2, 3]}})
        record(db, name="Top", members=[(1, 501, 0), (2, 502, 1), (3, 503, 3)], wanted=[(511, False, 2)], order="custom.asc")
        assert arrivals.handle_item(db, plex, 11, now=NOW) is True
        assert plex.collections["Top"]["keys"] == [1, 2, 11, 3]
        assert plex.moves == [("Top", 11, 2)]
        entry = outbox(db)[0]["payload"]["collections"][0]
        # The record counts from 0, the event from 1.
        assert entry["added"] is True and entry["slotted"] is True and entry["position"] == 3
        assert {m["rating_key"]: m["position"] for m in live_record.get_members(db, "Movies", "Top")}[11] == 2

    def test_position_zero_goes_to_the_start(self, db):
        plex = FakePlex(ext_ids={1: 501, 11: 511}, collections={"Top": {"sort": arrivals.CUSTOM_SORT, "keys": [1]}})
        record(db, name="Top", members=[(1, 501, 1)], wanted=[(511, False, 0)], order="custom.asc")
        arrivals.handle_item(db, plex, 11, now=NOW)
        assert plex.collections["Top"]["keys"] == [11, 1]
        assert plex.moves == [("Top", 11, None)]

    def test_the_slot_follows_plex_not_the_record(self, db):
        # The recorded predecessor (502, pos 1) is no longer in the Plex collection: slot after 501.
        plex = FakePlex(ext_ids={1: 501, 2: 502, 3: 503, 11: 511}, collections={"Top": {"sort": arrivals.CUSTOM_SORT, "keys": [1, 3]}})
        record(db, name="Top", members=[(1, 501, 0), (2, 502, 1), (3, 503, 3)], wanted=[(511, False, 2)], order="custom.asc")
        arrivals.handle_item(db, plex, 11, now=NOW)
        assert plex.collections["Top"]["keys"] == [1, 11, 3]

    def test_a_collection_not_custom_ordered_is_never_moved(self, db):
        plex = FakePlex(ext_ids={1: 501, 11: 511}, collections={"Picks": {"sort": 1, "keys": [1]}})
        record(db, name="Picks", members=[(1, 501, None)], wanted=[(511, False, None)], order=None)
        arrivals.handle_item(db, plex, 11, now=NOW)
        assert plex.moves == []
        entry = outbox(db)[0]["payload"]["collections"][0]
        assert entry["slotted"] is False and entry["position"] is None

    def test_plex_not_in_custom_order_is_not_moved_even_if_the_record_says_custom(self, db):
        plex = FakePlex(ext_ids={1: 501, 11: 511}, collections={"Top": {"sort": 1, "keys": [1]}})
        record(db, name="Top", members=[(1, 501, 1)], wanted=[(511, False, 0)], order="custom.asc")
        assert arrivals.handle_item(db, plex, 11, now=NOW) is True
        assert plex.moves == []
        assert plex.adds == [("Top", 11)]
        assert outbox(db)[0]["payload"]["collections"][0]["slotted"] is False

    def test_a_move_plex_did_not_apply_is_a_failure(self, db):
        plex = FakePlex(ext_ids={1: 501, 11: 511}, collections={"Top": {"sort": arrivals.CUSTOM_SORT, "keys": [1]}}, move_lands=False)
        record(db, name="Top", members=[(1, 501, 1)], wanted=[(511, False, 0)], order="custom.asc")
        assert arrivals.handle_item(db, plex, 11, now=NOW) is False
        # Not recorded as a member: the next run tries again.
        assert 11 not in {m["rating_key"] for m in live_record.get_members(db, "Movies", "Top")}


# ── idempotency ───────────────────────────────────────────────────────


class TestIdempotent:
    def test_a_second_run_changes_nothing(self, db):
        plex = FakePlex(ext_ids={1: 501, 2: 502, 11: 511}, collections={"Top": {"sort": arrivals.CUSTOM_SORT, "keys": [1, 2]}})
        record(db, name="Top", members=[(1, 501, 0), (2, 502, 2)], wanted=[(511, False, 1)], order="custom.asc")
        arrivals.handle_item(db, plex, 11, now=NOW)
        history = live_record.get_history(db, "Movies", "Top")
        adds, moves = list(plex.adds), list(plex.moves)
        assert arrivals.handle_item(db, plex, 11, now=NOW + timedelta(minutes=1)) is True
        assert plex.adds == adds and plex.moves == moves
        assert live_record.get_history(db, "Movies", "Top") == history

    def test_an_item_the_sweep_already_added_is_not_added_again(self, db):
        # The sweep added it to Plex but the record (written before) still lists it as wanted.
        plex = FakePlex(ext_ids={1: 501, 11: 511}, collections={"Picks": {"sort": 0, "keys": [1, 11]}})
        record(db, name="Picks", members=[(1, 501, None)], wanted=[(511, False, None)])
        assert arrivals.handle_item(db, plex, 11, now=NOW) is True
        assert plex.adds == []
        entry = outbox(db)[0]["payload"]["collections"][0]
        assert entry["added"] is False
        assert live_record.find_waiting(db, "tmdb", 511, now=NOW) == []

    def test_a_published_arrival_is_not_announced_again(self, db):
        plex = FakePlex(ext_ids={11: 511}, collections={"Picks": {"sort": 0, "keys": []}})
        record(db, wanted=[(511, False, None)])
        arrivals.handle_item(db, plex, 11, now=NOW)
        live_record.mark_published(db, "Movies", 11)
        arrivals.handle_item(db, plex, 11, now=NOW + timedelta(hours=1))
        assert outbox(db) == []


# ── a waiting collection not in Plex ──────────────────────────────────


class TestAbsent:
    @pytest.mark.parametrize("present", [False, True])
    def test_a_missing_collection_is_queued_for_a_scoped_run_not_created_bare(self, db, present):
        plex = FakePlex(ext_ids={11: 511}, collections={})
        record(db, name="Picks", wanted=[(511, False, None)], present=present)
        assert arrivals.handle_item(db, plex, 11, now=NOW) is True
        assert plex.adds == []
        assert live_record.pending_builds(db) == [{"library": "movies-config-key", "mapping_name": "Picks", "source_file": "picks", "attempts": 0}]
        # The item is not in that collection yet, so the event does not list it.
        assert outbox(db)[0]["payload"]["collections"] == []


# ── the run ───────────────────────────────────────────────────────────


class TestRun:
    def test_run_handles_every_run_item_in_the_library_and_never_raises(self, tmp_path, monkeypatch):
        conn = live_record.connect(live_record.db_path(str(tmp_path)))
        record(conn, wanted=[(511, False, None)])
        conn.close()
        plex = FakePlex(ext_ids={11: 511}, collections={"Picks": {"sort": 0, "keys": []}})
        config = SimpleNamespace(default_dir=str(tmp_path), run_items=[11])
        assert arrivals.run(config, plex, now=NOW) is True
        assert plex.adds == [("Picks", 11)]

    def test_a_key_the_run_did_not_load_fails_the_run_so_it_is_retried(self, tmp_path, quiet):
        # Exit 0 would make the trigger forget it unannounced.
        conn = live_record.connect(live_record.db_path(str(tmp_path)))
        record(conn, wanted=[(511, False, None)])
        conn.close()
        plex = FakePlex(ext_ids={11: 511}, collections={"Picks": {"sort": 0, "keys": []}})
        config = SimpleNamespace(default_dir=str(tmp_path), run_items=[11, 77])
        assert arrivals.run(config, plex, now=NOW) is False
        assert plex.adds == [("Picks", 11)]
        assert any("77" in m for m in quiet.error_messages)

    def test_a_failing_item_makes_the_run_report_failure(self, tmp_path, monkeypatch):
        plex = FakePlex(ext_ids={11: 511, 12: 512}, collections={})
        monkeypatch.setattr(arrivals, "handle_item", lambda conn, library, rk, now=None: (_ for _ in ()).throw(RuntimeError("boom")) if rk == 11 else True)
        config = SimpleNamespace(default_dir=str(tmp_path), run_items=[11, 12])
        assert arrivals.run(config, plex, now=NOW) is False

    def test_an_unopenable_record_is_a_failure_not_a_crash(self, tmp_path):
        config = SimpleNamespace(default_dir=str(tmp_path / "missing" / "dir"), run_items=[11])
        assert arrivals.run(config, FakePlex(ext_ids={11: 511}), now=NOW) is False


# ── a sweep snapshot taken before an arrival never reverts it ─────────


class TestStaleSnapshot:
    def test_a_snapshot_observed_before_the_arrival_keeps_the_arrival(self, db):
        plex = FakePlex(ext_ids={1: 501, 11: 511}, collections={"Picks": {"sort": 0, "keys": [1]}})
        record(db, members=[(1, 501, None)], wanted=[(511, False, None)])
        observed = NOW - timedelta(minutes=10)
        arrivals.handle_item(db, plex, 11, now=NOW)
        # A sweep that read the collection before the arrival writes what it saw.
        record(db, members=[(1, 501, None)], wanted=[(511, False, None)], observed_at=observed)
        members = {m["rating_key"] for m in live_record.get_members(db, "Movies", "Picks")}
        assert members == {1, 11}
        assert live_record.find_waiting(db, "tmdb", 511, now=NOW) == []
        assert not [h for h in live_record.get_history(db, "Movies", "Picks") if h["event"] == live_record.MEMBER_REMOVED]

    def test_a_snapshot_observed_after_the_arrival_is_the_truth(self, db):
        plex = FakePlex(ext_ids={1: 501, 11: 511}, collections={"Picks": {"sort": 0, "keys": [1]}})
        record(db, members=[(1, 501, None)], wanted=[(511, False, None)])
        arrivals.handle_item(db, plex, 11, now=NOW)
        # The sweep ran later and its builder no longer wants the film: sync removed it.
        record(db, members=[(1, 501, None)], wanted=[], observed_at=NOW + timedelta(hours=1))
        assert {m["rating_key"] for m in live_record.get_members(db, "Movies", "Picks")} == {1}
        assert any(h["event"] == live_record.MEMBER_REMOVED and h["rating_key"] == 11 for h in live_record.get_history(db, "Movies", "Picks"))

    def test_a_kept_arrival_becomes_a_run_member_once_a_sweep_sees_it(self, db):
        plex = FakePlex(ext_ids={1: 501, 11: 511}, collections={"Picks": {"sort": 0, "keys": [1]}})
        record(db, members=[(1, 501, None)], wanted=[(511, False, None)])
        arrivals.handle_item(db, plex, 11, now=NOW)
        record(db, members=[(1, 501, None), (11, 511, None)], wanted=[], observed_at=NOW + timedelta(hours=1))
        sources = {r["rating_key"]: r["source"] for r in db.execute("SELECT rating_key, source FROM members WHERE name = 'Picks'")}
        assert sources == {1: live_record.SOURCE_RUN, 11: live_record.SOURCE_RUN}


# ── the wire contract (kometa-contract item-arrived fixtures, #5634 S3n) ──

FIXTURES = Path(__file__).parent / "fixtures" / "kometa_contract"
# Exactly YYYY-MM-DDTHH:MM:SSZ: the contract rejects an offset (+00:00) and fractions.
ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _shape(value):
    """Keys and JSON types, recursively: what the strict zod schema checks besides value ranges."""
    if isinstance(value, dict):
        return {key: _shape(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    return type(value).__name__


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


def _check_values(payload):
    assert payload["v"] == 1
    assert re.fullmatch(r"\d+", payload["rating_key"])
    assert payload["media_type"] in ("movie", "show")
    assert ISO_UTC.match(payload["added_at"]) and ISO_UTC.match(payload["arrived_at"])
    assert payload["imdb_id"] is None or re.fullmatch(r"tt\d+", payload["imdb_id"])
    assert any(payload[key] is not None for key in ("tmdb_id", "tvdb_id", "imdb_id"))
    for entry in payload["collections"]:
        assert isinstance(entry["added"], bool) and isinstance(entry["slotted"], bool)
        assert entry["position"] is None or (isinstance(entry["position"], int) and entry["position"] >= 1)


class TestContract:
    def test_a_movie_event_matches_the_fixture(self, db):
        # One collection the film was added to and slotted in, one it was already in (not custom).
        fixture = _fixture("item-arrived.json")
        plex = FakePlex(ext_ids={1: 501, 11: 9426}, imdb_ids={11: "tt0091064"}, collections={"Body Horror": {"sort": arrivals.CUSTOM_SORT, "keys": [1]}, "Cronenberg": {"sort": 0, "keys": [11]}})
        record(db, name="Body Horror", members=[(1, 501, 0)], wanted=[(9426, False, 6)], order="custom.asc")
        record(db, name="Cronenberg", wanted=[(9426, False, None)])
        arrivals.handle_item(db, plex, 11, now=NOW)
        payload = outbox(db)[0]["payload"]
        payload = json.loads(json.dumps(payload))  # the wire form
        _check_values(payload)
        assert _shape(payload) == _shape(fixture)
        assert [(c["added"], c["position"], c["slotted"]) for c in payload["collections"]] == [(c["added"], c["position"], c["slotted"]) for c in fixture["collections"]]

    def test_a_plain_show_event_matches_the_fixture(self, db):
        fixture = _fixture("item-arrived-plain.json")
        plex = FakePlex(is_movie=False, ext_ids={40: 81189}, collections={})
        arrivals.handle_item(db, plex, 40, now=NOW)
        payload = json.loads(json.dumps(outbox(db)[0]["payload"]))
        _check_values(payload)
        assert _shape(payload) == _shape(fixture)
        assert payload["collections"] == [] and payload["imdb_id"] is None and payload["tmdb_id"] is None

    @pytest.mark.parametrize("name", ["item-arrived.json", "item-arrived-plain.json"])
    def test_the_fixtures_pass_the_value_checks(self, name):
        _check_values(_fixture(name))


# ── a run that fails part-way ─────────────────────────────────────────


class TestPartialFailure:
    def test_a_failed_collection_holds_the_event_and_the_retry_sends_it_complete(self, db):
        # Picks takes the film; the move in Top does not land. The event must not go out listing only
        # Picks: the trigger runs the item again, and that run's event lists both, Top as added.
        plex = FakePlex(ext_ids={1: 501, 11: 511}, collections={"Picks": {"sort": 0, "keys": []}, "Top": {"sort": arrivals.CUSTOM_SORT, "keys": [1]}}, move_lands=False)
        record(db, name="Picks", wanted=[(511, False, None)])
        record(db, name="Top", members=[(1, 501, 1)], wanted=[(511, False, 0)], order="custom.asc")
        assert arrivals.handle_item(db, plex, 11, now=NOW) is False
        assert outbox(db) == []
        plex.move_lands = True
        assert arrivals.handle_item(db, plex, 11, now=NOW + timedelta(minutes=3)) is True
        [row] = outbox(db)
        entries = {c["name"]: (c["added"], c["slotted"]) for c in row["payload"]["collections"]}
        assert entries == {"Picks": (True, False), "Top": (True, True)}
        assert plex.collections["Top"]["keys"] == [11, 1]

    def test_a_run_killed_right_after_the_add_still_reports_the_add(self, db):
        # Plex took the add, then the run died (timeout kill) before the verify read. The retry finds
        # the film already a member; the event must still say the trigger added it.
        class Killed(BaseException):
            pass

        plex = FakePlex(ext_ids={11: 511}, collections={"Picks": {"sort": 0, "keys": []}})
        record(db, name="Picks", wanted=[(511, False, None)])
        reads = []
        real = plex.get_collection_items

        def dies_after_the_add(collection, smart):
            reads.append(1)
            if len(reads) == 2:
                raise Killed()
            return real(collection, smart)

        plex.get_collection_items = dies_after_the_add
        with pytest.raises(Killed):
            arrivals.handle_item(db, plex, 11, now=NOW)
        assert outbox(db) == []  # held: the run never finished
        plex.get_collection_items = real
        assert arrivals.handle_item(db, plex, 11, now=NOW + timedelta(minutes=3)) is True
        [row] = outbox(db)
        assert [(c["name"], c["added"]) for c in row["payload"]["collections"]] == [("Picks", True)]
        assert plex.adds == [("Picks", 11)]

    def test_giving_up_publishes_what_the_last_run_managed(self, db):
        plex = FakePlex(ext_ids={1: 501, 11: 511}, collections={"Picks": {"sort": 0, "keys": []}, "Top": {"sort": arrivals.CUSTOM_SORT, "keys": [1]}}, move_lands=False)
        record(db, name="Picks", wanted=[(511, False, None)])
        record(db, name="Top", members=[(1, 501, 1)], wanted=[(511, False, 0)], order="custom.asc")
        arrivals.handle_item(db, plex, 11, now=NOW)
        assert live_record.release_arrival(db, 11) is True
        [row] = outbox(db)
        # Top has the film, out of its slot: the event says so rather than leaving it out.
        assert {c["name"]: (c["added"], c["slotted"]) for c in row["payload"]["collections"]} == {"Picks": (True, False), "Top": (True, False)}
        assert live_record.release_arrival(db, 11) is False

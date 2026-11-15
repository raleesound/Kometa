"""acquire: exhaustive (epic #5634 addendum 6): add everything with no drip, tag every wanted film exhaustive-<slug>, untag what leaves."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

import modules.builder  # noqa: F401
from modules import add_budget, exhaustive_reconcile, live_record
from modules.builder import BuilderValidationError, CollectionBuilder
from tests.conftest import FakeLogger

TAG = "exhaustive-picks"
IDS = {TAG: 1, "other": 2, "exhaustive-other": 3, "kl-picks": 4}


def _movie(tmdb_id, *labels):
    return SimpleNamespace(tmdbId=tmdb_id, title=f"T{tmdb_id}", tagsIds=[IDS[label] for label in labels])


def _radarr(monkeypatch, movies):
    monkeypatch.setattr("modules.radarr.logger", FakeLogger())
    from modules.radarr import Radarr

    r = Radarr.__new__(Radarr)
    r.api = MagicMock()
    r.api.all_movies.return_value = movies
    r.api.all_tags.return_value = [SimpleNamespace(id=i, label=label) for label, i in IDS.items()]
    return r


def _edits(radarr):
    return [(call.args[0], call.kwargs["apply_tags"]) for call in radarr.api.edit_multiple_movies.call_args_list]


class TestSlug:
    def test_the_tag_is_the_prefix_plus_a_kebab_slug(self):
        assert add_budget.exhaustive_tag("The Cinema Gremlins' Bottom 50") == "exhaustive-the-cinema-gremlins-bottom-50"

    def test_names_that_lose_letters_get_a_hash_so_they_never_share_a_tag(self):
        tags = {add_budget.exhaustive_tag(n) for n in ("映画", "Фильмы", "!!!", "Amélie", "Amelie", "Top 10")}
        assert len(tags) == 6
        assert add_budget.exhaustive_tag("映画").startswith("exhaustive-collection-")
        assert add_budget.exhaustive_tag("Amélie").startswith("exhaustive-am-lie-")
        assert add_budget.exhaustive_tag("Top 10") == "exhaustive-top-10"
        assert add_budget.exhaustive_tag("映画") == add_budget.exhaustive_tag("映画")


class TestSyncTag:
    def test_tags_wanted_films_already_in_radarr_and_skips_ones_that_are_not(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1), _movie(2), _movie(3)])
        assert r.sync_tag(TAG, {1, 2, 99}) == {1, 2}
        assert _edits(r) == [([1, 2], "add")]
        assert r.api.edit_multiple_movies.call_args.kwargs["tags"] == [TAG]

    def test_untags_ids_that_left_and_sends_nothing_when_nothing_changed(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1, TAG), _movie(2, TAG, "other"), _movie(3, "other")])
        assert r.sync_tag(TAG, {1}) == {1}
        assert _edits(r) == [([2], "remove")]
        r2 = _radarr(monkeypatch, [_movie(1, TAG), _movie(3)])
        assert r2.sync_tag(TAG, {1}) == {1}
        r2.api.edit_multiple_movies.assert_not_called()

    def test_held_ids_keep_their_tag(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1, TAG), _movie(2, TAG)])
        assert r.sync_tag(TAG, set(), {2}) == {2}
        assert _edits(r) == [([1], "remove")]

    def test_other_collections_tags_are_never_touched(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1, "exhaustive-other"), _movie(2, "kl-picks")])
        assert r.sync_tag(TAG, set()) == set()
        r.api.edit_multiple_movies.assert_not_called()


def _builder(monkeypatch, radarr, **attrs):
    monkeypatch.setattr("modules.builder.logger", FakeLogger())
    b = CollectionBuilder.__new__(CollectionBuilder)
    b.name = "Picks"
    b.Type = "Collection"
    b.exhaustive = True
    b.exhaustive_tagged = None
    b.exhaustive_tag_applied = None
    b.do_missing = True
    b.budget_blockers = []
    b.missing_movies = [1, 2, 3, 4]
    b.missing_passed_movies = [1, 2]
    b.missing_filtered_movies = [3]
    b.has_imdb_filters = False
    b.playlist = False
    b.smart_url = None
    b.collectionless = False
    b.radarr_details = {}
    b.library = SimpleNamespace(is_movie=True, Radarr=radarr, name="Movies")
    b.config = SimpleNamespace(config_path="/nonexistent/config.yml")
    for key, value in attrs.items():
        setattr(b, key, value)
    return b


class TestRunExhaustive:
    def test_tags_passed_ids_untags_filtered_and_holds_unevaluated(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1), _movie(3, TAG), _movie(4, TAG)])
        b = _builder(monkeypatch, r)
        b.run_exhaustive()
        # 2 is not in Radarr; 3 was filtered out; 4 was never evaluated so it keeps its tag
        assert _edits(r) == [([1], "add"), ([3], "remove")]
        assert b.exhaustive_tagged == {1, 4}
        assert b.exhaustive_tag_applied == TAG

    def test_imdb_filters_make_filtered_ids_hold_their_tag(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(3, TAG)])
        b = _builder(monkeypatch, r, has_imdb_filters=True)
        b.run_exhaustive()
        r.api.edit_multiple_movies.assert_not_called()
        assert b.exhaustive_tagged == {3}

    def test_a_blocker_tags_and_untags_nothing(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1), _movie(3, TAG)])
        b = _builder(monkeypatch, r, budget_blockers=["Builder x failed: boom"])
        b.run_exhaustive()
        r.api.edit_multiple_movies.assert_not_called()
        assert b.exhaustive_tagged is None
        assert b.exhaustive_tag_applied is None

    def test_nothing_wanted_after_a_run_that_wanted_something_untags_nothing(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1, TAG)])
        b = _builder(monkeypatch, r, missing_movies=[], missing_passed_movies=[], missing_filtered_movies=[])
        b.run_exhaustive(previous_wanted=3)
        r.api.edit_multiple_movies.assert_not_called()
        assert b.exhaustive_tagged == {1}

    def test_nothing_wanted_with_no_record_to_judge_by_untags_nothing(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1, TAG)])
        b = _builder(monkeypatch, r, missing_movies=[], missing_passed_movies=[], missing_filtered_movies=[])
        b.run_exhaustive()
        r.api.edit_multiple_movies.assert_not_called()

    def test_nothing_wanted_twice_running_untags_everything(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1, TAG)])
        b = _builder(monkeypatch, r, missing_movies=[], missing_passed_movies=[], missing_filtered_movies=[])
        b.run_exhaustive(previous_wanted=0)
        assert _edits(r) == [([1], "remove")]
        assert b.exhaustive_tagged == set()

    def test_any_error_is_logged_and_leaves_the_tags_alone(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1)])
        r.api.all_movies.side_effect = ConnectionError("down")
        b = _builder(monkeypatch, r, **{"library": SimpleNamespace(is_movie=True, Radarr=r, name="Movies")})
        modules.builder.logger.stacktrace = MagicMock()
        b.run_exhaustive(previous_wanted=1)
        assert b.exhaustive_tagged is None

    def test_a_radarr_error_is_logged_not_raised(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1)])
        r.api.edit_multiple_movies.side_effect = modules.builder.ArrException("down")
        b = _builder(monkeypatch, r)
        b.run_exhaustive()
        assert b.exhaustive_tagged is None

    def test_a_collection_without_the_key_is_untouched(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1)])
        b = _builder(monkeypatch, r, exhaustive=False)
        b.run_exhaustive()
        r.api.all_movies.assert_not_called()


class TestApply:
    def test_forces_add_missing_without_a_drip(self, monkeypatch):
        b = _builder(monkeypatch, MagicMock())
        b._apply_exhaustive()
        assert b.exhaustive is True and b.radarr_details == {"add_missing": True}

    @pytest.mark.parametrize("details", [{"add_missing": False}, {"add_missing_initial": 5, "add_missing_per_week": 1}, {"add_missing_ledger_tag": "kl-x"}])
    def test_conflicting_drip_settings_are_an_error(self, monkeypatch, details):
        b = _builder(monkeypatch, MagicMock(), radarr_details=dict(details))
        with pytest.raises(BuilderValidationError):
            b._apply_exhaustive()

    def test_shows_and_playlists_ignore_it_with_a_log_line(self, monkeypatch):
        b = _builder(monkeypatch, MagicMock())
        b.library.is_movie = False
        b._apply_exhaustive()
        assert b.exhaustive is False and b.radarr_details == {}
        assert any("only applies to movie" in m for m in modules.builder.logger.info_messages)

    def test_two_collections_that_map_to_one_tag_are_both_refused(self, monkeypatch, tmp_path):
        config = tmp_path / "config.yml"
        files = tmp_path / "movies.yml"
        config.write_text("libraries:\n  Movies:\n    collection_files:\n      - file: " + str(files) + "\n")
        files.write_text("collections:\n  Top 10:\n    acquire: exhaustive\n  Top-10!:\n    acquire: exhaustive\n")
        exhaustive_reconcile.forget_run()
        for name in ("Top 10", "Top-10!"):
            b = _builder(monkeypatch, MagicMock(), name=name, config=SimpleNamespace(config_path=str(config)))
            with pytest.raises(BuilderValidationError, match="share the Radarr tag"):
                b._apply_exhaustive()
        exhaustive_reconcile.forget_run()

    def test_the_same_name_in_two_libraries_in_one_run_is_refused(self, monkeypatch):
        exhaustive_reconcile.forget_run()
        _builder(monkeypatch, MagicMock())._apply_exhaustive()
        other = _builder(monkeypatch, MagicMock())
        other.library = SimpleNamespace(is_movie=True, Radarr=MagicMock(), name="Films 4K")
        with pytest.raises(BuilderValidationError):
            other._apply_exhaustive()
        exhaustive_reconcile.forget_run()

    def test_no_radarr_is_a_warning_not_a_crash(self, monkeypatch):
        b = _builder(monkeypatch, None)
        b._apply_exhaustive()
        assert b.exhaustive is False


def _snap(wanted, tagged):
    return {
        "library": "Movies",
        "name": "Picks",
        "mapping_name": "Picks",
        "source_file": "picks",
        "source_path": "/c/p.yml",
        "id_type": "tmdb",
        "built": True,
        "present": True,
        "kind": "list",
        "missing_checked": True,
        "collection_order": None,
        "order_recorded": False,
        "members": [],
        "previous_members": None,
        "wanted": [(i, False, False, None) for i in wanted],
        "tagged": tagged,
    }


class TestRecord:
    @pytest.fixture
    def conn(self, tmp_path):
        c = live_record.connect(live_record.db_path(str(tmp_path)))
        yield c
        c.close()

    def _tagged(self, conn):
        return {r["ext_id"] for r in conn.execute("SELECT ext_id FROM wanted WHERE tagged = 1")}

    def test_records_the_tagged_set_and_carries_it_when_a_run_did_not_evaluate_it(self, conn):
        live_record.write_collection(conn, _snap([1, 2, 3], {1, 2}))
        assert self._tagged(conn) == {1, 2}
        live_record.write_collection(conn, _snap([1, 2, 3], None))
        assert self._tagged(conn) == {1, 2}
        live_record.write_collection(conn, _snap([2, 3], {2}))
        assert self._tagged(conn) == {2}
        live_record.write_collection(conn, _snap([2, 3], set()))
        assert self._tagged(conn) == set()

    def test_a_carried_id_that_left_the_wanted_set_is_dropped(self, conn):
        live_record.write_collection(conn, _snap([1, 2], {1, 2}))
        live_record.write_collection(conn, _snap([2], None))
        assert self._tagged(conn) == {2}

    def test_a_v6_record_is_upgraded_in_place(self, tmp_path):
        import sqlite3

        path = live_record.db_path(str(tmp_path))
        old = sqlite3.connect(path)
        old.executescript(live_record.SCHEMA.replace("    tagged INTEGER NOT NULL DEFAULT 0,\n", "") + live_record.SCHEMA_V3)
        old.execute("INSERT INTO wanted VALUES ('Movies', 'Picks', 511, 0, 0, NULL)")
        old.execute("PRAGMA user_version = 6")
        old.commit()
        old.close()
        c = live_record.connect(path, log=FakeLogger())
        assert c.execute("SELECT tagged FROM wanted WHERE ext_id = 511").fetchone()[0] == 0
        c.close()

    def test_the_tag_a_run_applied_is_recorded_as_kometas_and_survives_a_rebuild(self, tmp_path):
        conn = live_record.connect(live_record.db_path(str(tmp_path)), log=FakeLogger())
        assert live_record.recorded_exhaustive_tags(conn) == set()
        live_record.write_collection(conn, _snap([1], {1}))
        assert live_record.recorded_exhaustive_tags(conn) == set()
        live_record.write_collection(conn, {**_snap([1], {1}), "exhaustive_tag": "Exhaustive-Picks"})
        assert live_record.recorded_exhaustive_tags(conn) == {"exhaustive-picks"}
        live_record.forget_exhaustive_tags(conn, {"exhaustive-picks", "exhaustive-other"})
        assert live_record.recorded_exhaustive_tags(conn) == set()
        live_record.write_collection(conn, {**_snap([1], {1}), "exhaustive_tag": "exhaustive-picks"})
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()
        conn = live_record.connect(live_record.db_path(str(tmp_path)), log=FakeLogger())
        assert live_record.recorded_exhaustive_tags(conn) == {"exhaustive-picks"}
        conn.close()

    def test_a_v7_record_is_upgraded_in_place_and_gains_the_tag_table(self, tmp_path):
        import sqlite3

        path = live_record.db_path(str(tmp_path))
        old = sqlite3.connect(path)
        old.executescript(live_record.SCHEMA + live_record.SCHEMA_V3)
        old.execute("DROP TABLE IF EXISTS exhaustive_tags")
        old.execute("INSERT INTO wanted VALUES ('Movies', 'Picks', 511, 0, 0, NULL, 1)")
        old.execute("PRAGMA user_version = 7")
        old.commit()
        old.close()
        c = live_record.connect(path, log=FakeLogger())
        assert c.execute("SELECT tagged FROM wanted WHERE ext_id = 511").fetchone()[0] == 1
        assert live_record.recorded_exhaustive_tags(c) == set()
        c.close()


class _StubRadarr(requests.adapters.BaseAdapter):
    """A Radarr that answers over a real requests.Session, so arrapi's real RadarrAPI/RadarrRawAPI classes run unmodified."""

    def __init__(self, movies):
        super().__init__()
        self.movies = movies  # {tmdbId: [tag ids]}
        self.tags = [{"id": 1, "label": "exhaustive-picks"}]
        self.requests = []
        self.timeouts = []

    def close(self):
        pass

    def send(self, request, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        path = request.path_url.split("?")[0]
        body = json.loads(request.body) if request.body else None
        self.requests.append((request.method, path, body))
        if path.endswith("/system/status"):
            payload = {"version": "5.0.0.0"}
        elif path.endswith("/tag"):
            payload = self.tags
        elif path.endswith("/movie"):
            payload = [{"id": i, "tmdbId": t, "title": f"T{t}", "tags": tags} for i, (t, tags) in enumerate(self.movies.items(), start=100)]
        elif path.endswith("/movie/editor"):
            payload = []
            for tmdb_id, ids in [(t, self.movies[t]) for t in self.movies if 100 + list(self.movies).index(t) in body["movieIds"]]:
                tag_ids = body.get("tags") or []
                self.movies[tmdb_id] = sorted(set(ids) | set(tag_ids)) if body.get("applyTags") == "add" else [i for i in ids if i not in tag_ids]
        elif re.search(r"/movie/\d+$", path):
            tmdb_id, tags = list(self.movies.items())[int(path.rsplit("/", 1)[1]) - 100]
            payload = {"id": int(path.rsplit("/", 1)[1]), "tmdbId": tmdb_id, "title": f"T{tmdb_id}", "tags": tags}
        elif request.method == "DELETE" and re.search(r"/tag/\d+$", path):
            self.tags = [t for t in self.tags if t["id"] != int(path.rsplit("/", 1)[1])]
            payload = {}
        else:
            raise AssertionError(f"unexpected Radarr call {request.method} {path}")
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        response.url = request.url
        return response


def _real_radarr(monkeypatch, movies):
    """A modules.radarr.Radarr holding a REAL arrapi.RadarrAPI built the way Kometa builds it: RadarrAPI(url, token, session=<requests session>)."""
    from arrapi import RadarrAPI

    monkeypatch.setattr("modules.radarr.logger", FakeLogger())
    from modules.radarr import Radarr

    session = requests.Session()
    stub = _StubRadarr(movies)
    session.mount("http://", stub)
    r = Radarr.__new__(Radarr)
    r.api = RadarrAPI("http://radarr:7878", "key", session=session)
    return r, stub


class TestRealArrapiShape:
    def test_the_real_api_keeps_its_session_on_the_raw_client_not_on_itself(self):
        from arrapi import RadarrAPI

        api = RadarrAPI("http://radarr:7878", "key", session=_session_with(_StubRadarr({})))
        assert not hasattr(api, "session") and api._raw.session is not None  # the shape that S9d's fake API hid

    def test_sync_tag_on_the_real_class_tags_with_a_timeout_on_every_request(self, monkeypatch):
        r, stub = _real_radarr(monkeypatch, {1: [], 2: [1], 3: []})
        stub.timeouts.clear()  # building the API probes system/status before sync_tag can bind: Kometa's own, out of scope here
        assert r.sync_tag(TAG, {1}) == {1}
        edits = [(method, path, body) for method, path, body in stub.requests if path.endswith("/movie/editor")]
        assert [m for m, _, _ in edits] == ["PUT", "PUT"] and {len(body["movieIds"]) for _, _, body in edits} == {1}
        assert {tuple(body["movieIds"]) for _, _, body in edits} == {(100,), (101,)}
        assert stub.timeouts and all(t == exhaustive_reconcile.TIMEOUT for t in stub.timeouts)

    def test_run_exhaustive_end_to_end_through_the_real_class_records_the_tag(self, monkeypatch):
        r, stub = _real_radarr(monkeypatch, {1: [], 2: []})
        b = _builder(monkeypatch, r, missing_passed_movies=[1], missing_movies=[1], missing_filtered_movies=[])
        b.run_exhaustive()
        assert b.exhaustive_tagged == {1} and b.exhaustive_tag_applied == TAG
        assert any(p.endswith("/movie/editor") for _, p, _ in stub.requests)

    def test_strip_orphans_on_the_real_class(self):
        from arrapi import RadarrAPI

        stub = _StubRadarr({1: [1], 2: []})
        api = RadarrAPI("http://radarr:7878", "key", session=_session_with(stub))
        exhaustive_reconcile.bound(api)
        log = MagicMock()
        assert exhaustive_reconcile.strip_orphans(api, set(), {TAG}, log) == {TAG: 1}
        assert any(m == "DELETE" and p.endswith("/tag/1") for m, p, _ in stub.requests)


def _session_with(stub):
    session = requests.Session()
    session.mount("http://", stub)
    return session

"""Tests for the add_missing drip (radarr_add_missing_initial / _per_week / _ledger_tag and the sonarr_ equivalents)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import modules.builder  # noqa: F401
from modules import add_budget
from tests.conftest import FakeLogger

LEDGER = "kl-picks"
NOW = datetime(2026, 10, 3, 12, 0, 0)


def ago(**kw):
    return NOW - timedelta(**kw)


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    monkeypatch.setattr("modules.add_budget.utcnow", lambda: NOW)


def _tag(label):
    return SimpleNamespace(id=1, label=label)


def _movie(tmdb_id, title, *labels, added=None):
    return SimpleNamespace(tmdbId=tmdb_id, title=title, tags=[_tag(label) for label in labels], added=added)


def _series(tvdb_id, title, *labels, added=None):
    return SimpleNamespace(tvdbId=tvdb_id, title=title, tags=[_tag(label) for label in labels], added=added)


def plan(candidates, tagged, initial=50, per_week=10, list_ids=None, unaddable=()):
    """tagged: {id: added}; titles are synthesized."""
    tagged_full = {_id: (f"T{_id}", added) for _id, added in tagged.items()}
    on_list = set(candidates) | set(tagged) if list_ids is None else set(list_ids)
    return add_budget.plan_adds(candidates, initial, per_week, LEDGER, tagged_full, on_list, set(unaddable), NOW)


class TestPlanAdds:
    def test_brand_new_list_gets_initial(self):
        p = plan(list(range(100, 160)), {})
        assert p.first_week and p.limit == 50 and p.spent == 0 and p.remaining == 50
        assert p.ids_to_pass == list(range(100, 150))
        assert p.held_back == list(range(150, 160))

    def test_first_week_cap_counts_all_tagged(self):
        p = plan([100, 101, 102], {1: ago(days=6), 2: ago(days=1), 3: ago(minutes=5)}, initial=4)
        assert p.first_week
        assert p.spent == 3 and p.remaining == 1
        assert p.ids_to_pass == [100] and p.held_back == [101, 102]

    def test_first_week_exhausted(self):
        p = plan([100], {i: ago(days=2) for i in range(1, 51)})
        assert p.first_week and p.remaining == 0
        assert p.ids_to_pass == [] and p.held_back == [100]

    def test_after_first_week_rolling_ten(self):
        tagged = {i: ago(days=30) for i in range(1, 51)}  # the initial batch, long ago
        tagged.update({51: ago(days=6), 52: ago(days=3), 53: ago(hours=1)})  # 3 inside the window
        p = plan(list(range(100, 120)), tagged)
        assert not p.first_week and p.limit == 10
        assert p.spent == 3 and p.remaining == 7
        assert p.ids_to_pass == list(range(100, 107))
        assert p.held_back == list(range(107, 120))

    def test_rolling_window_frees_up_as_items_age_out(self):
        tagged = {1: ago(days=20)} | {i: ago(days=7, minutes=1) for i in range(2, 12)}  # 10 adds just outside the window
        p = plan([100, 101], tagged)
        assert not p.first_week and p.spent == 0 and p.ids_to_pass == [100, 101]

    def test_first_week_ends_exactly_at_seven_days(self):
        p = plan([100], {1: ago(days=7)}, initial=50, per_week=0)
        assert not p.first_week and p.limit == 0 and p.held_back == [100]

    def test_orphans_still_count_towards_rate_and_are_reported(self):
        tagged = {1: ago(days=30), 2: ago(days=1), 3: ago(days=2)}
        p = plan([100, 101, 102], tagged, per_week=3, list_ids={1, 100, 101, 102})
        assert p.orphans == [(2, "T2"), (3, "T3")]
        assert p.spent == 2 and p.ids_to_pass == [100] and p.held_back == [101, 102]

    def test_missing_added_date_is_old(self):
        # An undated tagged item ends the first week and does not count towards the rolling window.
        p = plan([100, 101], {1: None, 2: ago(days=1)}, initial=50, per_week=2)
        assert not p.first_week and p.limit == 2 and p.spent == 1
        assert p.ids_to_pass == [100] and p.held_back == [101]

    def test_only_undated_items_means_rolling_with_nothing_spent(self):
        p = plan([100], {1: None}, initial=0, per_week=1)
        assert not p.first_week and p.spent == 0 and p.ids_to_pass == [100]

    def test_timezone_aware_dates_are_normalized(self):
        aware = (NOW - timedelta(days=1)).replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=-4)))
        p = plan([100], {1: aware, 2: ago(days=30)}, per_week=1)
        assert not p.first_week and p.spent == 1 and p.held_back == [100]

    def test_unaddable_items_pass_through_without_using_allowance(self):
        p = plan([10, 11, 12], {}, initial=1, unaddable={10})
        assert p.ids_to_pass == [10, 11]
        assert p.held_back == [12]

    def test_zero_allowance_still_passes_unaddable(self):
        p = plan([10, 11], {}, initial=0, unaddable={11})
        assert p.ids_to_pass == [11] and p.held_back == [10]

    def test_overspent_never_goes_negative(self):
        p = plan([10], {i: ago(days=1) for i in range(1, 5)}, initial=50, per_week=2)
        assert p.first_week and p.remaining == 46
        p = plan([10], {0: ago(days=30)} | {i: ago(days=1) for i in range(1, 5)}, per_week=2)
        assert p.remaining == 0 and p.held_back == [10]


class TestLedgerTagHelpers:
    def test_default_ledger_tag_is_slugged_and_stable(self):
        assert add_budget.default_ledger_tag("Member Picks!") == "kl-member-picks"
        assert add_budget.default_ledger_tag("Member Picks!") == add_budget.default_ledger_tag("Member Picks!")
        assert add_budget.valid_ledger_tag(add_budget.default_ledger_tag("Ünïcode & Friends (2024)"))

    def test_default_ledger_tag_for_symbol_only_name(self):
        assert add_budget.default_ledger_tag("!!!") == "kl-collection"

    def test_with_ledger_tag_appends_once(self):
        assert add_budget.with_ledger_tag(["a"], LEDGER) == ["a", LEDGER]
        assert add_budget.with_ledger_tag(["a", LEDGER], LEDGER) == ["a", LEDGER]
        assert add_budget.with_ledger_tag(None, LEDGER) == [LEDGER]

    @pytest.mark.parametrize("tag,ok", [("kl-picks", True), ("kl_picks", False), ("Kl picks", False), ("", False)])
    def test_valid_ledger_tag(self, tag, ok):
        assert add_budget.valid_ledger_tag(tag) is ok


def _radarr(monkeypatch, movies):
    monkeypatch.setattr("modules.radarr.logger", FakeLogger())
    from modules.radarr import Radarr

    r = Radarr.__new__(Radarr)
    r.api = MagicMock()
    r.api.all_movies.return_value = movies
    r.cache = MagicMock()
    r.cache.query_radarr_adds.return_value = None
    r.library = SimpleNamespace(original_mapping_name="Movies")
    r.ignore_cache = False
    r.tag = ["library-tag"]
    return r


def _sonarr(monkeypatch, series):
    monkeypatch.setattr("modules.sonarr.logger", FakeLogger())
    from modules.sonarr import Sonarr

    s = Sonarr.__new__(Sonarr)
    s.api = MagicMock()
    s.api.all_series.return_value = series
    s.cache = MagicMock()
    s.cache.query_sonarr_adds.return_value = None
    s.library = SimpleNamespace(original_mapping_name="Shows")
    s.ignore_cache = False
    s.tag = []
    return s


class TestBudgetState:
    def test_radarr_tagged_and_unaddable(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1, "Tagged", LEDGER, "other", added=ago(days=2)), _movie(2, "Plain"), _movie(3, "OtherLedger", "kl-else")])
        r.cache.query_radarr_adds.side_effect = lambda tmdb_id, lib: tmdb_id == 20
        tagged, unaddable = r.budget_state(LEDGER, [1, 2, 10, 20], False)
        assert tagged == {1: ("Tagged", ago(days=2))}
        assert unaddable == {1, 2, 20}

    def test_radarr_item_without_added_attribute(self, monkeypatch):
        movie = SimpleNamespace(tmdbId=1, title="Old", tags=[_tag(LEDGER)])
        tagged, _ = _radarr(monkeypatch, [movie]).budget_state(LEDGER, [], False)
        assert tagged == {1: ("Old", None)}

    def test_radarr_ignore_cache_skips_cache(self, monkeypatch):
        r = _radarr(monkeypatch, [])
        r.cache.query_radarr_adds.return_value = True
        _, unaddable = r.budget_state(LEDGER, [10], True)
        assert unaddable == set()
        r.cache.query_radarr_adds.assert_not_called()

    def test_sonarr_tagged_and_unaddable(self, monkeypatch):
        s = _sonarr(monkeypatch, [_series(1, "Tagged", LEDGER, added=ago(days=9)), _series(2, "Plain")])
        s.cache.query_sonarr_adds.side_effect = lambda tvdb_id, lib: tvdb_id == 20
        tagged, unaddable = s.budget_state(LEDGER, [1, 2, 10, 20], False)
        assert tagged == {1: ("Tagged", ago(days=9))}
        assert unaddable == {1, 2, 20}


def _builder(monkeypatch, arr, plex_tmdb_by_key=None, found_keys=(), filtered_keys=(), missing=(), is_movie=True):
    """CollectionBuilder shell wired just enough for _apply_add_budget."""
    from modules.builder import CollectionBuilder

    monkeypatch.setattr("modules.builder.logger", FakeLogger())
    b = CollectionBuilder.__new__(CollectionBuilder)
    b.name = "Picks"
    b.Type = "Collection"
    key_map = plex_tmdb_by_key or {}
    library = SimpleNamespace(
        movie_rating_key_map=key_map if is_movie else {},
        show_rating_key_map={} if is_movie else key_map,
        add_budget_held_back=MagicMock(),
        add_budget_orphans=MagicMock(),
    )
    b.library = library
    b.libraries = [library]
    b.found_items = [SimpleNamespace(ratingKey=k) for k in found_keys]
    b.filtered_keys = {k: "x" for k in filtered_keys}
    b.missing_movies = list(missing) if is_movie else []
    b.missing_shows = [] if is_movie else list(missing)
    b.held_back_radarr = []
    b.held_back_sonarr = []
    b.budget_blockers = []
    b.do_report = True
    return b, library


def _details(**extra):
    return {"add_missing": True, **extra}


def _drip(initial=50, per_week=10, **extra):
    return _details(add_missing_initial=initial, add_missing_per_week=per_week, add_missing_ledger_tag=LEDGER, **extra)


class TestApplyAddBudget:
    def test_no_ledger_is_unchanged(self, monkeypatch):
        r = _radarr(monkeypatch, [])
        r.budget_state = MagicMock()
        b, lib = _builder(monkeypatch, r, missing=[10, 11])
        details = _details(tag=["x"])
        ids, options = b._apply_add_budget(r, details, [("A", 10), ("B", 11)], True)
        assert ids == [10, 11]
        assert options is details
        assert "kl-" not in str(options)
        r.budget_state.assert_not_called()
        assert b.held_back_radarr == []
        lib.add_budget_held_back.assert_not_called()

    def test_exempt_list_tags_everything_and_limits_nothing(self, monkeypatch):
        r = _radarr(monkeypatch, [])
        r.budget_state = MagicMock()
        b, lib = _builder(monkeypatch, r, missing=list(range(100, 200)))
        b.budget_blockers = ["list may be incomplete"]  # exempt lists have no limit to protect
        missing = [(f"M{i}", i) for i in range(100, 200)]
        ids, options = b._apply_add_budget(r, _details(add_missing_ledger_tag=LEDGER, tag=["mine"]), missing, True)
        assert ids == list(range(100, 200))
        assert options["tag"] == ["mine", LEDGER]
        r.budget_state.assert_not_called()
        assert b.held_back_radarr == []
        lib.add_budget_held_back.assert_not_called()

    def test_first_week_partially_used_adds_remainder_and_reports_held_back(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1, "Already", LEDGER, added=ago(days=1))])
        b, lib = _builder(monkeypatch, r, plex_tmdb_by_key={100: 1}, found_keys=[100], missing=[10, 11, 12])
        ids, _ = b._apply_add_budget(r, _drip(initial=2), [("A", 10), ("B", 11), ("C", 12)], True)
        assert ids == [10]
        assert b.held_back_radarr == [{"title": "B", "id": 11}, {"title": "C", "id": 12}]
        lib.add_budget_held_back.assert_called_once_with("Picks", [("B", 11), ("C", 12)], True)
        lib.add_budget_orphans.assert_not_called()
        assert any("first week, 1/2 added in the last 7 days, 1 remaining" in m for m in modules.builder.logger.info_messages)
        assert any("Held Back" in m for m in modules.builder.logger.warning_messages)
        assert any("2 Items Held Back" in m and "next run will continue" in m for m in modules.builder.logger.info_messages)

    def test_after_first_week_rolling_ten(self, monkeypatch):
        movies = [_movie(i, f"Old {i}", LEDGER, added=ago(days=40)) for i in range(1, 51)]
        movies += [_movie(60 + i, f"Recent {i}", LEDGER, added=ago(days=i)) for i in range(1, 5)]  # 4 in the last 7 days
        r = _radarr(monkeypatch, movies)
        missing = [(f"M{i}", i) for i in range(100, 120)]
        b, lib = _builder(monkeypatch, r, missing=[i for _, i in missing])
        ids, _ = b._apply_add_budget(r, _drip(), missing, True)
        assert ids == list(range(100, 106))
        assert [h["id"] for h in b.held_back_radarr] == list(range(106, 120))
        assert any("rolling 7 days, 4/10 added in the last 7 days, 6 remaining" in m for m in modules.builder.logger.info_messages)

    def test_unaddable_pass_through_without_using_allowance(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(10, "Already In Radarr", added=ago(days=400))])
        r.cache.query_radarr_adds.side_effect = lambda tmdb_id, lib: tmdb_id == 11
        b, _ = _builder(monkeypatch, r, missing=[10, 11, 12, 13])
        ids, _ = b._apply_add_budget(r, _drip(initial=1), [("A", 10), ("B", 11), ("C", 12), ("D", 13)], True)
        assert ids == [10, 11, 12]
        assert b.held_back_radarr == [{"title": "D", "id": 13}]

    def test_orphans_count_towards_rate_reported_and_never_touched(self, monkeypatch):
        movies = [_movie(1, "Still Listed", LEDGER, added=ago(days=30)), _movie(2, "Dropped", LEDGER, added=ago(days=2))]
        r = _radarr(monkeypatch, movies)
        b, lib = _builder(monkeypatch, r, plex_tmdb_by_key={100: 1}, found_keys=[100], missing=[10, 11])
        ids, _ = b._apply_add_budget(r, _drip(per_week=2), [("A", 10), ("B", 11)], True)
        assert ids == [10]  # Dropped (2) was added 2 days ago and still counts, even off the list
        lib.add_budget_orphans.assert_called_once_with("Picks", [("Dropped", 2)], True)
        assert any("Dropped" in m and "Orphan" in m for m in modules.builder.logger.info_messages)
        r.api.delete_multiple_movies.assert_not_called()
        r.api.edit_multiple_movies.assert_not_called()

    def test_missing_added_dates_treated_as_old(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1, "Undated", LEDGER, added=None)])
        b, _ = _builder(monkeypatch, r, plex_tmdb_by_key={100: 1}, found_keys=[100], missing=[10, 11, 12])
        ids, _ = b._apply_add_budget(r, _drip(initial=50, per_week=2), [("A", 10), ("B", 11), ("C", 12)], True)
        assert ids == [10, 11]  # rolling window (undated = old ends the first week), nothing spent this week
        assert any("rolling 7 days, 0/2" in m for m in modules.builder.logger.info_messages)

    def test_uses_injected_clock(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1, "A", LEDGER, added=ago(days=3))])
        b, _ = _builder(monkeypatch, r, plex_tmdb_by_key={100: 1}, found_keys=[100], missing=[10])
        ids, _ = b._apply_add_budget(r, _drip(initial=1, per_week=5), [("B", 10)], True)
        assert ids == []  # day 3: first week, initial of 1 used
        monkeypatch.setattr("modules.add_budget.utcnow", lambda: NOW + timedelta(days=5))
        b.held_back_radarr = []
        ids, _ = b._apply_add_budget(r, _drip(initial=1, per_week=5), [("B", 10)], True)
        assert ids == [10]  # day 8: rolling window, nothing added in the last 7 days

    def test_ledger_tag_applied_to_new_adds_alongside_existing_tags(self, monkeypatch):
        r = _radarr(monkeypatch, [])
        b, _ = _builder(monkeypatch, r, missing=[10])
        _, options = b._apply_add_budget(r, _drip(tag=["mine"]), [("A", 10)], True)
        assert options["tag"] == ["mine", LEDGER]
        assert options["add_missing_initial"] == 50

    def test_ledger_tag_keeps_library_default_tags_when_no_collection_tag(self, monkeypatch):
        r = _radarr(monkeypatch, [])
        b, _ = _builder(monkeypatch, r, missing=[10])
        _, options = b._apply_add_budget(r, _drip(), [("A", 10)], True)
        assert options["tag"] == ["library-tag", LEDGER]

    def test_sonarr_uses_tvdb_ids_and_sonarr_held_back(self, monkeypatch):
        s = _sonarr(monkeypatch, [_series(1, "Listed", LEDGER, added=ago(days=1))])
        b, lib = _builder(monkeypatch, s, plex_tmdb_by_key={100: 1}, found_keys=[100], missing=[10, 11], is_movie=False)
        ids, options = b._apply_add_budget(s, _drip(initial=2), [("A", 10), ("B", 11)], False)
        assert ids == [10]
        assert options["tag"] == [LEDGER]
        assert b.held_back_sonarr == [{"title": "B", "id": 11}]
        assert b.held_back_radarr == []
        lib.add_budget_held_back.assert_called_once_with("Picks", [("B", 11)], False)

    def test_report_skipped_when_not_reporting(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1, "A", LEDGER, added=ago(days=1))])
        b, lib = _builder(monkeypatch, r, plex_tmdb_by_key={100: 1}, found_keys=[100], missing=[10])
        b.do_report = False
        b._apply_add_budget(r, _drip(initial=1), [("A", 10)], True)
        lib.add_budget_held_back.assert_not_called()
        lib.add_budget_orphans.assert_not_called()
        assert b.held_back_radarr == [{"title": "A", "id": 10}]


class TestAttributeParsing:
    def _builder(self):
        from modules.builder import CollectionBuilder

        b = CollectionBuilder.__new__(CollectionBuilder)
        b.Type = "Collection"
        b.radarr_details = {}
        b.sonarr_details = {}
        b.builders = []
        return b

    def test_drip_and_ledger_tag_parse(self):
        b = self._builder()
        b._radarr("radarr_add_missing_initial", "50")
        b._radarr("radarr_add_missing_per_week", 10)
        b._radarr("radarr_add_missing_ledger_tag", "KL-Picks")
        b._sonarr("sonarr_add_missing_initial", 3)
        b._sonarr("sonarr_add_missing_per_week", "1")
        assert b.radarr_details == {"add_missing_initial": 50, "add_missing_per_week": 10, "add_missing_ledger_tag": "kl-picks"}
        assert b.sonarr_details == {"add_missing_initial": 3, "add_missing_per_week": 1}

    @pytest.mark.parametrize("key", ["radarr_add_missing_initial", "radarr_add_missing_per_week"])
    def test_zero_allowed_negative_rejected(self, key):
        from modules.util import Failed

        b = self._builder()
        b._radarr(key, 0)
        assert b.radarr_details[key[7:]] == 0
        with pytest.raises(Failed):
            b._radarr(key, -1)

    @pytest.mark.parametrize("arr", ["radarr", "sonarr"])
    def test_deprecated_budget_key_parses_to_marker(self, arr):
        from modules.util import Failed

        b = self._builder()
        getattr(b, f"_{arr}")(f"{arr}_add_missing_budget", "7")
        assert getattr(b, f"{arr}_details") == {"add_missing_budget": True}
        with pytest.raises(Failed):
            getattr(b, f"_{arr}")(f"{arr}_add_missing_budget", -1)

    def test_invalid_ledger_tag_rejected(self):
        from modules.util import BuilderValidationError

        with pytest.raises(BuilderValidationError):
            self._builder()._radarr("radarr_add_missing_ledger_tag", "bad tag!")


class TestFailClosed:
    """If the list may be incomplete, a dripped collection adds nothing that run."""

    def test_blocker_skips_dripped_add_even_with_allowance_left(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1, "Looks Orphaned", LEDGER), _movie(2, "Also Orphaned", LEDGER)])
        r.budget_state = MagicMock()
        b, lib = _builder(monkeypatch, r, missing=[10, 11])
        b.budget_blockers = []
        b.add_budget_blocker("Builder letterboxd_list failed: boom")
        ids, _ = b._apply_add_budget(r, _drip(initial=2), [("A", 10), ("B", 11)], True)
        assert ids == []
        r.budget_state.assert_not_called()
        assert [h["id"] for h in b.held_back_radarr] == [10, 11]
        lib.add_budget_held_back.assert_called_once_with("Picks", [("A", 10), ("B", 11)], True)
        assert any("may be incomplete" in m for m in modules.builder.logger.warning_messages)
        assert any("boom" in m for m in modules.builder.logger.warning_messages)

    def test_blocker_does_not_affect_undripped_adds(self, monkeypatch):
        r = _radarr(monkeypatch, [])
        b, _ = _builder(monkeypatch, r, missing=[10])
        b.budget_blockers = ["whatever"]
        details = _details()
        ids, options = b._apply_add_budget(r, details, [("A", 10)], True)
        assert ids == [10]
        assert options is details

    def test_sonarr_blocker_skips_and_reports_sonarr(self, monkeypatch):
        s = _sonarr(monkeypatch, [])
        b, lib = _builder(monkeypatch, s, missing=[10], is_movie=False)
        b.budget_blockers = ["x"]
        ids, _ = b._apply_add_budget(s, _drip(initial=5), [("A", 10)], False)
        assert ids == []
        assert b.held_back_sonarr == [{"title": "A", "id": 10}]
        lib.add_budget_held_back.assert_called_once_with("Picks", [("A", 10)], False)

    def test_blocker_recorded_once(self, monkeypatch):
        b, _ = _builder(monkeypatch, _radarr(monkeypatch, []))
        b.budget_blockers = []
        b.add_budget_blocker("same")
        b.add_budget_blocker("same")
        assert b.budget_blockers == ["same"]

    def _two_list_builder(self, monkeypatch, outcomes, ignore_blank_results=True, obj=None):
        r = _radarr(monkeypatch, [_movie(1, "Tagged From List 2", LEDGER)])
        b, lib = _builder(monkeypatch, r, missing=[10])
        b.budget_blockers = []
        b.ignore_blank_results = ignore_blank_results
        b.obj = obj
        b.gather_ids = lambda method, value: outcomes[value]() if callable(outcomes[value]) else outcomes[value]
        b.filter_and_save_items = MagicMock()
        return b, r, lib

    def test_one_of_several_lists_fails_adds_nothing(self, monkeypatch):
        from modules.util import Failed

        def boom():
            raise Failed("Letterboxd Error: list unavailable")

        b, r, lib = self._two_list_builder(monkeypatch, {"one": [(1, "tmdb")], "two": boom})
        b.gather_and_save_items("letterboxd_list", "one")
        b.gather_and_save_items("letterboxd_list", "two")  # swallowed by ignore_blank_results, exactly the hole being closed
        assert len(b.budget_blockers) == 1
        ids, _ = b._apply_add_budget(r, _drip(initial=1), [("A", 10)], True)
        assert ids == []
        assert b.held_back_radarr == [{"title": "A", "id": 10}]

    def test_failure_swallowed_because_collection_exists_also_blocks(self, monkeypatch):
        from modules.util import Failed

        def boom():
            raise Failed("nope")

        b, _, _ = self._two_list_builder(monkeypatch, {"two": boom}, ignore_blank_results=False, obj=object())
        b.gather_and_save_items("letterboxd_list", "two")
        assert b.budget_blockers

    def test_failure_that_raises_still_records_blocker_and_propagates(self, monkeypatch):
        from modules.util import Failed

        def boom():
            raise Failed("nope")

        b, _, _ = self._two_list_builder(monkeypatch, {"two": boom}, ignore_blank_results=False)
        with pytest.raises(Failed):
            b.gather_and_save_items("letterboxd_list", "two")

    def test_list_returning_nothing_blocks(self, monkeypatch):
        b, _, _ = self._two_list_builder(monkeypatch, {"one": [(1, "tmdb")], "two": []})
        b.gather_and_save_items("letterboxd_list", "one")
        assert b.budget_blockers == []
        b.gather_and_save_items("letterboxd_list", "two")
        assert b.budget_blockers == ["Builder letterboxd_list returned no items"]

    def test_healthy_lists_leave_drip_usable(self, monkeypatch):
        b, r, _ = self._two_list_builder(monkeypatch, {"one": [(1, "tmdb")], "two": [(2, "tmdb")]})
        b.gather_and_save_items("letterboxd_list", "one")
        b.gather_and_save_items("letterboxd_list", "two")
        assert b.budget_blockers == []

    def test_unresolvable_id_blocks(self, monkeypatch):
        r = _radarr(monkeypatch, [])
        b, _ = _builder(monkeypatch, r)
        b.budget_blockers = []
        b.libraries = []
        b._find_plex_keys = lambda input_id: []
        b._log_episode_count = MagicMock()
        b.playlist = False
        b.filter_and_save_items([(123, "plex")])
        assert b.budget_blockers == ["ID (123, 'plex') could not be resolved"]


class TestValidateAddBudgets:
    def _builder(self, monkeypatch, radarr_details, sonarr_details=None):
        from modules.builder import CollectionBuilder

        monkeypatch.setattr("modules.builder.logger", FakeLogger())
        b = CollectionBuilder.__new__(CollectionBuilder)
        b.Type = "Collection"
        b.name = "Member Picks"
        b.radarr_details = radarr_details
        b.sonarr_details = sonarr_details or {"add_missing": False}
        return b

    def test_default_ledger_tag_derived_from_name(self, monkeypatch):
        b = self._builder(monkeypatch, {"add_missing": True, "add_missing_initial": 50, "add_missing_per_week": 10})
        b._validate_add_budgets()
        assert b.radarr_details["add_missing_ledger_tag"] == "kl-member-picks"
        assert modules.builder.logger.warning_messages == []

    def test_explicit_ledger_tag_kept(self, monkeypatch):
        b = self._builder(monkeypatch, {"add_missing": True, "add_missing_initial": 5, "add_missing_per_week": 1, "add_missing_ledger_tag": "kl-mine"})
        b._validate_add_budgets()
        assert b.radarr_details["add_missing_ledger_tag"] == "kl-mine"

    def test_ledger_tag_alone_is_valid_exempt_config(self, monkeypatch):
        b = self._builder(monkeypatch, {"add_missing": True, "add_missing_ledger_tag": "kl-x"})
        b._validate_add_budgets()
        assert b.radarr_details == {"add_missing": True, "add_missing_ledger_tag": "kl-x"}
        assert modules.builder.logger.warning_messages == []

    @pytest.mark.parametrize("arr", ["radarr", "sonarr"])
    @pytest.mark.parametrize("given", ["add_missing_initial", "add_missing_per_week"])
    def test_initial_and_per_week_must_come_together(self, monkeypatch, arr, given):
        from modules.util import BuilderValidationError

        details = {"add_missing": True, given: 5}
        b = self._builder(monkeypatch, details if arr == "radarr" else {"add_missing": True}, details if arr == "sonarr" else None)
        with pytest.raises(BuilderValidationError, match=f"{arr}_add_missing_initial and {arr}_add_missing_per_week must be used together"):
            b._validate_add_budgets()

    def test_one_half_plus_ledger_tag_still_rejected(self, monkeypatch):
        from modules.util import BuilderValidationError

        b = self._builder(monkeypatch, {"add_missing": True, "add_missing_initial": 5, "add_missing_ledger_tag": "kl-x"})
        with pytest.raises(BuilderValidationError):
            b._validate_add_budgets()

    @pytest.mark.parametrize("arr", ["radarr", "sonarr"])
    def test_compat_budget_only_maps_to_default_drip(self, monkeypatch, arr):
        details = {"add_missing": True, "add_missing_budget": True}
        b = self._builder(monkeypatch, details if arr == "radarr" else {"add_missing": True}, details if arr == "sonarr" else None)
        b._validate_add_budgets()
        got = getattr(b, f"{arr}_details")
        assert got == {"add_missing": True, "add_missing_initial": 25, "add_missing_per_week": 10, "add_missing_ledger_tag": "kl-member-picks"}
        assert modules.builder.logger.warning_messages == [
            f"Collection Warning: {arr}_add_missing_budget is deprecated and now means the default drip (initial 25, per_week 10); rewrite this collection to {arr}_add_missing_initial/{arr}_add_missing_per_week"
        ]

    @pytest.mark.parametrize("arr", ["radarr", "sonarr"])
    def test_compat_budget_with_ledger_tag_is_drip_not_exempt(self, monkeypatch, arr):
        details = {"add_missing": True, "add_missing_budget": True, "add_missing_ledger_tag": "kl-mine"}
        b = self._builder(monkeypatch, details if arr == "radarr" else {"add_missing": True}, details if arr == "sonarr" else None)
        b._validate_add_budgets()
        got = getattr(b, f"{arr}_details")
        assert got["add_missing_initial"] == 25 and got["add_missing_per_week"] == 10
        assert got["add_missing_ledger_tag"] == "kl-mine"
        assert "add_missing_budget" not in got

    @pytest.mark.parametrize("arr", ["radarr", "sonarr"])
    @pytest.mark.parametrize("other", ["add_missing_initial", "add_missing_per_week"])
    def test_compat_budget_cannot_combine_with_drip(self, monkeypatch, arr, other):
        from modules.util import BuilderValidationError

        details = {"add_missing": True, "add_missing_budget": True, other: 5}
        b = self._builder(monkeypatch, details if arr == "radarr" else {"add_missing": True}, details if arr == "sonarr" else None)
        with pytest.raises(BuilderValidationError, match=f"{arr}_add_missing_budget cannot be combined with"):
            b._validate_add_budgets()

    def test_warns_when_drip_set_but_add_missing_off(self, monkeypatch):
        b = self._builder(monkeypatch, {"add_missing": False, "add_missing_initial": 5, "add_missing_per_week": 1})
        b._validate_add_budgets()
        assert any("radarr_add_missing drip/ledger attributes have no effect" in m for m in modules.builder.logger.warning_messages)

    def test_warns_for_sonarr(self, monkeypatch):
        b = self._builder(monkeypatch, {"add_missing": True}, {"add_missing": False, "add_missing_ledger_tag": "kl-x"})
        b._validate_add_budgets()
        assert any("sonarr_add_missing drip/ledger attributes have no effect" in m for m in modules.builder.logger.warning_messages)

    def test_nothing_set_no_warning_no_ledger(self, monkeypatch):
        b = self._builder(monkeypatch, {"add_missing": False})
        b._validate_add_budgets()
        assert modules.builder.logger.warning_messages == []
        assert "add_missing_ledger_tag" not in b.radarr_details

"""Tests for the lifetime add_missing budget (radarr_add_missing_budget / sonarr_add_missing_budget)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import modules.builder  # noqa: F401
from modules import add_budget
from tests.conftest import FakeLogger

LEDGER = "kl-picks"


def _tag(label):
    return SimpleNamespace(id=1, label=label)


def _movie(tmdb_id, title, *labels):
    return SimpleNamespace(tmdbId=tmdb_id, title=title, tags=[_tag(label) for label in labels])


def _series(tvdb_id, title, *labels):
    return SimpleNamespace(tvdbId=tvdb_id, title=title, tags=[_tag(label) for label in labels])


class TestPlanAdds:
    def test_budget_partially_used_adds_only_remainder_in_list_order(self):
        plan = add_budget.plan_adds([10, 11, 12, 13], 3, LEDGER, {1: "A"}, {1, 10, 11, 12, 13}, set())
        assert plan.spent == 1
        assert plan.remaining == 2
        assert plan.ids_to_pass == [10, 11]
        assert plan.held_back == [12, 13]

    def test_budget_exhausted_adds_none(self):
        plan = add_budget.plan_adds([10, 11], 2, LEDGER, {1: "A", 2: "B"}, {1, 2, 10, 11}, set())
        assert plan.remaining == 0
        assert plan.ids_to_pass == []
        assert plan.held_back == [10, 11]

    def test_overspent_budget_never_goes_negative(self):
        plan = add_budget.plan_adds([10], 1, LEDGER, {1: "A", 2: "B", 3: "C"}, {1, 2, 3, 10}, set())
        assert plan.remaining == 0
        assert plan.held_back == [10]

    def test_orphans_do_not_count_and_are_reported(self):
        tagged = {1: "On List", 2: "Orphan A", 3: "Orphan B"}
        plan = add_budget.plan_adds([10, 11], 2, LEDGER, tagged, {1, 10, 11}, set())
        assert plan.spent == 1
        assert plan.orphans == [(2, "Orphan A"), (3, "Orphan B")]
        assert plan.ids_to_pass == [10]
        assert plan.held_back == [11]

    def test_unaddable_items_pass_through_without_using_budget(self):
        plan = add_budget.plan_adds([10, 11, 12], 1, LEDGER, {}, {10, 11, 12}, {10})
        assert plan.ids_to_pass == [10, 11]
        assert plan.held_back == [12]

    def test_zero_budget_holds_everything_addable(self):
        plan = add_budget.plan_adds([10, 11], 0, LEDGER, {}, {10, 11}, set())
        assert plan.ids_to_pass == []
        assert plan.held_back == [10, 11]


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
        r = _radarr(monkeypatch, [_movie(1, "Tagged", LEDGER, "other"), _movie(2, "Plain"), _movie(3, "OtherLedger", "kl-else")])
        r.cache.query_radarr_adds.side_effect = lambda tmdb_id, lib: tmdb_id == 20
        tagged, unaddable = r.budget_state(LEDGER, [1, 2, 10, 20], False)
        assert tagged == {1: "Tagged"}
        assert unaddable == {1, 2, 20}

    def test_radarr_ignore_cache_skips_cache(self, monkeypatch):
        r = _radarr(monkeypatch, [])
        r.cache.query_radarr_adds.return_value = True
        _, unaddable = r.budget_state(LEDGER, [10], True)
        assert unaddable == set()
        r.cache.query_radarr_adds.assert_not_called()

    def test_sonarr_tagged_and_unaddable(self, monkeypatch):
        s = _sonarr(monkeypatch, [_series(1, "Tagged", LEDGER), _series(2, "Plain")])
        s.cache.query_sonarr_adds.side_effect = lambda tvdb_id, lib: tvdb_id == 20
        tagged, unaddable = s.budget_state(LEDGER, [1, 2, 10, 20], False)
        assert tagged == {1: "Tagged"}
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


class TestApplyAddBudget:
    def test_no_budget_is_unchanged(self, monkeypatch):
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

    def test_partially_used_budget_adds_remainder_and_reports_held_back(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1, "Already", LEDGER)])
        b, lib = _builder(monkeypatch, r, plex_tmdb_by_key={100: 1}, found_keys=[100], missing=[10, 11, 12])
        details = _details(add_missing_budget=2, add_missing_ledger_tag=LEDGER)
        ids, _ = b._apply_add_budget(r, details, [("A", 10), ("B", 11), ("C", 12)], True)
        assert ids == [10]
        assert b.held_back_radarr == [{"title": "B", "id": 11}, {"title": "C", "id": 12}]
        lib.add_budget_held_back.assert_called_once_with("Picks", [("B", 11), ("C", 12)], True)
        lib.add_budget_orphans.assert_not_called()
        assert any("1/2 used, 1 remaining" in m for m in modules.builder.logger.info_messages)
        assert any("Held Back" in m for m in modules.builder.logger.warning_messages)

    def test_exhausted_budget_adds_none(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1, "A", LEDGER), _movie(2, "B", LEDGER)])
        b, _ = _builder(monkeypatch, r, plex_tmdb_by_key={100: 1, 101: 2}, found_keys=[100, 101], missing=[10, 11])
        ids, _ = b._apply_add_budget(r, _details(add_missing_budget=2, add_missing_ledger_tag=LEDGER), [("A", 10), ("B", 11)], True)
        assert ids == []
        assert [h["id"] for h in b.held_back_radarr] == [10, 11]

    def test_orphans_are_excluded_from_count_reported_and_never_touched(self, monkeypatch):
        movies = [_movie(1, "Still Listed", LEDGER), _movie(2, "Dropped", LEDGER)]
        r = _radarr(monkeypatch, movies)
        b, lib = _builder(monkeypatch, r, plex_tmdb_by_key={100: 1}, found_keys=[100], missing=[10, 11])
        ids, _ = b._apply_add_budget(r, _details(add_missing_budget=2, add_missing_ledger_tag=LEDGER), [("A", 10), ("B", 11)], True)
        assert ids == [10]  # Dropped (2) is not on the list, so only item 1 counts
        lib.add_budget_orphans.assert_called_once_with("Picks", [("Dropped", 2)], True)
        assert any("Dropped" in m and "Orphan" in m for m in modules.builder.logger.info_messages)
        r.api.delete_multiple_movies.assert_not_called()
        r.api.edit_multiple_movies.assert_not_called()

    def test_tagged_item_still_missing_or_filtered_counts_as_on_list(self, monkeypatch):
        movies = [_movie(1, "Missing And Tagged", LEDGER), _movie(2, "Filtered In Plex", LEDGER)]
        r = _radarr(monkeypatch, movies)
        b, lib = _builder(monkeypatch, r, plex_tmdb_by_key={100: 2}, filtered_keys=[100], missing=[1, 10])
        ids, _ = b._apply_add_budget(r, _details(add_missing_budget=3, add_missing_ledger_tag=LEDGER), [("M", 1), ("N", 10)], True)
        assert ids == [1, 10]  # 1 already in Radarr passes through; budget 3 - 2 spent = 1 slot for item 10
        lib.add_budget_orphans.assert_not_called()

    def test_ledger_tag_applied_to_new_adds_alongside_existing_tags(self, monkeypatch):
        r = _radarr(monkeypatch, [])
        b, _ = _builder(monkeypatch, r, missing=[10])
        _, options = b._apply_add_budget(r, _details(add_missing_budget=5, add_missing_ledger_tag=LEDGER, tag=["mine"]), [("A", 10)], True)
        assert options["tag"] == ["mine", LEDGER]
        assert options["add_missing_budget"] == 5

    def test_ledger_tag_keeps_library_default_tags_when_no_collection_tag(self, monkeypatch):
        r = _radarr(monkeypatch, [])
        b, _ = _builder(monkeypatch, r, missing=[10])
        _, options = b._apply_add_budget(r, _details(add_missing_budget=5, add_missing_ledger_tag=LEDGER), [("A", 10)], True)
        assert options["tag"] == ["library-tag", LEDGER]

    def test_sonarr_uses_tvdb_ids_and_sonarr_held_back(self, monkeypatch):
        s = _sonarr(monkeypatch, [_series(1, "Listed", LEDGER)])
        b, lib = _builder(monkeypatch, s, plex_tmdb_by_key={100: 1}, found_keys=[100], missing=[10, 11], is_movie=False)
        ids, options = b._apply_add_budget(s, _details(add_missing_budget=2, add_missing_ledger_tag=LEDGER), [("A", 10), ("B", 11)], False)
        assert ids == [10]
        assert options["tag"] == [LEDGER]
        assert b.held_back_sonarr == [{"title": "B", "id": 11}]
        assert b.held_back_radarr == []
        lib.add_budget_held_back.assert_called_once_with("Picks", [("B", 11)], False)

    def test_report_skipped_when_not_reporting(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1, "A", LEDGER)])
        b, lib = _builder(monkeypatch, r, plex_tmdb_by_key={100: 1}, found_keys=[100], missing=[10])
        b.do_report = False
        b._apply_add_budget(r, _details(add_missing_budget=1, add_missing_ledger_tag=LEDGER), [("A", 10)], True)
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

    def test_budget_and_ledger_tag_parse(self):
        b = self._builder()
        b._radarr("radarr_add_missing_budget", "25")
        b._radarr("radarr_add_missing_ledger_tag", "KL-Picks")
        b._sonarr("sonarr_add_missing_budget", 3)
        assert b.radarr_details == {"add_missing_budget": 25, "add_missing_ledger_tag": "kl-picks"}
        assert b.sonarr_details == {"add_missing_budget": 3}

    def test_zero_budget_allowed_negative_rejected(self):
        from modules.util import Failed

        b = self._builder()
        b._radarr("radarr_add_missing_budget", 0)
        assert b.radarr_details["add_missing_budget"] == 0
        with pytest.raises(Failed):
            b._radarr("radarr_add_missing_budget", -1)

    def test_invalid_ledger_tag_rejected(self):
        from modules.util import BuilderValidationError

        with pytest.raises(BuilderValidationError):
            self._builder()._radarr("radarr_add_missing_ledger_tag", "bad tag!")


class TestFailClosed:
    """If the list may be incomplete, a budgeted collection adds nothing: a fetch failure must never free up budget."""

    def test_blocker_skips_budgeted_add_even_when_orphans_would_free_budget(self, monkeypatch):
        r = _radarr(monkeypatch, [_movie(1, "Looks Orphaned", LEDGER), _movie(2, "Also Orphaned", LEDGER)])
        r.budget_state = MagicMock()
        b, lib = _builder(monkeypatch, r, missing=[10, 11])
        b.budget_blockers = []
        b.add_budget_blocker("Builder letterboxd_list failed: boom")
        ids, _ = b._apply_add_budget(r, _details(add_missing_budget=2, add_missing_ledger_tag=LEDGER), [("A", 10), ("B", 11)], True)
        assert ids == []
        r.budget_state.assert_not_called()
        assert [h["id"] for h in b.held_back_radarr] == [10, 11]
        lib.add_budget_held_back.assert_called_once_with("Picks", [("A", 10), ("B", 11)], True)
        assert any("may be incomplete" in m for m in modules.builder.logger.warning_messages)
        assert any("boom" in m for m in modules.builder.logger.warning_messages)

    def test_blocker_does_not_affect_unbudgeted_adds(self, monkeypatch):
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
        ids, _ = b._apply_add_budget(s, _details(add_missing_budget=5, add_missing_ledger_tag=LEDGER), [("A", 10)], False)
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
        ids, _ = b._apply_add_budget(r, _details(add_missing_budget=1, add_missing_ledger_tag=LEDGER), [("A", 10)], True)
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

    def test_healthy_lists_leave_budget_usable(self, monkeypatch):
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
        b = self._builder(monkeypatch, {"add_missing": True, "add_missing_budget": 5})
        b._validate_add_budgets()
        assert b.radarr_details["add_missing_ledger_tag"] == "kl-member-picks"
        assert modules.builder.logger.warning_messages == []

    def test_explicit_ledger_tag_kept(self, monkeypatch):
        b = self._builder(monkeypatch, {"add_missing": True, "add_missing_budget": 5, "add_missing_ledger_tag": "kl-mine"})
        b._validate_add_budgets()
        assert b.radarr_details["add_missing_ledger_tag"] == "kl-mine"

    def test_warns_when_budget_set_but_add_missing_off(self, monkeypatch):
        b = self._builder(monkeypatch, {"add_missing": False, "add_missing_budget": 5})
        b._validate_add_budgets()
        assert any("radarr_add_missing_budget has no effect" in m for m in modules.builder.logger.warning_messages)

    def test_warns_for_sonarr(self, monkeypatch):
        b = self._builder(monkeypatch, {"add_missing": True}, {"add_missing": False, "add_missing_budget": 1})
        b._validate_add_budgets()
        assert any("sonarr_add_missing_budget has no effect" in m for m in modules.builder.logger.warning_messages)

    def test_no_budget_no_warning_no_ledger(self, monkeypatch):
        b = self._builder(monkeypatch, {"add_missing": False})
        b._validate_add_budgets()
        assert modules.builder.logger.warning_messages == []
        assert "add_missing_ledger_tag" not in b.radarr_details

    def test_ledger_tag_without_budget_rejected(self, monkeypatch):
        from modules.util import BuilderValidationError

        b = self._builder(monkeypatch, {"add_missing": True, "add_missing_ledger_tag": "kl-x"})
        with pytest.raises(BuilderValidationError):
            b._validate_add_budgets()

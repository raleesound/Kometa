"""Orphaned exhaustive-* tags (#5634 S9c): which tags the config owns, and stripping the rest without ever guessing."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from modules import add_budget, exhaustive_reconcile, live_record
from tests.conftest import FakeLogger


def _config(tmp_path, collections, templates="", extra_library="", radarr="radarr:\n  url: http://r:7878\n  token: secret\n"):
    movies = tmp_path / "movies.yml"
    movies.write_text(templates + "collections:\n" + collections)
    config = tmp_path / "config.yml"
    config.write_text(radarr + "libraries:\n  Movies:\n    collection_files:\n      - file: " + str(movies) + "\n" + extra_library)
    return str(config), movies


class TestOwnedTags:
    def test_collections_with_the_key_own_their_tag(self, tmp_path):
        config, _ = _config(tmp_path, "  Picks:\n    acquire: exhaustive\n  Plain:\n    tmdb_movie: 1\n")
        assert exhaustive_reconcile.owned_tags(config) == ({"exhaustive-picks"}, None)

    def test_the_key_can_come_from_a_template_defined_in_the_file(self, tmp_path):
        config, _ = _config(tmp_path, "  Picks:\n    template: {name: t}\n", templates="templates:\n  t:\n    acquire: exhaustive\n")
        assert exhaustive_reconcile.owned_tags(config)[0] == {"exhaustive-picks"}

    @pytest.mark.parametrize(
        "collections,templates",
        [
            ("  Picks:\n    template: {name: elsewhere}\n", ""),
            ("  Picks:\n    acquire: <<how>>\n", ""),
            ("  Picks:\n    template: {name: t}\n", "templates:\n  t:\n    acquire: <<how>>\n"),
            ("  <<name>>:\n    acquire: exhaustive\n", ""),
            ("  - not a mapping\n", ""),
        ],
    )
    def test_anything_it_cannot_read_literally_answers_none(self, tmp_path, collections, templates):
        config, _ = _config(tmp_path, collections, templates=templates)
        owned, reason = exhaustive_reconcile.owned_tags(config)
        assert owned is None and reason

    def test_a_literal_name_override_is_the_name_that_owns_the_tag(self, tmp_path):
        config, _ = _config(tmp_path, "  Picks:\n    name: Rob's Best Picks\n    acquire: exhaustive\n")
        assert exhaustive_reconcile.owned_tags(config) == ({add_budget.exhaustive_tag("Rob's Best Picks")}, None)

    @pytest.mark.parametrize(
        "collections,templates",
        [
            ("  Picks:\n    name: <<x>>\n    acquire: exhaustive\n", ""),
            ("  Picks:\n    name_mapping: Other\n    acquire: exhaustive\n", ""),
            ("  Picks:\n    translations: {fr: Choix}\n    acquire: exhaustive\n", ""),
            ("  Picks:\n    template: {name: t}\n", "templates:\n  t:\n    acquire: exhaustive\n    name: Other\n"),
        ],
    )
    def test_a_name_it_cannot_resolve_answers_none(self, tmp_path, collections, templates):
        config, _ = _config(tmp_path, collections, templates=templates)
        assert exhaustive_reconcile.owned_tags(config)[0] is None

    def test_keys_and_the_value_match_the_way_the_builder_reads_them(self, tmp_path):
        config, _ = _config(tmp_path, "  Picks:\n    Acquire: ' EXHAUSTIVE '\n    Name: Real\n")
        assert exhaustive_reconcile.owned_tags(config) == ({"exhaustive-real"}, None)

    @pytest.mark.parametrize(
        "collections,templates",
        [
            ("  Picks:\n    collection_name: Real\n    acquire: exhaustive\n", ""),
            ("  Picks:\n    translation_key: imdb_top_250\n    acquire: exhaustive\n", ""),
            ("  Picks:\n    template: {name: t, k: acquire}\n", "templates:\n  t:\n    <<k>>: exhaustive\n"),
        ],
    )
    def test_more_ways_the_name_or_key_can_move_answer_none(self, tmp_path, collections, templates):
        config, _ = _config(tmp_path, collections, templates=templates)
        assert exhaustive_reconcile.owned_tags(config)[0] is None

    def test_deprecated_metadata_path_answers_none(self, tmp_path):
        config, movies = _config(tmp_path, "  Picks:\n    acquire: exhaustive\n")
        text = open(config).read().replace("collection_files:", "metadata_path:")
        open(config, "w").write(text)
        assert exhaustive_reconcile.owned_tags(config)[0] is None

    def test_names_read_the_way_kometa_reads_them(self, tmp_path):
        config, _ = _config(tmp_path, "  Yes:\n    acquire: exhaustive\n  Picks:\n    name: 12:30\n    acquire: exhaustive\n")
        assert exhaustive_reconcile.owned_tags(config) == ({"exhaustive-yes", "exhaustive-12-30"}, None)

    @pytest.mark.parametrize("collections", ["  Picks:\n    name: 010\n    acquire: exhaustive\n"])
    def test_a_numeric_name_answers_none(self, tmp_path, collections):
        config, _ = _config(tmp_path, collections)
        assert exhaustive_reconcile.owned_tags(config)[0] is None

    def test_a_folder_with_brackets_is_read_and_an_empty_or_missing_one_answers_none(self, tmp_path):
        folder = tmp_path / "charts [new]"
        folder.mkdir()
        config = tmp_path / "config.yml"
        config.write_text(f"libraries:\n  Movies:\n    collection_files:\n      - folder: {folder}\n")
        assert exhaustive_reconcile.owned_tags(str(config))[0] is None
        (folder / "x.yml").write_text("collections:\n  Picks:\n    acquire: exhaustive\n")
        assert exhaustive_reconcile.owned_tags(str(config)) == ({"exhaustive-picks"}, None)

    @pytest.mark.parametrize("collections", ["  1e3:\n    acquire: exhaustive\n", "  Picks:\n    name: 0o17\n    acquire: exhaustive\n"])
    def test_names_ruamel_reads_as_numbers_answer_none(self, tmp_path, collections):
        config, _ = _config(tmp_path, collections)
        assert exhaustive_reconcile.owned_tags(config)[0] is None

    def test_a_config_with_no_libraries_or_no_files_answers_none(self, tmp_path):
        bare = tmp_path / "bare.yml"
        bare.write_text("radarr:\n  url: http://r\n  token: x\n")
        assert exhaustive_reconcile.owned_tags(str(bare))[0] is None
        bare.write_text("libraries:\n  Movies:\n    plex: {}\n")
        assert exhaustive_reconcile.owned_tags(str(bare))[0] is None

    def test_dynamic_collections_answer_none(self, tmp_path):
        config, movies = _config(tmp_path, "  Picks:\n    tmdb_movie: 1\n")
        movies.write_text("dynamic_collections:\n  X:\n    type: genre\n    template: {name: t}\ntemplates:\n  t:\n    acquire: exhaustive\n")
        assert exhaustive_reconcile.owned_tags(config)[0] is None

    SEASONAL = (
        "external_templates:\n  default: templates\ntemplates:\n  holiday:\n    default:\n      sync_mode: sync\n"
        "dynamic_collections:\n  Seasonal:\n    type: custom\n    data: {easter: Easter}\n    template: [holiday, shared, arr]\n    template_variables:\n      emoji: {easter: x}\n"
    )

    def test_a_dynamic_block_that_cannot_carry_the_key_does_not_stop_the_scan(self, tmp_path):
        config, movies = _config(tmp_path, "  Picks:\n    acquire: exhaustive\n")
        movies.write_text(self.SEASONAL + "collections:\n  Picks:\n    acquire: exhaustive\n")
        assert exhaustive_reconcile.owned_tags(config) == ({"exhaustive-picks"}, None)

    @pytest.mark.parametrize(
        "block",
        [
            "    type: custom\n    template_variables: {acquire: exhaustive}\n",
            "    type: custom\n    template_variables: {x: acquire}\n",
            "    type: custom\n    template: [holiday, no_such_template]\n",
            "    type: custom\n    template: {name: <<which>>}\n",
            "    type: custom\n    template: holiday\n    other_template: no_such_template\n",
            "    type: custom\n    <<key>>: exhaustive\n",
        ],
    )
    def test_a_dynamic_block_that_might_carry_the_key_still_stops_the_scan(self, tmp_path, block):
        config, movies = _config(tmp_path, "  Picks:\n    acquire: exhaustive\n")
        movies.write_text("templates:\n  holiday:\n    default: {sync_mode: sync}\ndynamic_collections:\n  Seasonal:\n" + block + "collections:\n  Picks:\n    acquire: exhaustive\n")
        owned, reason = exhaustive_reconcile.owned_tags(config)
        assert owned is None and "dynamic_collections" in reason

    def test_a_local_template_with_the_key_stops_the_scan(self, tmp_path):
        config, movies = _config(tmp_path, "  Picks:\n    acquire: exhaustive\n")
        movies.write_text("templates:\n  t:\n    default: {acquire: exhaustive}\ndynamic_collections:\n  D:\n    type: custom\n    template: t\n")
        assert exhaustive_reconcile.owned_tags(config)[0] is None
        # a local template shadowing a built-in name and setting the key
        movies.write_text("templates:\n  shared:\n    acquire: exhaustive\ndynamic_collections:\n  D:\n    type: custom\n    template: shared\n")
        assert exhaustive_reconcile.owned_tags(config)[0] is None
        # other_template as a list naming a defined template that sets the key, and an upper-case key
        movies.write_text("templates:\n  t:\n    Acquire: exhaustive\ndynamic_collections:\n  D:\n    type: custom\n    other_template: [t]\n")
        assert exhaustive_reconcile.owned_tags(config)[0] is None
        movies.write_text("dynamic_collections:\n  D:\n    type: custom\n    template_variables: {Acquire: exhaustive}\n")
        assert exhaustive_reconcile.owned_tags(config)[0] is None

    @pytest.mark.parametrize("body", ["dynamic_collections: [x]\n", "dynamic_collections:\n  D: nope\n"])
    def test_a_dynamic_collections_value_that_is_not_a_mapping_stops_the_scan(self, tmp_path, body):
        config, movies = _config(tmp_path, "  Picks:\n    acquire: exhaustive\n")
        movies.write_text(body)
        assert exhaustive_reconcile.owned_tags(config)[0] is None

    @pytest.mark.parametrize("external", ["  file: /somewhere/else.yml\n", "  url: https://example.com/t.yml\n", "  git: x\n", "  repo: x\n", "  default: templates\n  file: /x.yml\n", "  default: other\n"])
    def test_built_in_names_are_not_trusted_when_external_templates_can_supply_them(self, tmp_path, external):
        config, movies = _config(tmp_path, "  Picks:\n    acquire: exhaustive\n")
        head = "external_templates:\n" + external
        movies.write_text(head + "dynamic_collections:\n  D:\n    type: custom\n    template: shared\n")
        assert exhaustive_reconcile.owned_tags(config)[0] is None
        movies.write_text(head + "collections:\n  Picks:\n    template: shared\n    acquire: exhaustive\n")
        assert exhaustive_reconcile.owned_tags(config)[0] is None

    def test_external_templates_that_are_unparseable_stop_the_scan(self, tmp_path):
        config, movies = _config(tmp_path, "  Picks:\n    acquire: exhaustive\n")
        movies.write_text("external_templates: [a, b]\ndynamic_collections:\n  D:\n    type: custom\n    template: shared\n")
        assert exhaustive_reconcile.owned_tags(config)[0] is None

    def test_a_template_defined_only_in_config_yml_is_not_defined(self, tmp_path):
        config, movies = _config(tmp_path, "  Picks:\n    acquire: exhaustive\n")
        movies.write_text("dynamic_collections:\n  D:\n    type: custom\n    template: from_config\ncollections:\n  Picks:\n    acquire: exhaustive\n")
        open(config, "w").write("templates:\n  from_config:\n    sync_mode: sync\n" + open(config).read())
        assert exhaustive_reconcile.owned_tags(config)[0] is None

    def test_a_failed_built_in_read_is_not_remembered(self, tmp_path, monkeypatch):
        monkeypatch.setattr(exhaustive_reconcile, "_BUILTINS", [])
        real = exhaustive_reconcile._read_yaml
        monkeypatch.setattr(exhaustive_reconcile, "_read_yaml", lambda p: (_ for _ in ()).throw(OSError("boom")))
        assert exhaustive_reconcile._builtin_templates() == {}
        monkeypatch.setattr(exhaustive_reconcile, "_read_yaml", real)
        assert "shared" in exhaustive_reconcile._builtin_templates()

    def test_a_template_full_of_suffix_variable_keys_is_fine_but_a_key_that_could_spell_the_attribute_is_not(self, tmp_path):
        assert exhaustive_reconcile._mentions_acquire({"default": {"sync_mode_<<key>>": "x", "radarr_tag_<<key>>": 1}}) is False
        for key in ("<<k>>", "ac<<x>>", "<<a>>quire"):
            assert exhaustive_reconcile._mentions_acquire({"default": {key: 1}}) is True

    def test_a_collection_may_name_a_built_in_template_but_one_that_can_rename_it_cannot_own_a_tag(self, tmp_path):
        config, movies = _config(tmp_path, "  Picks:\n    template: {name: arr}\n    acquire: exhaustive\n  Sep:\n    template: {name: separator}\n")
        assert exhaustive_reconcile.owned_tags(config) == ({"exhaustive-picks"}, None)
        movies.write_text("collections:\n  Picks:\n    template: {name: shared}\n    acquire: exhaustive\n")
        assert exhaustive_reconcile.owned_tags(config)[0] is None
        movies.write_text("collections:\n  Picks:\n    template: {name: not_a_builtin}\n")
        assert exhaustive_reconcile.owned_tags(config)[0] is None

    def test_the_built_in_templates_the_scan_trusts_are_real_and_never_mention_the_key(self):
        builtin = exhaustive_reconcile._builtin_templates()
        assert {"shared", "arr"} <= set(builtin)
        assert not any(exhaustive_reconcile._mentions_acquire(v, variable_keys=False) for v in builtin.values())

    def test_a_missing_unparsable_or_remote_file_answers_none(self, tmp_path):
        config, movies = _config(tmp_path, "  Picks:\n    acquire: exhaustive\n")
        movies.write_text("collections: [unclosed\n")
        assert exhaustive_reconcile.owned_tags(config)[0] is None
        movies.unlink()
        assert exhaustive_reconcile.owned_tags(config)[0] is None
        remote = tmp_path / "remote.yml"
        remote.write_text("libraries:\n  Movies:\n    collection_files:\n      - url: https://example.com/x.yml\n")
        assert exhaustive_reconcile.owned_tags(str(remote))[0] is None
        assert exhaustive_reconcile.owned_tags(str(tmp_path / "gone.yml"))[0] is None

    def test_a_folder_is_read_whole_and_metadata_files_count_too(self, tmp_path):
        folder = tmp_path / "charts"
        folder.mkdir()
        (folder / "a.yml").write_text("collections:\n  Alpha:\n    acquire: exhaustive\n")
        meta = tmp_path / "meta.yml"
        meta.write_text("collections:\n  Beta:\n    acquire: exhaustive\n")
        config = tmp_path / "config.yml"
        config.write_text(f"libraries:\n  Movies:\n    collection_files:\n      - folder: {folder}\n    metadata_files:\n      - file: {meta}\n")
        assert exhaustive_reconcile.owned_tags(str(config))[0] == {"exhaustive-alpha", "exhaustive-beta"}

    def test_clashes_name_the_other_collection(self, tmp_path):
        config, _ = _config(tmp_path, "  Top 10:\n    acquire: exhaustive\n  Top-10:\n    acquire: exhaustive\n  Other:\n    acquire: exhaustive\n")
        exhaustive_reconcile.forget_run()
        assert exhaustive_reconcile.clashes(config, "Movies", "Top 10") == [("Movies", "Top-10")]
        assert exhaustive_reconcile.clashes(config, "Movies", "Other") == []
        exhaustive_reconcile.forget_run()


def _api(tags, movies):
    api = MagicMock()
    api.all_tags.side_effect = lambda: [SimpleNamespace(id=i, label=label) for label, i in dict(tags).items()]
    api.all_movies.return_value = [SimpleNamespace(tmdbId=t, tagsIds=[tags[label] for label in labels]) for t, labels in movies.items()]
    return api


def _record(tmp_path, labels):
    """A live record in which Kometa has recorded applying these exhaustive tags."""
    conn = live_record.connect(live_record.db_path(str(tmp_path)))
    for label in labels:
        conn.execute("INSERT OR REPLACE INTO exhaustive_tags (label, library, name, applied_at) VALUES (?, 'Movies', ?, '2026-11-14T00:00:00Z')", (label, label))
    conn.commit()
    conn.close()


def _recorded_labels(tmp_path):
    conn = live_record.connect(live_record.db_path(str(tmp_path)))
    try:
        return live_record.recorded_exhaustive_tags(conn)
    finally:
        conn.close()


TAGS = {"exhaustive-picks": 1, "exhaustive-gone": 2, "exhaustive": 3, "exhaustive_x": 4, "exhaustive-": 5, "kl-gone": 6, "Exhaustive-Loud": 7, "exhaustive-a": 8, "exhaustive-b": 9, "exhaustive-keepers": 10}
LOG = logging.getLogger("t")


class TestStripOrphans:
    def test_only_recorded_unowned_slug_tags_are_stripped_and_a_hand_made_one_is_left_with_a_warning(self, caplog):
        api = _api(TAGS, {10: ["exhaustive-gone"], 11: ["exhaustive-gone", "exhaustive-picks"], 12: ["exhaustive"], 13: ["kl-gone", "exhaustive_x"], 14: ["exhaustive-picks"], 15: ["exhaustive-keepers"]})
        recorded = {"exhaustive-picks", "exhaustive-gone", "exhaustive-loud", "exhaustive-a", "exhaustive-b"}
        with caplog.at_level(logging.WARNING):
            result = exhaustive_reconcile.strip_orphans(api, {"exhaustive-picks", "exhaustive-a", "exhaustive-b"}, recorded, LOG)
        assert result == {"exhaustive-gone": 2, "exhaustive-loud": 0}
        calls = [(c.args[0], c.kwargs["tags"], c.kwargs["apply_tags"]) for c in api.edit_multiple_movies.call_args_list]
        assert calls == [([10, 11], ["exhaustive-gone"], "remove")]
        assert [c.args[0] for c in api.delete_tag.call_args_list] == [2, 7]
        assert "exhaustive-keepers alone" in caplog.text

    def test_a_hand_made_tag_with_films_is_untouched_even_when_it_is_the_only_lookalike(self):
        api = _api({"exhaustive-keepers": 1}, {10: ["exhaustive-keepers"]})
        assert exhaustive_reconcile.strip_orphans(api, set(), set(), LOG) == {}
        api.all_movies.assert_not_called()
        api.edit_multiple_movies.assert_not_called()
        api.delete_tag.assert_not_called()

    def test_nothing_orphaned_touches_nothing_and_reads_no_movies(self):
        api = _api(TAGS, {10: ["exhaustive-picks"]})
        assert exhaustive_reconcile.strip_orphans(api, {"exhaustive-picks"}, {"exhaustive-picks"}, LOG) == {}
        api.all_movies.assert_not_called()
        api.delete_tag.assert_not_called()

    def test_tags_are_listed_once_per_pass(self):
        api = _api(TAGS, {})
        exhaustive_reconcile.strip_orphans(api, set(), {"exhaustive-gone", "exhaustive-a", "exhaustive-b", "exhaustive-picks"}, LOG)
        assert api.all_tags.call_count == 1

    def test_at_most_two_recorded_orphans_go_per_pass_and_the_rest_wait(self, caplog):
        tags = {f"exhaustive-{n}": i for i, n in enumerate("abcdefg", 1)}
        api = _api(tags, {i: [label] for label, i in tags.items()})
        recorded = set(tags)
        owned = {"exhaustive-g"}
        with caplog.at_level(logging.WARNING):
            first = exhaustive_reconcile.strip_orphans(api, owned, recorded, LOG)
        assert set(first) == {"exhaustive-a", "exhaustive-b"}
        assert "4 more" in caplog.text and "later pass" in caplog.text
        for label in first:
            tags.pop(label)
            recorded.discard(label)
        assert set(exhaustive_reconcile.strip_orphans(api, owned, recorded, LOG)) == {"exhaustive-c", "exhaustive-d"}

    def test_the_cap_counts_only_recorded_tags_and_a_lone_tag_can_go(self):
        tags = {"exhaustive-a": 1, "exhaustive-b": 2, "exhaustive-c": 3, "exhaustive-stranger": 4, "exhaustive-stranger2": 5}
        # three recorded, one owned: half of three is one
        assert set(exhaustive_reconcile.strip_orphans(_api(tags, {}), {"exhaustive-a"}, {"exhaustive-a", "exhaustive-b", "exhaustive-c"}, LOG)) == {"exhaustive-b"}
        assert set(exhaustive_reconcile.strip_orphans(_api({"exhaustive-only": 1}, {}), set(), {"exhaustive-only"}, LOG)) == {"exhaustive-only"}

    def test_a_delete_that_fails_to_read_radarrs_reply_is_fine_once_the_tag_is_gone(self):
        state = {"tags": {"exhaustive-gone": 2, "exhaustive-picks": 1}}
        api = _api(state["tags"], {10: ["exhaustive-gone"]})
        api.all_tags.side_effect = lambda: [SimpleNamespace(id=i, label=label) for label, i in state["tags"].items()]

        def delete(tag_id):
            state["tags"] = {k: v for k, v in state["tags"].items() if v != tag_id}
            raise ValueError("no JSON")

        api.delete_tag.side_effect = delete
        assert "exhaustive-gone" in exhaustive_reconcile.strip_orphans(api, {"exhaustive-picks"}, {"exhaustive-gone", "exhaustive-picks"}, LOG)

    def test_a_delete_that_really_failed_raises(self):
        api = _api({"exhaustive-gone": 2, "exhaustive-picks": 1}, {10: ["exhaustive-gone"]})
        api.delete_tag.side_effect = ValueError("no JSON")
        with pytest.raises(ValueError):
            exhaustive_reconcile.strip_orphans(api, {"exhaustive-picks"}, {"exhaustive-gone", "exhaustive-picks"}, LOG)


class TestReconcile:
    def test_a_deleted_chart_loses_its_recorded_tag_and_the_record_forgets_it(self, tmp_path):
        config, movies = _config(tmp_path, "  Picks:\n    acquire: exhaustive\n  A:\n    acquire: exhaustive\n  B:\n    acquire: exhaustive\n")
        _record(tmp_path, ["exhaustive-picks", "exhaustive-gone", "exhaustive-a", "exhaustive-b", "exhaustive-loud"])
        api = _api(TAGS, {10: ["exhaustive-gone"], 14: ["exhaustive-picks"], 15: ["exhaustive-keepers"]})
        assert exhaustive_reconcile.reconcile(config, log=LOG, make_api=lambda url, token: api) == {1: {"exhaustive-gone": 1, "exhaustive-loud": 0}}
        assert _recorded_labels(tmp_path) == {"exhaustive-picks", "exhaustive-a", "exhaustive-b"}
        api.reset_mock()
        movies.write_text("collections: [unclosed\n")
        assert exhaustive_reconcile.reconcile(config, log=LOG, make_api=lambda url, token: api) is None
        api.all_tags.assert_not_called()

    def test_an_unrecorded_orphan_is_never_stripped(self, tmp_path):
        config, _ = _config(tmp_path, "  Picks:\n    tmdb_movie: 1\n")
        _record(tmp_path, [])
        api = _api({"exhaustive-picks": 1}, {10: ["exhaustive-picks"]})
        assert exhaustive_reconcile.reconcile(config, log=LOG, make_api=lambda url, token: api) == {1: {}}
        api.edit_multiple_movies.assert_not_called()
        api.delete_tag.assert_not_called()

    def test_a_scan_that_lost_collections_costs_at_most_two_tags_a_pass(self, tmp_path):
        names = ["a", "b", "c", "d", "e", "f"]
        tags = {f"exhaustive-{n}": i for i, n in enumerate(names, 1)}
        config, movies = _config(tmp_path, "".join(f"  {n}:\n    acquire: exhaustive\n" for n in names[:1]))
        _record(tmp_path, list(tags))
        api = _api(tags, {i: [f"exhaustive-{n}"] for i, n in enumerate(names, 1)})
        assert set(exhaustive_reconcile.reconcile(config, log=LOG, make_api=lambda url, token: api)[1]) == {"exhaustive-b", "exhaustive-c"}
        assert api.delete_tag.call_count == 2

    def test_removing_the_key_orphans_a_recorded_tag(self, tmp_path):
        config, _ = _config(tmp_path, "  Picks:\n    tmdb_movie: 1\n")
        _record(tmp_path, ["exhaustive-picks"])
        api = _api({"exhaustive-picks": 1}, {10: ["exhaustive-picks"]})
        assert exhaustive_reconcile.reconcile(config, log=LOG, make_api=lambda url, token: api) == {1: {"exhaustive-picks": 1}}
        assert _recorded_labels(tmp_path) == set()

    def test_a_failing_instance_keeps_the_record_and_never_raises(self, tmp_path, caplog):
        config, _ = _config(tmp_path, "  Picks:\n    tmdb_movie: 1\n")
        _record(tmp_path, ["exhaustive-picks"])

        def boom(url, token):
            raise ConnectionError(f"bad {token}")

        with caplog.at_level(logging.DEBUG):
            assert exhaustive_reconcile.reconcile(config, log=LOG, make_api=boom) == {}
        assert "secret" not in caplog.text
        assert _recorded_labels(tmp_path) == {"exhaustive-picks"}

    def test_no_radarr_in_the_config_is_nothing_to_do(self, tmp_path):
        config, _ = _config(tmp_path, "  Picks:\n    acquire: exhaustive\n", radarr="")
        assert exhaustive_reconcile.reconcile(config, log=LOG) == {}

    def test_an_unopenable_record_is_a_logged_skip(self, tmp_path, caplog):
        config, _ = _config(tmp_path, "  Picks:\n    tmdb_movie: 1\n")
        with caplog.at_level(logging.WARNING):
            assert exhaustive_reconcile.reconcile(config, log=LOG, make_api=lambda u, t: _api({}, {}), record_path=str(tmp_path / "no" / "such" / "dir" / "r.db")) is None
        assert "live record" in caplog.text

    def test_radarr_instances_come_from_the_global_section_and_each_library(self):
        config = {"radarr": {"url": "http://a/", "token": "1"}, "libraries": {"M": {"radarr": {"url": "http://b", "token": "2"}}, "N": {"radarr": {"url": "http://a", "token": "1"}}, "O": {"radarr": {"url": "<<u>>", "token": "3"}}}}
        assert exhaustive_reconcile.radarr_instances(config) == [("http://a", "1"), ("http://b", "2")]


class TestTimeout:
    def test_bound_gives_every_request_a_timeout_once(self):
        seen = []
        session = SimpleNamespace(request=lambda *a, **k: seen.append(k))
        api = SimpleNamespace(session=session)
        exhaustive_reconcile.bound(api)
        wrapped = session.request
        exhaustive_reconcile.bound(api)
        assert session.request is wrapped
        session.request("GET", "http://x")
        session.request("GET", "http://x", timeout=3)
        assert seen[0]["timeout"] == exhaustive_reconcile.TIMEOUT and seen[1]["timeout"] == 3

    def test_a_radarr_that_never_answers_is_a_logged_skip_not_a_hang(self, tmp_path, monkeypatch, caplog):
        import socket
        import time

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(5)  # accepts at the kernel level and never replies
        monkeypatch.setattr(exhaustive_reconcile, "TIMEOUT", (0.5, 0.5))
        config, _ = _config(tmp_path, "  Picks:\n    tmdb_movie: 1\n", radarr=f"radarr:\n  url: http://127.0.0.1:{server.getsockname()[1]}\n  token: secret\n")
        _record(tmp_path, ["exhaustive-picks"])
        started = time.monotonic()
        try:
            with caplog.at_level(logging.ERROR):
                assert exhaustive_reconcile.reconcile(config, log=LOG) == {}
        finally:
            server.close()
        assert time.monotonic() - started < 10
        assert "could not look for orphaned tags" in caplog.text and "secret" not in caplog.text
        assert _recorded_labels(tmp_path) == {"exhaustive-picks"}


def test_the_record_counts_what_it_held_before(tmp_path):
    rec = live_record.RunRecorder(str(tmp_path), FakeLogger())
    assert rec.wanted_before("Movies", "Picks") is None
    conn = live_record.connect(live_record.db_path(str(tmp_path)))
    from tests.test_exhaustive import _snap

    live_record.write_collection(conn, _snap([1, 2], None))
    conn.close()
    assert rec.wanted_before("Movies", "Picks") == 2
    assert add_budget.exhaustive_tag("Picks") == "exhaustive-picks"

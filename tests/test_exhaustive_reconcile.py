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


TAGS = {"exhaustive-picks": 1, "exhaustive-gone": 2, "exhaustive": 3, "exhaustive_x": 4, "exhaustive-": 5, "kl-gone": 6, "Exhaustive-Loud": 7}


class TestStripOrphans:
    def test_only_unowned_exhaustive_slug_tags_are_stripped_and_deleted(self):
        api = _api(TAGS, {10: ["exhaustive-gone"], 11: ["exhaustive-gone", "exhaustive-picks"], 12: ["exhaustive"], 13: ["kl-gone", "exhaustive_x"], 14: ["exhaustive-picks"]})
        result = exhaustive_reconcile.strip_orphans(api, {"exhaustive-picks"}, logging.getLogger("t"))
        assert result == {"exhaustive-gone": 2, "exhaustive-loud": 0}
        calls = [(c.args[0], c.kwargs["tags"], c.kwargs["apply_tags"]) for c in api.edit_multiple_movies.call_args_list]
        assert calls == [([10, 11], ["exhaustive-gone"], "remove")]
        assert [c.args[0] for c in api.delete_tag.call_args_list] == [2, 7]

    def test_nothing_orphaned_touches_nothing_and_reads_no_movies(self):
        api = _api(TAGS, {10: ["exhaustive-picks"]})
        assert exhaustive_reconcile.strip_orphans(api, {"exhaustive-picks", "exhaustive-gone", "exhaustive-loud"}, logging.getLogger("t")) == {}
        api.all_movies.assert_not_called()
        api.delete_tag.assert_not_called()

    def test_a_delete_that_fails_to_read_radarrs_reply_is_fine_once_the_tag_is_gone(self):
        state = {"tags": dict(TAGS)}
        api = _api(TAGS, {10: ["exhaustive-gone"]})
        api.all_tags.side_effect = lambda: [SimpleNamespace(id=i, label=label) for label, i in state["tags"].items()]

        def delete(tag_id):
            state["tags"] = {k: v for k, v in state["tags"].items() if v != tag_id}
            raise ValueError("no JSON")

        api.delete_tag.side_effect = delete
        assert "exhaustive-gone" in exhaustive_reconcile.strip_orphans(api, {"exhaustive-picks"}, logging.getLogger("t"))

    def test_a_delete_that_really_failed_raises(self):
        api = _api(TAGS, {10: ["exhaustive-gone"]})
        api.delete_tag.side_effect = ValueError("no JSON")
        with pytest.raises(ValueError):
            exhaustive_reconcile.strip_orphans(api, {"exhaustive-picks"}, logging.getLogger("t"))


class TestReconcile:
    def test_a_deleted_chart_loses_its_tag_and_a_broken_config_touches_nothing(self, tmp_path):
        config, movies = _config(tmp_path, "  Picks:\n    acquire: exhaustive\n")
        api = _api(TAGS, {10: ["exhaustive-gone"], 14: ["exhaustive-picks"]})
        log = logging.getLogger("t")
        assert exhaustive_reconcile.reconcile(config, log=log, make_api=lambda url, token: api) == {1: {"exhaustive-gone": 1, "exhaustive-loud": 0}}
        api.reset_mock()
        movies.write_text("collections: [unclosed\n")
        assert exhaustive_reconcile.reconcile(config, log=log, make_api=lambda url, token: api) is None
        api.all_tags.assert_not_called()

    def test_removing_the_key_orphans_the_tag(self, tmp_path):
        config, movies = _config(tmp_path, "  Picks:\n    tmdb_movie: 1\n")
        api = _api({"exhaustive-picks": 1}, {10: ["exhaustive-picks"]})
        assert exhaustive_reconcile.reconcile(config, log=logging.getLogger("t"), make_api=lambda url, token: api) == {1: {"exhaustive-picks": 1}}

    def test_no_radarr_in_the_config_is_nothing_to_do_and_a_radarr_error_never_raises(self, tmp_path):
        config, _ = _config(tmp_path, "  Picks:\n    acquire: exhaustive\n", radarr="")
        assert exhaustive_reconcile.reconcile(config, log=logging.getLogger("t")) == {}
        config, _ = _config(tmp_path, "  Picks:\n    acquire: exhaustive\n")

        def boom(url, token):
            raise ConnectionError("down")

        assert exhaustive_reconcile.reconcile(config, log=logging.getLogger("t"), make_api=boom) == {}

    def test_the_log_never_carries_the_token(self, tmp_path, caplog):
        config, _ = _config(tmp_path, "  Picks:\n    acquire: exhaustive\n")

        def boom(url, token):
            raise ConnectionError(f"bad {token}")

        with caplog.at_level(logging.DEBUG):
            exhaustive_reconcile.reconcile(config, log=logging.getLogger("t"), make_api=boom)
        assert "secret" not in caplog.text

    def test_radarr_instances_come_from_the_global_section_and_each_library(self):
        config = {"radarr": {"url": "http://a/", "token": "1"}, "libraries": {"M": {"radarr": {"url": "http://b", "token": "2"}}, "N": {"radarr": {"url": "http://a", "token": "1"}}, "O": {"radarr": {"url": "<<u>>", "token": "3"}}}}
        assert exhaustive_reconcile.radarr_instances(config) == [("http://a", "1"), ("http://b", "2")]


def test_the_record_counts_what_it_held_before(tmp_path):
    rec = live_record.RunRecorder(str(tmp_path), FakeLogger())
    assert rec.wanted_before("Movies", "Picks") is None
    conn = live_record.connect(live_record.db_path(str(tmp_path)))
    from tests.test_exhaustive import _snap

    live_record.write_collection(conn, _snap([1, 2], None))
    conn.close()
    assert rec.wanted_before("Movies", "Picks") == 2
    assert add_budget.exhaustive_tag("Picks") == "exhaustive-picks"

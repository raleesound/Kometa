"""Tests for modules/convert.py — ID lookups across sources."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from tenacity import RetryError

import modules.builder  # noqa: F401


class TestConvert:
    @pytest.fixture
    def adapter(self):
        from modules.convert import Convert

        c = Convert.__new__(Convert)
        c.requests = MagicMock()
        c.cache = MagicMock()
        c.tmdb = MagicMock()
        return c

    def test_tmdb_to_imdb_cache_hit(self, adapter):
        adapter.cache.query_imdb_to_tmdb_map.return_value = ("tt999", False)
        assert adapter.tmdb_to_imdb(550, is_movie=True, fail=False) == "tt999"

    def test_imdb_to_tmdb_cache_hit(self, adapter):
        adapter.cache.query_imdb_to_tmdb_map.return_value = (550, False, None)
        tmdb_id, _ = adapter.imdb_to_tmdb("tt123", fail=False)
        assert tmdb_id == 550

    def test_tmdb_to_tvdb_cache_hit(self, adapter):
        adapter.cache.query_tmdb_to_tvdb_map.return_value = (368207, False)
        assert adapter.tmdb_to_tvdb(550, fail=False) == 368207

    def test_tvdb_to_tmdb_cache_hit(self, adapter):
        adapter.cache.query_tmdb_to_tvdb_map.return_value = (550, False)
        assert adapter.tvdb_to_tmdb(368207, fail=False) == 550

    def test_anidb_ids_fetch_failure_is_non_fatal(self, monkeypatch):
        from modules.convert import Convert

        requests = MagicMock()
        requests.get_json.side_effect = ValueError("invalid JSON")
        logger = MagicMock()
        monkeypatch.setattr("modules.convert.logger", logger)

        convert = Convert(requests, MagicMock(), MagicMock())

        assert convert._anidb_ids == {}
        logger.error.assert_called_once_with("Convert Error: Unable to fetch AniDB IDs from https://raw.githubusercontent.com/Kometa-Team/Anime-IDs/master/anime_ids.json: invalid JSON")

    def test_anidb_ids_retry_failure_is_non_fatal(self, monkeypatch):
        from modules.convert import Convert

        requests = MagicMock()
        requests.get_json.side_effect = RetryError(MagicMock())
        monkeypatch.setattr("modules.convert.logger", MagicMock())

        assert Convert(requests, MagicMock(), MagicMock())._anidb_ids == {}

    def test_anidb_ids_rate_limit_is_non_fatal(self, monkeypatch):
        from modules.convert import Convert
        from modules.util import Failed

        requests = MagicMock()
        requests.get_json.side_effect = Failed("URL Error: (429) Too Many Requests")
        monkeypatch.setattr("modules.convert.logger", MagicMock())

        assert Convert(requests, MagicMock(), MagicMock())._anidb_ids == {}

    def test_hama_suffix_extracts_trailing_id(self):
        from modules.convert import Convert

        assert Convert._hama_suffix("anidb-12345") == "12345"
        assert Convert._hama_suffix("tvdb-67890") == "67890"
        # Hama also has an 'aNNN' anidb format the call sites peel a prefix off later;
        # _hama_suffix just returns everything after the first dash unchanged.
        assert Convert._hama_suffix("anidb-a987") == "a987"

    def test_hama_suffix_raises_on_malformed_id(self):
        from modules.convert import Convert
        from modules.util import MappingConvertError

        with pytest.raises(MappingConvertError, match="Malformed Hama ID 'anidb'"):
            Convert._hama_suffix("anidb")

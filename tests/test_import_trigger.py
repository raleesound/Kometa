"""Tests for deploy/trigger/import_trigger.py, the websocket-driven import trigger.

The trigger is a standalone script bind-mounted into its container, so it is
loaded here by path. Plex is faked: nothing here opens a socket.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from plexapi.exceptions import NotFound

TRIGGER_PATH = Path(__file__).resolve().parent.parent / "deploy" / "trigger" / "import_trigger.py"


KOMETA_CONFIG = "plex:\n  url: http://plex:32400\n  token: t\nlibraries:\n  Movies:\n    operations: {}\n  TV Shows:\n"


@pytest.fixture
def trig(monkeypatch, tmp_path):
    config = tmp_path / "config.yml"
    config.write_text(KOMETA_CONFIG)
    spec = importlib.util.spec_from_file_location("import_trigger_under_test", TRIGGER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DEBOUNCE_SECONDS", 90)
    monkeypatch.setattr(module, "RETRY_SECONDS", 150)
    monkeypatch.setattr(module, "NEW_ITEM_WINDOW_SECONDS", 6 * 3600)
    monkeypatch.setattr(module, "MAX_CATCH_UP_SECONDS", 7 * 86400)
    monkeypatch.setattr(module, "CONFIG_PATH", str(config))
    return module


@pytest.fixture
def pending(trig, tmp_path):
    return trig.Pending(str(tmp_path / "queue.json"))


NOW = time.time()


def admit(pending, key, library="Movies", target=None, added=NOW - 60):
    return pending.admit(library, str(key), str(target or key), added, now=NOW)


def movie(key, added=NOW - 60, guid="plex://movie/abc", title="A Film"):
    return SimpleNamespace(type="movie", ratingKey=key, guid=guid, title=title, addedAt=datetime.fromtimestamp(added), grandparentTitle=None)


def episode(key, show, added=NOW - 60, guid="plex://episode/abc"):
    return SimpleNamespace(
        type="episode",
        ratingKey=key,
        grandparentRatingKey=show,
        parentRatingKey=show + 1,
        guid=guid,
        title=f"Episode {key}",
        grandparentTitle="A Show",
        addedAt=datetime.fromtimestamp(added),
    )


def timeline(*entries):
    return {"type": "timeline", "size": len(entries), "TimelineEntry": list(entries)}


def entry(item_id, section="2", state=5, kind=1, identifier="com.plexapp.plugins.library"):
    return {"identifier": identifier, "sectionID": section, "itemID": str(item_id), "type": kind, "state": state, "title": "x"}


class FakeSection:
    def __init__(self, key, title, kind, items=()):
        self.key, self.title, self.type, self.items = key, title, kind, list(items)
        self.searches = []

    def search(self, libtype=None, filters=None):
        self.searches.append((libtype, filters))
        since = filters["addedAt>>"]
        return [i for i in self.items if i.addedAt > since]


class FakeServer:
    def __init__(self, sections=(), items=()):
        self.sections_by_id = {int(s.key): s for s in sections}
        self.items = {int(i.ratingKey): i for i in items}
        self.library = SimpleNamespace(sectionByID=lambda key: self.sections_by_id[key], sections=lambda: list(self.sections_by_id.values()))

    def fetchItem(self, key):  # noqa: N802 - plexapi API
        if key not in self.items:
            raise NotFound(f"{key} not found")
        return self.items[key]

    def url(self, key, includeToken=False):  # noqa: N803 - plexapi API
        return f"http://10.0.0.1:32400{key}" + ("?X-Plex-Token=SECRET" if includeToken else "")


# ── the new-item filter ──────────────────────────────────────────────────────


class TestManagedLibraries:
    def test_maps_plex_names_to_config_keys(self, trig):
        Path(trig.CONFIG_PATH).write_text("libraries:\n  Movies:\n  Shows:\n    library_name: TV Shows\n")
        assert trig.managed_libraries() == {"Movies": "Movies", "TV Shows": "Shows"}

    def test_queues_under_the_key_kometa_matches(self, trig, pending):
        # Kometa matches --run-libraries against the config key, not the Plex name.
        Path(trig.CONFIG_PATH).write_text("libraries:\n  Shows:\n    library_name: TV Shows\n")
        assert trig.consider(pending, "TV Shows", movie(10)) == "10"
        assert trig.consider(pending, "Shows", movie(11)) is None
        assert pending.items == {"Shows": {"10": 0}}

    def test_unmanaged_library_is_never_queued(self, trig, pending):
        # Live 11-12: a film in "Involuntary Film Club" produced a Kometa run
        # that matched no library, exited 0 and was logged as finished.
        assert trig.consider(pending, "Involuntary Film Club", movie(10)) is None
        assert pending.items == {}
        assert trig.consider(pending, "Movies", movie(11)) == "11"

    def test_sweep_skips_unmanaged_sections(self, trig, pending):
        ifc = FakeSection(4, "Involuntary Film Club", "movie", [movie(10)])
        movies = FakeSection(2, "Movies", "movie", [movie(11)])
        assert trig.catch_up(FakeServer(sections=[ifc, movies]), pending, now=NOW) == 1
        assert ifc.searches == [] and pending.items == {"Movies": {"11": 0}}

    def test_config_change_is_picked_up(self, trig, pending):
        assert trig.consider(pending, "Anime", movie(10)) is None
        path = Path(trig.CONFIG_PATH)
        path.write_text(KOMETA_CONFIG + "  Anime:\n")
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
        assert trig.consider(pending, "Anime", movie(10)) == "10"

    def test_broken_config_keeps_the_last_good_list(self, trig, pending, caplog):
        assert trig.consider(pending, "Movies", movie(10)) == "10"
        path = Path(trig.CONFIG_PATH)
        path.write_text("{half written: [")
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
        assert trig.consider(pending, "Involuntary Film Club", movie(11)) is None
        assert trig.consider(pending, "Movies", movie(12)) == "12"
        assert [r.levelname for r in caplog.records].count("ERROR") == 1  # once per broken version

    @pytest.mark.parametrize("text", ["{not yaml: [", "- a list\n"])
    def test_unreadable_config_queues_everything_loudly(self, trig, pending, caplog, text):
        Path(trig.CONFIG_PATH).write_text(text)
        assert trig.consider(pending, "Involuntary Film Club", movie(10)) == "10"
        assert any(r.levelname == "ERROR" for r in caplog.records)

    def test_yaml_error_never_logs_the_file(self, trig, pending, caplog, monkeypatch):
        secret = "s3cr3t-T0KEN-value"
        Path(trig.CONFIG_PATH).write_text(f"plex:\n  token: {secret}: [\nlibraries:\n  Movies:\n")
        # Parse the whole text, as a load from a string would: the error then
        # quotes the token line, so only the logging guard keeps it out.
        real = trig.yaml.safe_load
        monkeypatch.setattr(trig.yaml, "safe_load", lambda fh: real(fh.read()))
        caplog.set_level("DEBUG")
        trig.consider(pending, "Movies", movie(10))
        assert caplog.records, "the failure must be logged"
        assert any("line 2" in r.getMessage() for r in caplog.records)
        assert all(secret not in r.getMessage() and secret not in str(r.args) for r in caplog.records)

    def test_yaml_error_text_never_reaches_the_log(self, trig):
        # PyYAML quotes the offending line when it holds the whole document; that
        # line can be the token line.
        secret = "s3cr3t-T0KEN-value"
        with pytest.raises(trig.yaml.YAMLError) as caught:
            trig.yaml.safe_load(f"plex:\n  token: {secret}: [\n")
        assert secret in str(caught.value)  # the leak this guards against is real
        described = trig.describe_config_error(caught.value)
        assert secret not in described and described.startswith("ScannerError at line 2, column")

    def test_missing_config_logs_once(self, trig, pending, caplog):
        Path(trig.CONFIG_PATH).unlink()
        for key in (10, 11, 12):
            trig.consider(pending, "Movies", movie(key))
        assert [r.levelname for r in caplog.records].count("ERROR") == 1

    def test_ignored_library_is_announced_once(self, trig, pending, caplog):
        caplog.set_level("INFO")
        for key in (10, 11):
            trig.consider(pending, "Involuntary Film Club", movie(key))
        notes = [r for r in caplog.records if "Involuntary Film Club" in r.getMessage()]
        assert len(notes) == 1 and notes[0].levelname == "INFO"

    def test_empty_library_list_warns(self, trig, pending, caplog):
        Path(trig.CONFIG_PATH).write_text("libraries:\n")
        assert trig.consider(pending, "Movies", movie(10)) is None
        assert any(r.levelname == "WARNING" for r in caplog.records)

    def test_missing_config_queues_everything_loudly(self, trig, pending, caplog):
        Path(trig.CONFIG_PATH).unlink()
        assert trig.consider(pending, "Involuntary Film Club", movie(10)) == "10"
        assert any(r.levelname == "ERROR" for r in caplog.records)


class TestProcessedEntries:
    def test_keeps_processed_movie_and_episode(self, trig):
        data = timeline(entry(1, kind=1), entry(2, kind=4))
        assert [e["itemID"] for e in trig.processed_entries(data)] == ["1", "2"]

    @pytest.mark.parametrize(
        "bad",
        [
            entry(1, state=0),  # created, not yet processed
            entry(1, state=1),
            entry(1, state=9),  # deleted
            entry(1, kind=2),  # show: its episodes are the imports
            entry(1, kind=3),  # season
            entry(1, kind=8),  # music
            entry(1, identifier="com.plexapp.plugins.other"),
            entry(1, section="-1"),
        ],
    )
    def test_drops_everything_else(self, trig, bad):
        assert trig.processed_entries(timeline(bad)) == []

    @pytest.mark.parametrize("junk", [{"type": "timeline", "TimelineEntry": ["junk"]}, {"type": "timeline", "TimelineEntry": {"a": 1}}, ["not", "a", "dict"], None])
    def test_junk_is_ignored_not_raised(self, trig, junk):
        assert trig.processed_entries(junk) == []

    def test_ignores_non_timeline_notifications(self, trig):
        assert trig.processed_entries({"type": "playing", "PlaySessionStateNotification": [{}]}) == []
        assert trig.processed_entries({"type": "timeline"}) == []


class TestConsider:
    def test_new_movie_is_queued(self, trig, pending):
        assert trig.consider(pending, "Movies", movie(10), now=NOW) == "10"
        assert pending.items == {"Movies": {"10": 0}}

    def test_refresh_of_old_movie_is_not_an_import(self, trig, pending):
        assert trig.consider(pending, "Movies", movie(10, added=NOW - 3 * 86400), now=NOW) is None
        assert pending.items == {}

    def test_future_pinned_movie_is_not_an_import(self, trig, pending):
        pinned = movie(10, added=datetime(2097, 12, 31, 19).timestamp())
        assert trig.consider(pending, "Movies", pinned, now=NOW) is None
        assert pending.items == {}

    def test_second_processed_event_for_same_item_is_deduped(self, trig, pending):
        assert trig.consider(pending, "Movies", movie(10), now=NOW) == "10"
        pending.items.clear()  # as if the batch already ran
        assert trig.consider(pending, "Movies", movie(10), now=NOW) is None
        assert pending.items == {}

    def test_episode_queues_its_show(self, trig, pending):
        assert trig.consider(pending, "TV Shows", episode(501, show=500), now=NOW) == "500"
        assert trig.consider(pending, "TV Shows", episode(502, show=500), now=NOW) == "500"
        assert pending.items == {"TV Shows": {"500": 0}}
        assert set(pending.seen) == {"501", "502"}

    def test_season_resolves_to_show(self, trig):
        season = SimpleNamespace(type="season", ratingKey=7, parentRatingKey=5)
        assert trig.target_key(season) == "5"

    def test_missing_added_at_is_ignored(self, trig, pending):
        item = movie(10)
        item.addedAt = None
        assert trig.consider(pending, "Movies", item, now=NOW) is None

    def test_seen_survives_restart(self, trig, pending, tmp_path):
        trig.consider(pending, "Movies", movie(10), now=NOW)
        reloaded = trig.Pending(pending.path)
        assert reloaded.items == {"Movies": {"10": 0}}
        assert trig.consider(reloaded, "Movies", movie(10), now=NOW) is None

    def test_seen_entries_age_out_with_the_window(self, trig, pending):
        assert pending.admit("Movies", "10", "10", NOW - 100, now=NOW)
        later = NOW + trig.NEW_ITEM_WINDOW_SECONDS
        assert pending.admit("Movies", "11", "11", later - 1, now=later)
        assert "10" not in pending.seen

    def test_unmatched_episode_give_up_frees_the_whole_show(self, trig, pending):
        trig.consider(pending, "TV Shows", episode(501, show=500), now=NOW)
        trig.consider(pending, "TV Shows", episode(502, show=500), now=NOW)
        pending.forget("500")  # what a give-up on the show does
        pending.items.clear()
        assert pending.seen == {}
        assert trig.consider(pending, "TV Shows", episode(502, show=500), now=NOW) == "500"

    def test_forget_lets_the_item_queue_again(self, trig, pending):
        trig.consider(pending, "Movies", movie(10), now=NOW)
        pending.forget("10")
        pending.items.clear()
        assert trig.consider(pending, "Movies", movie(10), now=NOW) == "10"


# ── websocket notifications end to end ──────────────────────────────────────


class TestHandleNotification:
    def test_queues_new_items_and_skips_refreshes(self, trig, pending):
        server = FakeServer(
            sections=[FakeSection(2, "Movies", "movie"), FakeSection(1, "TV Shows", "show")],
            items=[movie(10), movie(11, added=NOW - 30 * 86400), episode(501, show=500)],
        )
        trig.handle_notification(server, pending, timeline(entry(10), entry(11), entry(501, section="1", kind=4)))
        assert pending.items == {"Movies": {"10": 0}, "TV Shows": {"500": 0}}

    def test_vanished_item_and_lookup_errors_do_not_raise(self, trig, pending):
        server = FakeServer(sections=[FakeSection(2, "Movies", "movie")], items=[movie(12)])
        # 99 is gone (NotFound); section 7 does not exist (KeyError) - neither may break the rest.
        failed = trig.handle_notification(server, pending, timeline(entry(99), entry(12, section="7"), entry(12)))
        assert pending.items == {"Movies": {"12": 0}}
        # A vanished item is not a failure (nothing to apply); an unknown section is, and is retried.
        assert [e["sectionID"] for e in failed] == ["7"]

    def test_failed_live_lookup_is_retried_not_dropped(self, trig, pending, monkeypatch):
        monkeypatch.setattr(trig, "RETRY_SECONDS", 0.01)
        server = FakeServer(sections=[FakeSection(2, "Movies", "movie")], items=[movie(10)])
        real, calls = server.fetchItem, []

        def flaky(key):
            calls.append(key)
            if len(calls) == 1:
                raise ConnectionResetError("plex busy")
            return real(key)

        server.fetchItem = flaky
        inbox = trig.queue.Queue()
        threading.Thread(target=trig.resolve_forever, args=(inbox, pending), daemon=True).start()
        inbox.put((server, timeline(entry(10)), 0))
        deadline = time.time() + 5
        while not pending.items and time.time() < deadline:
            time.sleep(0.01)
        assert pending.items == {"Movies": {"10": 0}} and calls == [10, 10]

    def test_resolver_survives_anything(self, trig, pending, monkeypatch):
        # The resolver is the inbox's only consumer: if it died, every later
        # live arrival would sit unread.
        def boom(*a):
            raise RuntimeError("bug")

        monkeypatch.setattr(trig, "handle_notification", boom)
        monkeypatch.setattr(trig, "processed_entries", boom)
        inbox = trig.queue.Queue()
        thread = threading.Thread(target=trig.resolve_forever, args=(inbox, pending), daemon=True)
        thread.start()
        inbox.put((None, {"type": "timeline", "TimelineEntry": ["junk"]}, 0))
        inbox.put((None, timeline(), 0))
        deadline = time.time() + 5
        while inbox.unfinished_tasks and not inbox.empty() and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        assert thread.is_alive() and inbox.empty()

    def test_resolver_survives_a_failed_retry_schedule(self, trig, pending, monkeypatch):
        handled = []

        def handle(server, pending, data):
            handled.append(data)
            return [{"identifier": "x"}]

        def boom(*a):
            raise RuntimeError("cannot start timer")

        monkeypatch.setattr(trig, "handle_notification", handle)
        monkeypatch.setattr(trig, "retry_lookup", boom)
        inbox = trig.queue.Queue()
        thread = threading.Thread(target=trig.resolve_forever, args=(inbox, pending), daemon=True)
        thread.start()
        inbox.put((None, timeline(), 0))
        inbox.put((None, timeline(), 0))
        deadline = time.time() + 5
        while len(handled) < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert len(handled) == 2 and thread.is_alive()

    def test_lookup_retries_are_bounded(self, trig, monkeypatch):
        monkeypatch.setattr(trig, "MAX_ATTEMPTS", 3)
        started = []
        monkeypatch.setattr(trig.threading, "Timer", lambda *a, **k: SimpleNamespace(start=lambda: started.append(k["args"]), daemon=False))
        inbox = trig.queue.Queue()
        trig.retry_lookup(inbox, None, [entry(10)], 1)
        trig.retry_lookup(inbox, None, [entry(10)], 2)
        assert len(started) == 1 and started[0][0][2] == 2

    def test_library_comes_from_the_item_first(self, trig, pending):
        # A library created after connect is missing from the cached section map.
        Path(trig.CONFIG_PATH).write_text(KOMETA_CONFIG + "  New Library:\n")
        film = movie(10)
        film.librarySectionTitle = "New Library"
        server = FakeServer(sections=[], items=[film])
        trig.handle_notification(server, pending, timeline(entry(10, section="9")))
        assert pending.items == {"New Library": {"10": 0}}

    def test_resolver_runs_off_the_socket_thread(self, trig, pending, monkeypatch):
        # A slow Plex lookup must not block the websocket callback (and its pings).
        release, handled = threading.Event(), threading.Event()

        def slow(server, pending, data):
            release.wait(timeout=10)
            handled.set()

        monkeypatch.setattr(trig, "handle_notification", slow)
        inbox = trig.queue.Queue()
        threading.Thread(target=trig.resolve_forever, args=(inbox, pending), daemon=True).start()
        inbox.put((None, timeline(), 0))  # what the Listener callback does: returns at once
        assert not handled.wait(timeout=0.2)
        release.set()
        assert handled.wait(timeout=10)


class TestCatchUp:
    def test_queues_unseen_recent_items_only(self, trig, pending):
        movies = FakeSection(2, "Movies", "movie", [movie(10), movie(11, added=NOW - 2 * 86400), movie(12)])
        shows = FakeSection(1, "TV Shows", "show", [episode(501, show=500)])
        music = FakeSection(3, "Music", "artist", [movie(900)])
        server = FakeServer(sections=[movies, shows, music])
        admit(pending, 12)  # already handled before the restart
        pending.items.clear()
        assert trig.catch_up(server, pending, now=NOW) == 2
        assert pending.items == {"Movies": {"10": 0}, "TV Shows": {"500": 0}}
        assert movies.searches[0][0] == "movie" and shows.searches[0][0] == "episode"
        assert music.searches == []

    def test_second_sweep_queues_nothing(self, trig, pending):
        server = FakeServer(sections=[FakeSection(2, "Movies", "movie", [movie(10)])])
        assert trig.catch_up(server, pending, now=NOW) == 1
        assert trig.catch_up(server, pending, now=NOW) == 0

    def test_outage_longer_than_the_window_loses_nothing(self, trig, pending):
        # Last sweep 3 days ago; Plex (or the trigger) was down since. A film that
        # landed 2 days ago is still an arrival we owe a run.
        movies = FakeSection(2, "Movies", "movie", [movie(10, added=NOW - 2 * 86400), movie(11, added=NOW - 4 * 86400)])
        pending.mark_caught_up(NOW - 3 * 86400)
        reloaded = trig.Pending(pending.path)  # the horizon is persisted
        assert trig.catch_up(FakeServer(sections=[movies]), reloaded, now=NOW) == 1
        assert reloaded.items == {"Movies": {"10": 0}}
        assert reloaded.horizon == NOW

    def test_catch_up_reach_is_capped(self, trig, pending):
        movies = FakeSection(2, "Movies", "movie", [movie(10, added=NOW - 8 * 86400), movie(11, added=NOW - 6 * 86400)])
        pending.mark_caught_up(NOW - 30 * 86400)
        assert trig.catch_up(FakeServer(sections=[movies]), pending, now=NOW) == 1
        assert pending.items == {"Movies": {"11": 0}}
        assert movies.searches[0][1]["addedAt>>"] >= datetime.fromtimestamp(NOW - 7 * 86400)

    def test_failed_sweep_keeps_the_old_horizon(self, trig, pending):
        class Broken(FakeSection):
            def search(self, libtype=None, filters=None):
                raise ConnectionResetError("reset")

        pending.mark_caught_up(NOW - 86400)
        with pytest.raises(ConnectionResetError):
            trig.catch_up(FakeServer(sections=[Broken(2, "Movies", "movie")]), pending, now=NOW)
        assert pending.horizon == NOW - 86400

    def test_live_window_returns_to_normal_after_a_sweep(self, trig, pending):
        trig.catch_up(FakeServer(), pending, now=NOW)
        # caught up: a refresh of a 2-day-old film is not an arrival
        assert trig.consider(pending, "Movies", movie(10, added=NOW - 2 * 86400), now=NOW) is None


# ── the websocket session and reconnects ─────────────────────────────────────


class TestListener:
    def test_dispatches_notification_container(self, trig):
        got = []
        listener = trig.Listener(FakeServer(), got.append)
        listener._message(None, json.dumps({"NotificationContainer": timeline(entry(1))}))
        assert got == [timeline(entry(1))]

    def test_bad_messages_and_handler_errors_are_contained(self, trig):
        def boom(_data):
            raise RuntimeError("handler bug")

        listener = trig.Listener(FakeServer(), boom)
        listener._message(None, "not json")
        listener._message(None, json.dumps({"other": 1}))
        listener._message(None, json.dumps({"NotificationContainer": {}}))  # handler raises, swallowed

    def test_run_connects_websocket_with_keepalive(self, trig, monkeypatch):
        seen, seen_timeouts = {}, []

        class FakeApp:
            def __init__(self, url, **callbacks):
                seen["url"] = url
                callbacks["on_open"](self)

            def run_forever(self, **kwargs):
                seen["kwargs"] = kwargs

        monkeypatch.setitem(sys.modules, "websocket", SimpleNamespace(WebSocketApp=FakeApp, setdefaulttimeout=lambda t: seen_timeouts.append(t)))
        listener = trig.Listener(FakeServer(), lambda d: None)
        listener.run()
        assert listener.ready.is_set() and listener.opened
        assert seen["url"].startswith("ws://10.0.0.1:32400/:/websockets/notifications?")
        assert seen["kwargs"]["ping_interval"] > seen["kwargs"]["ping_timeout"] > 0
        assert seen_timeouts == [trig.PLEX_TIMEOUT_SECONDS]  # a hung connect cannot leak the thread

    def test_token_never_logged(self, trig, monkeypatch, caplog):
        seen_timeouts = []

        class FakeApp:
            def __init__(self, url, on_error, **callbacks):
                on_error(self, ConnectionResetError("reset by peer"))

            def run_forever(self, **kwargs):
                pass

        monkeypatch.setitem(sys.modules, "websocket", SimpleNamespace(WebSocketApp=FakeApp, setdefaulttimeout=lambda t: seen_timeouts.append(t)))
        with caplog.at_level("DEBUG"):
            listener = trig.Listener(FakeServer(), lambda d: None)
            listener.run()
        assert "SECRET" not in caplog.text
        # never opened, yet the waiter is released (and will skip the sweep)
        assert listener.ready.is_set() and not listener.opened
        assert "reset by peer" in caplog.text


class TestListenForever:
    def _harness(self, trig, monkeypatch, sessions, pending):
        """Run listen_forever over a scripted list of sessions; stop after the last one."""
        monkeypatch.setattr(trig, "RECONNECT_MIN_SECONDS", 0)
        stop = threading.Event()
        events = []
        script = iter(sessions)

        def connect():
            step = next(script, None)
            if step is None:
                stop.set()
                raise ConnectionError("done")
            events.append(("connect", step))
            if step == "refused":
                raise ConnectionRefusedError("plex down")
            return FakeServer(sections=[FakeSection(2, "Movies", "movie", [movie(10)])])

        class FakeListener:
            def __init__(self, server, on_message):
                self.on_message = on_message
                self.ready, self.opened = threading.Event(), True
                self.ready.set()

            def run(self):
                events.append(("socket-open",))
                self.on_message(timeline())  # a notification arrives, then the socket drops
                events.append(("socket-closed",))

        trig.listen_forever(pending, connect=connect, listener_cls=FakeListener, stop=stop)
        return events

    def test_handler_never_runs_on_the_socket_thread(self, trig, monkeypatch, pending):
        # Plex lookups can take seconds; if they ran inside the websocket callback
        # the read loop (and its pings) would stall. The callback must only enqueue.
        monkeypatch.setattr(trig, "RECONNECT_MIN_SECONDS", 0)
        release, socket_threads, handler_threads = threading.Event(), [], []

        def slow(server, pending, data):
            handler_threads.append(threading.current_thread().name)
            release.wait(timeout=5)
            return []

        monkeypatch.setattr(trig, "handle_notification", slow)
        monkeypatch.setattr(trig, "catch_up", lambda *a, **k: 0)
        stop, calls, returned = threading.Event(), iter([1]), []

        def connect():
            if next(calls, None) is None:
                stop.set()
                raise ConnectionError("done")
            return FakeServer()

        class Fast:
            def __init__(self, server, on_message):
                self.on_message = on_message
                self.ready, self.opened = threading.Event(), True
                self.ready.set()

            def run(self):
                socket_threads.append(threading.current_thread().name)
                t0 = time.time()
                self.on_message(timeline(entry(1)))
                returned.append(time.time() - t0)

        trig.listen_forever(pending, connect=connect, listener_cls=Fast, stop=stop)
        release.set()
        assert returned and returned[0] < 1  # did not wait on the 5s handler
        deadline = time.time() + 5
        while not handler_threads and time.time() < deadline:
            time.sleep(0.01)
        assert handler_threads and handler_threads[0] != socket_threads[0]

    def test_reconnects_after_drop_and_catches_up(self, trig, monkeypatch, pending):
        events = self._harness(trig, monkeypatch, ["ok", "ok"], pending)
        assert events == [("connect", "ok"), ("socket-open",), ("socket-closed",)] * 2
        # catch-up ran on the first session and queued the film once, not twice
        assert pending.items == {"Movies": {"10": 0}}

    def test_never_reconnects_while_the_socket_is_open(self, trig, monkeypatch, pending):
        # The socket stays open until the next connect() (or 1s). If the loop did
        # not wait for the socket to close, connect #2 would land before close #1.
        monkeypatch.setattr(trig, "RECONNECT_MIN_SECONDS", 0)
        stop, reconnected, events = threading.Event(), threading.Event(), []
        calls = iter([1, 2])

        def connect():
            n = next(calls, None)
            if n is None:
                stop.set()
                raise ConnectionError("done")
            events.append(f"connect{n}")
            if n > 1:
                reconnected.set()
            return FakeServer()

        class SlowListener:
            def __init__(self, server, on_message):
                self.n = len([e for e in events if e.startswith("connect")])
                self.ready, self.opened = threading.Event(), True
                self.ready.set()

            def run(self):
                reconnected.wait(timeout=1)
                events.append(f"close{self.n}")

        trig.listen_forever(pending, connect=connect, listener_cls=SlowListener, stop=stop)
        assert events == ["connect1", "close1", "connect2", "close2"]

    def test_retries_when_plex_is_down(self, trig, monkeypatch, pending):
        events = self._harness(trig, monkeypatch, ["refused", "refused", "ok"], pending)
        assert [e for e in events if e[0] == "connect"] == [("connect", "refused"), ("connect", "refused"), ("connect", "ok")]
        assert ("socket-open",) in events

    def test_failed_catch_up_keeps_the_live_socket(self, trig, monkeypatch, pending):
        def broken(server, pending, now=None):
            raise RuntimeError("search failed")

        monkeypatch.setattr(trig, "catch_up", broken)
        events = self._harness(trig, monkeypatch, ["ok"], pending)
        # one session, one socket: the sweep failure did not start a second listener
        assert events == [("connect", "ok"), ("socket-open",), ("socket-closed",)]

    def test_no_sweep_when_the_socket_never_opens(self, trig, monkeypatch, pending):
        monkeypatch.setattr(trig, "RECONNECT_MIN_SECONDS", 0)
        stop, sweeps = threading.Event(), []
        monkeypatch.setattr(trig, "catch_up", lambda *a, **k: sweeps.append(1))
        calls = iter([1])

        def connect():
            if next(calls, None) is None:
                stop.set()
                raise ConnectionError("done")
            return FakeServer()

        class Refused:
            def __init__(self, server, on_message):
                self.ready, self.opened = threading.Event(), False

            def run(self):
                self.ready.set()  # handshake failed

        trig.listen_forever(pending, connect=connect, listener_cls=Refused, stop=stop)
        assert sweeps == [] and pending.horizon is None

    def test_hung_handshake_is_abandoned_and_retried(self, trig, monkeypatch, pending):
        monkeypatch.setattr(trig, "RECONNECT_MIN_SECONDS", 0)
        monkeypatch.setattr(trig, "OPEN_TIMEOUT_SECONDS", 0.05)
        stop, connects, hung = threading.Event(), [], []
        monkeypatch.setattr(trig, "catch_up", lambda *a, **k: None)

        def connect():
            connects.append(1)
            if len(connects) > 2:
                stop.set()
                raise ConnectionError("done")
            return FakeServer()

        class Hangs(trig.Listener):
            def run(self):
                hung.append(self)
                stop.wait(timeout=5)  # the handshake never completes

        loop = threading.Thread(target=trig.listen_forever, args=(pending, connect, Hangs, stop), daemon=True)
        loop.start()
        loop.join(timeout=5)
        stop.set()
        assert len(connects) == 3  # it did not wait forever on the first socket
        assert all(listener.abandoned for listener in hung)

    def test_abandoned_listener_delivers_nothing(self, trig):
        got = []
        listener = trig.Listener(FakeServer(), got.append)
        hung = []
        listener.ws = SimpleNamespace(close=lambda: hung.append(1))
        listener.abandon()
        assert hung == [1]  # the hung socket is closed, not leaked
        listener._message(None, json.dumps({"NotificationContainer": timeline(entry(1))}))
        closed = []
        listener._open(SimpleNamespace(close=lambda: closed.append(1)))
        assert got == [] and closed == [1] and not listener.opened

    def test_backoff_doubles_and_caps(self, trig, monkeypatch, pending):
        monkeypatch.setattr(trig, "RECONNECT_MAX_SECONDS", 20)
        waits = []
        stop = threading.Event()
        monkeypatch.setattr(stop, "wait", lambda d: waits.append(d) or len(waits) >= 5 and stop.set())

        def connect():
            raise ConnectionRefusedError("plex down")

        monkeypatch.setattr(trig, "RECONNECT_MIN_SECONDS", 5)
        trig.listen_forever(pending, connect=connect, stop=stop)
        assert waits == [5, 10, 20, 20, 20]


# ── the queue and the runner ─────────────────────────────────────────────────


class TestQueue:
    def test_nothing_due_inside_debounce(self, trig, pending):
        admit(pending, 10)
        assert pending.take_due() == {}

    def test_due_after_debounce_moves_to_inflight(self, trig, pending):
        admit(pending, 10)
        pending.due_at = time.time() - 1
        assert pending.take_due() == {"Movies": {"10": 0}}
        saved = json.loads(Path(pending.path).read_text())
        assert saved["items"] == {} and saved["inflight"] == {"Movies": {"10": 0}}
        pending.settle()
        assert json.loads(Path(pending.path).read_text())["inflight"] == {}

    def test_crash_mid_run_requeues_the_batch_on_startup(self, trig, pending):
        admit(pending, 10)
        pending.due_at = time.time() - 1
        pending.take_due()  # ...and the container is killed before settle()
        recovered = trig.Pending(pending.path)
        assert recovered.items == {"Movies": {"10": 0}}
        assert recovered.inflight == {}

    def test_requeue_does_not_pull_a_later_deadline_forward(self, trig, pending):
        pending.due_at = time.time() + 10 * trig.RETRY_SECONDS
        later = pending.due_at
        pending.requeue("Movies", "10", 1)
        assert pending.due_at == later

    def test_new_arrival_does_not_cut_a_retry_backoff_short(self, trig, pending):
        pending.requeue("Movies", "10", 1)
        retry_at = pending.due_at
        admit(pending, 11)
        assert pending.due_at >= retry_at

    def test_wait_due_wakes_on_add(self, trig, pending, monkeypatch):
        monkeypatch.setattr(trig, "DEBOUNCE_SECONDS", 0)
        result = {}
        thread = threading.Thread(target=lambda: result.update(pending.wait_due()), daemon=True)
        thread.start()
        admit(pending, 10)
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert result == {"Movies": {"10": 0}}

    def test_recovered_queue_is_due_promptly(self, trig, tmp_path, monkeypatch):
        path = tmp_path / "queue.json"
        path.write_text(json.dumps({"items": {"Movies": {"10": 2}, "Empty": {}}}))
        recovered = trig.Pending(str(path))
        assert recovered.items == {"Movies": {"10": 2}}
        assert recovered.due_at <= time.time() + trig.DEBOUNCE_SECONDS

    @pytest.mark.parametrize("content", ["{not json", "", "[1, 2]", '{"items": {"Movies": ["10"]}}', '{"items": {"Movies": {"10": "x"}}}', '{"items": {"M": {"1": 1e999}}}'])
    def test_bad_queue_file_starts_empty_and_says_so(self, trig, tmp_path, caplog, content):
        path = tmp_path / "queue.json"
        path.write_text(content)
        with caplog.at_level("ERROR"):
            loaded = trig.Pending(str(path))
        assert loaded.items == {} and loaded.seen == {} and loaded.horizon is None
        assert "starting with an empty queue" in caplog.text

    def test_missing_queue_file_is_a_quiet_first_start(self, trig, tmp_path, caplog):
        with caplog.at_level("WARNING"):
            assert trig.Pending(str(tmp_path / "none.json")).items == {}
        assert caplog.text == ""

    def test_live_queue_file_from_the_old_trigger_loads(self, trig, tmp_path):
        path = tmp_path / "queue.json"
        path.write_text('{"items": {}}')  # what the webhook trigger left on the host
        assert trig.Pending(str(path)).items == {}


class TestProcessDue:
    def test_runs_matched_items_and_dedupes_targets(self, trig, pending):
        server = FakeServer(items=[movie(10), movie(11), SimpleNamespace(type="show", ratingKey=500, guid="plex://show/x")])
        runs = []
        due = {"Movies": {"10": 0, "11": 0}, "TV Shows": {"500": 0}}
        trig.process_due(pending, due, connect=lambda: server, run=lambda lib, keys: runs.append((lib, keys)) or True)
        assert runs == [("Movies", ["10", "11"]), ("TV Shows", ["500"])]

    def test_unmatched_item_is_retried_then_forgotten(self, trig, pending, monkeypatch):
        monkeypatch.setattr(trig, "MAX_ATTEMPTS", 3)
        server = FakeServer(items=[movie(10, guid="local://10")])
        admit(pending, 10)
        pending.items.clear()
        runs = []
        trig.process_due(pending, {"Movies": {"10": 0}}, connect=lambda: server, run=lambda *a: runs.append(a) or True)
        assert pending.items == {"Movies": {"10": 1}}
        assert pending.due_at >= time.time() + trig.RETRY_SECONDS - 5
        pending.items.clear()
        trig.process_due(pending, {"Movies": {"10": 2}}, connect=lambda: server, run=lambda *a: runs.append(a) or True)
        assert pending.items == {}
        assert "10" not in pending.seen  # a later match event may queue it again
        assert runs == []

    def test_vanished_item_is_dropped(self, trig, pending):
        runs = []
        trig.process_due(pending, {"Movies": {"99": 0}}, connect=lambda: FakeServer(), run=lambda *a: runs.append(a) or True)
        assert runs == [] and pending.items == {}

    def test_plex_unreachable_holds_the_batch(self, trig, pending):
        def down():
            raise ConnectionRefusedError("plex down")

        trig.process_due(pending, {"Movies": {"10": 1}}, connect=down, run=lambda *a: pytest.fail("ran"))
        assert pending.items == {"Movies": {"10": 1}}

    def test_lookup_error_requeues_that_item(self, trig, pending):
        server = FakeServer(items=[movie(11)])

        def flaky(key):
            if key == 10:
                raise ConnectionResetError("reset")
            return server.items[key]

        server.fetchItem = flaky
        runs = []
        trig.process_due(pending, {"Movies": {"10": 0, "11": 0}}, connect=lambda: server, run=lambda lib, keys: runs.append(keys) or True)
        assert runs == [["11"]]
        assert pending.items == {"Movies": {"10": 0}}

    def test_failed_run_is_retried_then_given_up(self, trig, pending, monkeypatch):
        monkeypatch.setattr(trig, "MAX_ATTEMPTS", 3)
        server = FakeServer(items=[movie(10)])
        admit(pending, 10)
        pending.items.clear()
        trig.process_due(pending, {"Movies": {"10": 0}}, connect=lambda: server, run=lambda *a: False)
        assert pending.items == {"Movies": {"10": 1}}
        assert pending.due_at >= time.time() + trig.RETRY_SECONDS - 5
        pending.items.clear()
        trig.process_due(pending, {"Movies": {"10": 2}}, connect=lambda: server, run=lambda *a: False)
        assert pending.items == {} and "10" not in pending.seen

    def test_batch_is_settled_after_processing(self, trig, pending):
        admit(pending, 10)
        pending.due_at = time.time() - 1
        due = pending.take_due()
        trig.process_due(pending, due, connect=lambda: FakeServer(items=[movie(10)]), run=lambda *a: True)
        assert pending.inflight == {} and pending.items == {}


class TestWorker:
    def test_unexpected_error_requeues_the_batch(self, trig, pending, monkeypatch):
        admit(pending, 10)
        pending.due_at = time.time() - 1

        def boom(*a):
            raise RuntimeError("bug")

        monkeypatch.setattr(trig, "process_due", boom)
        trig.worker_step(pending)  # must not raise: it is the only thread that runs Kometa
        assert pending.items == {"Movies": {"10": 1}} and pending.inflight == {}

    def test_a_batch_that_keeps_crashing_is_given_up(self, trig, pending, monkeypatch):
        monkeypatch.setattr(trig, "MAX_ATTEMPTS", 3)
        admit(pending, 10)
        admit(pending, 11)
        pending.items["Movies"]["10"] = 2
        pending.due_at = time.time() - 1
        pending.take_due()
        pending.restore_inflight()
        assert pending.items == {"Movies": {"11": 1}}  # 10 hit the cap; 11 retries
        assert "10" not in pending.seen and "11" in pending.seen


class TestRunKometa:
    def test_command_is_item_scoped(self, trig, monkeypatch):
        calls = []
        monkeypatch.setattr(trig, "run_command", lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0, stdout="", stderr=""))
        assert trig.run_kometa("Movies", ["10", "11"])
        cmd = calls[0]
        assert cmd[cmd.index("--run-items") + 1] == "10|11"
        assert "--operations-only" in cmd and "--read-only-config" in cmd
        assert cmd[cmd.index("--run-libraries") + 1] == "Movies"

    @pytest.mark.parametrize("stream", ["stdout", "stderr"])
    def test_run_on_no_library_is_an_error_not_a_retry(self, trig, monkeypatch, caplog, stream):
        # Live 11-12: Kometa exited 0 having matched no library; that was logged as finished.
        out = {"stdout": "", "stderr": "", stream: "[CRITICAL] | Config Error: No libraries were found in config"}
        monkeypatch.setattr(trig, "run_command", lambda cmd, **kw: SimpleNamespace(returncode=0, **out))
        assert trig.run_kometa("Involuntary Film Club", ["10"])  # settled: retrying cannot help
        assert any(r.levelname == "ERROR" for r in caplog.records)
        assert not any("finished operations" in r.getMessage() for r in caplog.records)

    def test_unstartable_kometa_returns_false(self, trig, monkeypatch):
        def missing(cmd, **kw):
            raise FileNotFoundError("python")

        monkeypatch.setattr(trig, "run_command", missing)
        assert not trig.run_kometa("Movies", ["10"])

    def test_failure_and_timeout_return_false(self, trig, monkeypatch):
        monkeypatch.setattr(trig, "run_command", lambda cmd, **kw: SimpleNamespace(returncode=1, stderr="boom"))
        assert not trig.run_kometa("Movies", ["10"])

        def timeout(cmd, **kw):
            raise trig.subprocess.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(trig, "run_command", timeout)
        assert not trig.run_kometa("Movies", ["10"])


class TestRunItemsGuard:
    def test_refuses_kometa_without_run_items(self, trig, tmp_path, monkeypatch):
        fake = tmp_path / "kometa.py"
        fake.write_text("parser.add_argument('--run')\n")
        monkeypatch.setattr(trig, "KOMETA_PY", str(fake))
        with pytest.raises(SystemExit):
            trig.assert_run_items_supported()
        fake.write_text('arguments = {"run-items": ...}\n')
        trig.assert_run_items_supported()


def test_the_dead_webhook_receiver_is_gone():
    source = TRIGGER_PATH.read_text(encoding="utf-8")
    for gone in ('library.new"', "ThreadingHTTPServer", "parse_multipart_payload", "LISTEN_PORT", "9596"):
        assert gone not in source, gone


# ── #5634 S3: one Kometa run at a time, queued collection runs, the arrival outbox ──


def _db(trig, tmp_path):
    from modules import live_record

    path = str(tmp_path / "live-collections.db")
    live_record.connect(path).close()
    return live_record, path


class TestRunLock:
    def test_a_busy_run_waits_for_the_lock_and_runs_again_without_an_attempt(self, trig, pending, monkeypatch):
        server = FakeServer(items=[movie(10)])
        results = [trig.RUN_BUSY, True]
        runs, waits = [], []
        monkeypatch.setattr(trig, "wait_for_run_lock", lambda: waits.append(1) or True)
        trig.process_due(pending, {"Movies": {"10": 0}}, connect=lambda: server, run=lambda lib, keys: runs.append(keys) or results.pop(0))
        assert runs == [["10"], ["10"]] and waits == [1]
        assert pending.items == {}

    def test_busy_while_nobody_holds_the_lock_is_a_failure_eventually(self, trig):
        runs = []
        assert trig.run_unless_busy(lambda: runs.append(1) or trig.RUN_BUSY, wait=lambda: False) is False
        assert len(runs) == trig.MAX_SPURIOUS_BUSY

    def test_wait_returns_at_once_when_nobody_holds_the_lock(self, trig, tmp_path):
        assert trig.wait_for_run_lock(str(tmp_path / "lock")) is False

    def test_wait_blocks_until_the_holder_releases(self, trig, tmp_path):
        import fcntl

        lock = str(tmp_path / "lock")
        holder = open(lock, "a+")
        fcntl.flock(holder, fcntl.LOCK_EX)
        result = []
        waiter = threading.Thread(target=lambda: result.append(trig.wait_for_run_lock(lock)))
        waiter.start()
        waiter.join(0.3)
        assert waiter.is_alive() and result == []
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
        waiter.join(10)
        assert result == [True]

    def test_exit_75_is_busy_not_failure(self, trig, monkeypatch):
        monkeypatch.setattr(trig, "run_command", lambda cmd, **kw: SimpleNamespace(returncode=trig.EXIT_LOCK_BUSY, stdout="", stderr=""))
        assert trig.run_kometa("Movies", ["10"]) is trig.RUN_BUSY
        assert trig.run_build("Movies", "Picks") is trig.RUN_BUSY

    def test_kometa_and_the_trigger_agree_on_the_busy_exit_code(self, trig):
        source = (Path(__file__).resolve().parent.parent / "kometa.py").read_text(encoding="utf-8")
        assert f"RUN_LOCK_BUSY = {trig.EXIT_LOCK_BUSY}" in source


class TestBuilds:
    def test_command_is_one_collection_in_one_library(self, trig, monkeypatch):
        calls = []
        monkeypatch.setattr(trig, "run_command", lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0, stdout="", stderr=""))
        assert trig.run_build("movies-key", "Gremlins Top 100") is True
        cmd = calls[0]
        assert cmd[cmd.index("--run-libraries") + 1] == "movies-key"
        assert cmd[cmd.index("--run-collections") + 1] == "Gremlins Top 100"
        assert "--collections-only" in cmd and "--read-only-config" in cmd and "--ignore-schedules" in cmd

    def test_a_built_collection_leaves_the_queue(self, trig, tmp_path):
        live_record, path = _db(trig, tmp_path)
        conn = live_record.connect(path)
        live_record.queue_build(conn, "movies-key", "Picks", "picks")
        runs = []
        trig.run_builds(path, run=lambda lib, name: runs.append((lib, name)) or True)
        assert runs == [("movies-key", "Picks")]
        assert live_record.pending_builds(conn) == []
        conn.close()

    def test_a_failed_build_is_retried_then_left_to_the_sweep(self, trig, tmp_path, monkeypatch):
        monkeypatch.setattr(trig, "BUILD_MAX_ATTEMPTS", 2)
        live_record, path = _db(trig, tmp_path)
        conn = live_record.connect(path)
        live_record.queue_build(conn, "movies-key", "Picks", "picks")
        trig.run_builds(path, run=lambda *a: False)
        assert [b["attempts"] for b in live_record.pending_builds(conn)] == [1]
        trig.run_builds(path, run=lambda *a: False)
        assert live_record.pending_builds(conn) == []
        conn.close()

    def test_an_unopenable_record_never_raises(self, trig, tmp_path):
        trig.run_builds(str(tmp_path / "no" / "such" / "dir" / "db"), run=lambda *a: pytest.fail("ran"))

    def test_the_after_step_runs_after_every_batch_even_a_failed_one(self, trig, pending, monkeypatch):
        calls = []
        monkeypatch.setattr(pending, "wait_due", lambda: {"Movies": {"10": 0}})

        def boom(*a):
            raise RuntimeError("batch broke")

        trig.worker_step(pending, connect=boom, after=lambda: calls.append(1))
        assert calls == [1]


CONTRACT_FIXTURES = Path(__file__).parent / "fixtures" / "kometa_contract"


def _contract_errors(payload):
    """What the kometa-contract item-arrived schema would reject in an event with no collections."""
    wire = json.loads(json.dumps(payload))
    fixture = json.loads((CONTRACT_FIXTURES / "item-arrived-plain.json").read_text())
    errors = []
    if set(wire) != set(fixture):
        errors.append(f"keys {sorted(set(wire) ^ set(fixture))}")
    for key in ("added_at", "arrived_at"):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", str(wire.get(key))):
            errors.append(f"{key} {wire.get(key)!r}")
    if not re.fullmatch(r"\d+", str(wire.get("rating_key"))):
        errors.append("rating_key")
    if not any(wire.get(k) is not None for k in ("tmdb_id", "tvdb_id", "imdb_id")):
        errors.append("no id")
    return errors


def matched(key, added=NOW - 60, guids=("tmdb://511", "imdb://tt0000511")):
    item = movie(key, added=added, guid="plex://movie/m")
    item.guids = [SimpleNamespace(id=g) for g in guids]
    item.librarySectionTitle = "Movies"
    item.year = 2011
    return item


class TestAnnounce:
    """Every arrival is announced once: held events go out when their item stops being retried, and
    an item Kometa never finished is announced from Plex alone."""

    def _outbox(self, live_record, path):
        conn = live_record.connect(path)
        try:
            return {r["rating_key"]: r["payload"] for r in live_record.unpublished_arrivals(conn)}
        finally:
            conn.close()

    def _hold(self, live_record, path, key):
        conn = live_record.connect(path)
        live_record.queue_arrival(conn, "Movies", key, {"v": 1, "rating_key": str(key), "collections": [{"name": "Top"}]}, complete=False)
        conn.close()

    def test_giving_up_with_no_event_announces_it_from_plex(self, trig, pending, tmp_path, monkeypatch):
        live_record, path = _db(trig, tmp_path)
        monkeypatch.setattr(trig, "RECORD_PATH", path)
        monkeypatch.setattr(trig, "MAX_ATTEMPTS", 1)
        server = FakeServer(items=[matched(10, added=time.time() + 5)])  # after the outbox began
        trig.process_due(pending, {"Movies": {"10": 0}}, connect=lambda: server, run=lambda *a: False)
        payload = self._outbox(live_record, path)[10]
        assert payload["collections"] == [] and payload["rating_key"] == "10" and payload["library"] == "Movies"
        assert (payload["media_type"], payload["tmdb_id"], payload["tvdb_id"], payload["imdb_id"], payload["year"]) == ("movie", 511, None, "tt0000511", 2011)
        assert payload["added_at"].endswith("Z") and payload["arrived_at"].endswith("Z")
        assert _contract_errors(payload) == []

    def test_giving_up_releases_a_held_event_instead(self, trig, pending, tmp_path, monkeypatch):
        live_record, path = _db(trig, tmp_path)
        monkeypatch.setattr(trig, "RECORD_PATH", path)
        monkeypatch.setattr(trig, "MAX_ATTEMPTS", 1)
        self._hold(live_record, path, 10)
        trig.process_due(pending, {"Movies": {"10": 0}}, connect=lambda: FakeServer(items=[matched(10)]), run=lambda *a: False)
        assert self._outbox(live_record, path)[10]["collections"] == [{"name": "Top"}]

    def test_an_item_with_no_id_cannot_be_announced_and_says_so(self, trig, pending, tmp_path, monkeypatch, caplog):
        live_record, path = _db(trig, tmp_path)
        monkeypatch.setattr(trig, "RECORD_PATH", path)
        monkeypatch.setattr(trig, "MAX_ATTEMPTS", 1)
        server = FakeServer(items=[matched(10, guids=())])
        server.items[10].guid = "local://10"
        trig.process_due(pending, {"Movies": {"10": 0}}, connect=lambda: server, run=lambda *a: pytest.fail("ran"))
        assert self._outbox(live_record, path) == {}
        assert any(r.levelname == "ERROR" and "cannot announce 10" in r.getMessage() for r in caplog.records)

    def test_an_item_plex_added_before_the_outbox_existed_is_not_announced(self, trig, pending, tmp_path, monkeypatch):
        live_record, path = _db(trig, tmp_path)
        monkeypatch.setattr(trig, "RECORD_PATH", path)
        monkeypatch.setattr(trig, "MAX_ATTEMPTS", 1)
        server = FakeServer(items=[matched(10, added=time.time() - 86400)])
        trig.process_due(pending, {"Movies": {"10": 0}}, connect=lambda: server, run=lambda *a: False)
        assert self._outbox(live_record, path) == {}

    def test_a_successful_rerun_releases_what_an_earlier_failure_held(self, trig, pending, tmp_path, monkeypatch):
        live_record, path = _db(trig, tmp_path)
        monkeypatch.setattr(trig, "RECORD_PATH", path)
        self._hold(live_record, path, 10)
        trig.process_due(pending, {"Movies": {"10": 1}}, connect=lambda: FakeServer(items=[matched(10)]), run=lambda *a: True)
        assert list(self._outbox(live_record, path)) == [10]

    def test_an_item_that_left_plex_releases_its_held_event(self, trig, pending, tmp_path, monkeypatch):
        live_record, path = _db(trig, tmp_path)
        monkeypatch.setattr(trig, "RECORD_PATH", path)
        self._hold(live_record, path, 10)
        trig.process_due(pending, {"Movies": {"10": 1}}, connect=lambda: FakeServer(), run=lambda *a: pytest.fail("ran"))
        assert list(self._outbox(live_record, path)) == [10]


class TestBuilder:
    def test_builds_run_at_start_and_on_every_wake_on_their_own_thread(self, trig):
        woken, stop, runs = threading.Event(), threading.Event(), []
        ran = threading.Semaphore(0)

        def run_all():
            runs.append(threading.current_thread().name)
            if len(runs) == 2:
                stop.set()
            ran.release()

        thread = threading.Thread(target=trig.builder, args=(woken, stop, run_all), name="collection-builder")
        thread.start()
        assert ran.acquire(timeout=10)  # at start: what a crash left queued
        assert not ran.acquire(timeout=0.2)  # then nothing until a batch wakes it
        woken.set()
        assert ran.acquire(timeout=10)
        thread.join(10)
        assert runs == ["collection-builder", "collection-builder"] and not thread.is_alive()

    def test_a_failing_build_pass_does_not_kill_the_thread(self, trig):
        woken, stop, calls = threading.Event(), threading.Event(), []

        def run_all():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            stop.set()

        thread = threading.Thread(target=trig.builder, args=(woken, stop, run_all))
        thread.start()
        woken.set()
        thread.join(10)
        assert calls == [1, 1]


# Kometa's shape: the parent runs the work in a ProcessPoolExecutor child, and the CHILD holds the
# run lock. Killing only the parent would leave the lock held by an orphan.
FAKE_KOMETA = """
import fcntl, os, signal, sys, time
if os.fork() == 0:
    if os.environ.get("FAKE_KOMETA_IGNORE_TERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    fh = open(os.environ["FAKE_KOMETA_LOCK"], "a+")
    fcntl.flock(fh, fcntl.LOCK_EX)
    with open(os.environ["FAKE_KOMETA_READY"], "w") as ready:
        ready.write("locked\\n")
    time.sleep(600)
    os._exit(0)
os.wait()
"""


def _lock_is_free(path):
    import fcntl

    with open(path, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fh, fcntl.LOCK_UN)
        return True


class TestArrivalsFirst:
    """A queued collection run never holds the run lock while an arrival is due (#5634 S3, B1)."""

    def _fake_kometa(self, trig, tmp_path, monkeypatch):
        lock, ready, script = tmp_path / "run.lock", tmp_path / "ready", tmp_path / "kometa.py"
        os.mkfifo(ready)
        script.write_text(FAKE_KOMETA)
        monkeypatch.setattr(trig, "KOMETA_PY", str(script))
        monkeypatch.setattr(trig, "RUN_LOCK_PATH", str(lock))
        monkeypatch.setenv("FAKE_KOMETA_LOCK", str(lock))
        monkeypatch.setenv("FAKE_KOMETA_READY", str(ready))
        return lock, ready

    def _await_locked(self, ready):
        """Blocks on the FIFO until the fake Kometa's child holds the lock: no sleeps."""
        got = []
        reader = threading.Thread(target=lambda: got.append(open(ready).readline()), daemon=True)
        reader.start()
        reader.join(60)
        if reader.is_alive():
            open(ready, "w").close()  # unblock the reader before failing
            pytest.fail("the fake build never took the run lock")
        assert got == ["locked\n"]

    def test_an_arrival_stops_a_running_build_and_is_published_within_seconds(self, trig, pending, tmp_path, monkeypatch):
        import asyncio
        import fcntl

        live_record, path = _db(trig, tmp_path)
        monkeypatch.setattr(trig, "RECORD_PATH", path)
        lock, ready = self._fake_kometa(trig, tmp_path, monkeypatch)
        gate = trig.BuildGate()
        monkeypatch.setattr(trig, "GATE", gate)
        conn = live_record.connect(path)
        live_record.queue_build(conn, "Movies", "Picks", "collections/picks.yml")
        conn.close()

        gate.worker_idle()
        builder = threading.Thread(target=trig.run_builds, args=(path,), daemon=True)
        builder.start()
        self._await_locked(ready)  # a 600 s build now holds the run lock

        ran = []

        def item_run(library, keys):
            # What Kometa does: exit 75 while the lock is held, else add + queue the arrival.
            with open(lock, "a+") as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return trig.RUN_BUSY
                conn = live_record.connect(path)
                live_record.queue_arrival(conn, "Movies", 10, {"v": 1, "rating_key": "10", "collections": []})
                conn.close()
                ran.append(keys)
                return True

        pending.requeue("Movies", "10", 0)
        pending.due_at = 0
        started = time.monotonic()
        step = threading.Thread(target=trig.worker_step, args=(pending,), kwargs={"connect": lambda: FakeServer(items=[matched(10)]), "run": item_run, "gate": gate}, daemon=True)
        step.start()
        step.join(60)
        assert not step.is_alive(), "the arrival is still waiting on the build's run lock"
        js = FakeJetStream()
        assert asyncio.run(trig.Publisher(path, "nats://x:4222", "seed").drain(js)) == trig.DRAIN_EMPTY
        elapsed = time.monotonic() - started

        assert ran == [["10"]] and [p["rating_key"] for _, p, _ in js.published] == ["10"]
        assert elapsed < 30, elapsed  # the build would have held the lock for 600 s
        builder.join(30)
        assert not builder.is_alive()
        conn = live_record.connect(path)
        # Stopped, not failed: still queued, no attempt charged, runs once the worker is idle again.
        assert [(b["mapping_name"], b["attempts"]) for b in live_record.pending_builds(conn)] == [("Picks", 0)]
        conn.close()
        assert _lock_is_free(lock)  # the lock-holding child died with the group

    def test_a_build_never_starts_while_the_worker_has_a_batch(self, trig, tmp_path, monkeypatch):
        live_record, path = _db(trig, tmp_path)
        gate = trig.BuildGate()
        monkeypatch.setattr(trig, "GATE", gate)
        monkeypatch.setattr(trig, "_spawn", lambda cmd: pytest.fail("a build started during a batch"))
        conn = live_record.connect(path)
        live_record.queue_build(conn, "Movies", "Picks", "collections/picks.yml")
        gate.worker_busy()
        trig.run_builds(path)
        assert [(b["mapping_name"], b["attempts"]) for b in live_record.pending_builds(conn)] == [("Picks", 0)]
        conn.close()

    def test_the_builder_wakes_whenever_the_worker_goes_idle(self, trig, pending, monkeypatch):
        gate = trig.BuildGate()
        pending.requeue("Movies", "10", 0)
        pending.due_at = 0
        trig.worker_step(pending, connect=lambda: FakeServer(items=[]), run=lambda *a: True, gate=gate)
        assert gate.woken.is_set()

    def test_a_timeout_kills_the_child_holding_the_lock_too(self, trig, tmp_path, monkeypatch):
        lock, ready = self._fake_kometa(trig, tmp_path, monkeypatch)
        real_spawn = trig._spawn

        def spawn_then_wait_for_the_lock(command):
            proc = real_spawn(command)
            self._await_locked(ready)  # the timeout below fires with the child holding the lock
            return proc

        monkeypatch.setattr(trig, "_spawn", spawn_then_wait_for_the_lock)
        monkeypatch.setattr(trig, "RUN_TIMEOUT_SECONDS", 0)
        assert trig.run_kometa("Movies", ["10"]) is False
        assert _lock_is_free(lock)

    def test_a_stopped_build_that_ignores_sigterm_is_killed_after_the_grace(self, trig, tmp_path, monkeypatch):
        lock, ready = self._fake_kometa(trig, tmp_path, monkeypatch)
        monkeypatch.setenv("FAKE_KOMETA_IGNORE_TERM", "1")
        monkeypatch.setattr(trig, "STOP_GRACE_SECONDS", 1)
        real_spawn = trig._spawn
        spawned = []
        monkeypatch.setattr(trig, "_spawn", lambda cmd: spawned.append(real_spawn(cmd)) or spawned[-1])
        gate = trig.BuildGate()
        gate.worker_idle()
        result = []
        build = threading.Thread(target=lambda: result.append(trig.run_command([sys.executable, trig.KOMETA_PY], gate=gate)), daemon=True)
        build.start()
        try:
            self._await_locked(ready)
            gate.worker_busy()
            build.join(30)
            assert not build.is_alive(), "a build that ignores SIGTERM still holds the run lock"
            assert result and result[0] is trig.RUN_STOPPED
            assert _lock_is_free(lock)
        finally:
            for proc in spawned:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_a_wake_while_the_worker_is_busy_never_counts_as_a_stop(self, trig, tmp_path, monkeypatch):
        live_record, path = _db(trig, tmp_path)
        gate = trig.BuildGate()  # never idle: nothing can start
        monkeypatch.setattr(trig, "GATE", gate)
        spawned = []
        monkeypatch.setattr(trig, "_spawn", lambda cmd: spawned.append(cmd))
        conn = live_record.connect(path)
        live_record.queue_build(conn, "Movies", "Picks", "collections/picks.yml")
        for _ in range(trig.BUILD_MAX_STOPS + 2):
            trig.run_builds(path)
        assert spawned == [] and gate.stops(("Movies", "Picks")) == 0
        assert [(b["mapping_name"], b["attempts"]) for b in live_record.pending_builds(conn)] == [("Picks", 0)]
        conn.close()

    def test_stops_are_counted_and_never_charged(self, trig, tmp_path, monkeypatch):
        live_record, path = _db(trig, tmp_path)
        gate = trig.BuildGate()
        monkeypatch.setattr(trig, "GATE", gate)
        conn = live_record.connect(path)
        live_record.queue_build(conn, "Movies", "Picks", "collections/picks.yml")
        for _ in range(trig.BUILD_MAX_STOPS):
            trig.run_builds(path, run=lambda library, name: trig.RUN_STOPPED)
        assert gate.stops(("Movies", "Picks")) == trig.BUILD_MAX_STOPS
        assert [(b["mapping_name"], b["attempts"]) for b in live_record.pending_builds(conn)] == [("Picks", 0)]
        trig.run_builds(path, run=lambda library, name: True)
        assert gate.stops(("Movies", "Picks")) == 0 and live_record.pending_builds(conn) == []
        conn.close()

    def test_a_build_stopped_too_often_runs_to_completion(self, trig, tmp_path, monkeypatch):
        # Arrivals first, but a build longer than the gap between arrivals must still finish.
        live_record, path = _db(trig, tmp_path)
        monkeypatch.setattr(trig, "RECORD_PATH", path)
        gate = trig.BuildGate()
        monkeypatch.setattr(trig, "GATE", gate)
        conn = live_record.connect(path)
        live_record.queue_build(conn, "Movies", "Picks", "collections/picks.yml")
        for _ in range(trig.BUILD_MAX_STOPS):
            gate.note_stop(("Movies", "Picks"))
        release = tmp_path / "release"
        os.mkfifo(release)
        started = threading.Event()
        spawned = []

        def spawn(command):  # a build that runs until the test releases it
            spawned.append(subprocess.Popen([sys.executable, "-c", f"open({str(release)!r}).read()"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True))
            started.set()
            return spawned[-1]

        monkeypatch.setattr(trig, "_spawn", spawn)
        gate.worker_idle()
        builder = threading.Thread(target=trig.run_builds, args=(path,), daemon=True)
        builder.start()
        try:
            assert started.wait(30)
            gate.worker_busy()  # an arrival is due: a protected build is not stopped
            builder.join(1)  # a stopped build would end at once; this one is still waiting to be released
            assert builder.is_alive() and spawned[0].poll() is None, "a protected build was stopped"
            os.close(os.open(release, os.O_WRONLY | os.O_NONBLOCK))
            builder.join(30)
            assert not builder.is_alive()
            assert live_record.pending_builds(conn) == []  # it finished, and counts as built
            assert gate.stops(("Movies", "Picks")) == 0
        finally:
            conn.close()
            for proc in spawned:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_an_error_waiting_on_the_lock_is_a_failed_run_not_a_spin(self, trig):
        calls = []

        def wait():
            calls.append(1)
            raise PermissionError("lock dir unreadable")

        assert trig.run_unless_busy(lambda: trig.RUN_BUSY, wait=wait) is False
        assert calls == [1]


class FakeJetStream:
    def __init__(self, failures=()):
        self.failures = list(failures)
        self.published = []

    async def publish(self, subject, payload, timeout=None, stream=None, headers=None):
        if self.failures:
            raise self.failures.pop(0)
        self.published.append((subject, json.loads(payload), headers))


class StreamNotFound(Exception):
    """Stands in for nats.js.errors.NotFoundError / NoRespondersError: no stream captures the subject yet."""


class NoStreamResponseError(Exception):
    """Named like nats.js.errors.NoStreamResponseError: what nats-py raises publishing with no stream."""


class BadRequestError(Exception):
    """Named like nats.js.errors.BadRequestError: a publish error nobody is waiting out."""


class TestPublisher:
    def _queue(self, live_record, path, *keys):
        conn = live_record.connect(path)
        for key in keys:
            live_record.queue_arrival(conn, "Movies", key, {"v": 1, "rating_key": key, "collections": []})
        conn.close()

    def test_drain_publishes_with_a_dedupe_id_and_empties_the_outbox(self, trig, tmp_path):
        import asyncio

        live_record, path = _db(trig, tmp_path)
        self._queue(live_record, path, 11, 12)
        js = FakeJetStream()
        publisher = trig.Publisher(path, "nats://x:4222", "seed")
        assert asyncio.run(publisher.drain(js)) == trig.DRAIN_EMPTY
        assert [(s, p["rating_key"], h) for s, p, h in js.published] == [
            ("kometa.item.arrived", 11, {"Nats-Msg-Id": "arrived:Movies:11"}),
            ("kometa.item.arrived", 12, {"Nats-Msg-Id": "arrived:Movies:12"}),
        ]
        conn = live_record.connect(path)
        assert live_record.unpublished_arrivals(conn) == []
        conn.close()

    def test_a_failed_publish_keeps_the_event_queued_and_a_retry_sends_it(self, trig, tmp_path):
        import asyncio

        live_record, path = _db(trig, tmp_path)
        self._queue(live_record, path, 11)
        js = FakeJetStream(failures=[StreamNotFound("stream not found")])
        publisher = trig.Publisher(path, "nats://x:4222", "seed")
        assert asyncio.run(publisher.drain(js)) == trig.DRAIN_WAIT
        conn = live_record.connect(path)
        assert [r["rating_key"] for r in live_record.unpublished_arrivals(conn)] == [11]
        assert asyncio.run(publisher.drain(js)) == trig.DRAIN_EMPTY
        assert live_record.unpublished_arrivals(conn) == []
        conn.close()
        assert [p["rating_key"] for _, p, _ in js.published] == [11]

    def test_a_publish_timeout_is_retried_on_a_backoff(self, trig, tmp_path):
        import asyncio

        live_record, path = _db(trig, tmp_path)
        self._queue(live_record, path, 11)
        publisher = trig.Publisher(path, "nats://x:4222", "seed")
        for timeout in (TimeoutError(), asyncio.TimeoutError(), type("TimeoutError", (Exception,), {})()):
            assert asyncio.run(publisher.drain(FakeJetStream(failures=[timeout]))) == trig.DRAIN_RETRY

    def test_no_stream_yet_is_a_warning_and_a_misconfiguration_is_an_error(self, trig, tmp_path, caplog):
        import asyncio
        import logging

        live_record, path = _db(trig, tmp_path)
        self._queue(live_record, path, 11)
        publisher = trig.Publisher(path, "nats://x:4222", "seed")
        with caplog.at_level(logging.WARNING, logger=trig.log.name):
            assert asyncio.run(publisher.drain(FakeJetStream(failures=[NoStreamResponseError()]))) == trig.DRAIN_WAIT
            assert asyncio.run(publisher.drain(FakeJetStream(failures=[BadRequestError("bad request")]))) == trig.DRAIN_WAIT
        levels = [(r.levelno, r.getMessage().split(": ")[1]) for r in caplog.records if "held in the outbox" in r.getMessage()]
        assert levels == [(logging.WARNING, "NoStreamResponseError"), (logging.ERROR, "BadRequestError")]

    def test_a_publish_the_server_refuses_logs_error_not_a_quiet_retry(self, trig, tmp_path, caplog):
        # nats-py's real path: the server's -ERR goes to error_cb only, and the publish gets no ack.
        import asyncio

        import nats.errors

        live_record, path = _db(trig, tmp_path)
        conn = live_record.connect(path)
        live_record.queue_arrival(conn, "Movies", 10, {"v": 1, "rating_key": "10", "collections": []})
        conn.close()
        publisher = trig.Publisher(path, "nats://x:4222", "seed")
        with caplog.at_level(logging.WARNING):
            asyncio.run(publisher.drain(FakeJetStream(failures=[nats.errors.TimeoutError()])))
            asyncio.run(publisher._on_error(nats.errors.Error('nats: Permissions Violation for Publish to "kometa.item.arrived"')))
            assert asyncio.run(publisher.drain(FakeJetStream(failures=[nats.errors.TimeoutError()]))) == trig.DRAIN_RETRY
        acks = [r.levelno for r in caplog.records if "no ack from JetStream" in r.getMessage()]
        assert acks == [logging.WARNING, logging.ERROR]  # a plain timeout is not an alarm; a refusal is
        assert any(r.levelno == logging.ERROR and "Permissions Violation" in r.getMessage() for r in caplog.records)
        js = FakeJetStream()
        assert asyncio.run(publisher.drain(js)) == trig.DRAIN_EMPTY
        assert publisher._refused is None  # fixed permissions: back to quiet timeouts

    def test_a_refused_connect_logs_error(self, trig, tmp_path, caplog, monkeypatch):
        import asyncio

        import nats.errors

        monkeypatch.setattr(trig, "PUBLISH_RETRY_MIN_SECONDS", 0)
        stop = threading.Event()
        stop.set()

        async def refused():
            raise nats.errors.AuthorizationError()

        async def down():
            raise nats.errors.NoServersError()

        with caplog.at_level(logging.WARNING):
            asyncio.run(trig.Publisher(str(tmp_path / "r.db"), "nats://x:4222", "seed", connect=refused).main(stop))
            asyncio.run(trig.Publisher(str(tmp_path / "r.db"), "nats://x:4222", "seed", connect=down).main(stop))
        assert [(r.levelno, "AuthorizationError" in r.getMessage()) for r in caplog.records if "connect to" in r.getMessage()] == [(logging.ERROR, True), (logging.WARNING, False)]

    def test_a_rerun_that_changed_the_event_mid_send_is_not_sent_again(self, trig, tmp_path, caplog):
        # One Nats-Msg-Id per item: a second send would be dropped as a duplicate inside the window,
        # or announce the item twice after it. The ack is the announcement.
        import asyncio
        import logging

        live_record, path = _db(trig, tmp_path)
        self._queue(live_record, path, 11)

        class RerunMidSend(FakeJetStream):
            async def publish(self, subject, payload, **kw):
                await super().publish(subject, payload, **kw)
                conn = live_record.connect(path)
                live_record.queue_arrival(conn, "Movies", 11, {"v": 1, "rating_key": 11, "collections": [{"name": "Late"}]})
                conn.close()

        js = RerunMidSend()
        with caplog.at_level(logging.WARNING, logger=trig.log.name):
            assert asyncio.run(trig.Publisher(path, "nats://x:4222", "seed").drain(js)) == trig.DRAIN_EMPTY
        assert len(js.published) == 1
        assert any("announced once" in r.getMessage() for r in caplog.records)

    def test_no_stream_waits_for_a_wake_not_a_timer(self, trig, tmp_path, monkeypatch):
        import asyncio

        monkeypatch.setattr(trig, "PUBLISH_RETRY_MIN_SECONDS", 0.001)
        live_record, path = _db(trig, tmp_path)
        self._queue(live_record, path, 11)
        js = FakeJetStream(failures=[StreamNotFound("no stream")] * 50)

        async def close():
            pass

        async def connect():
            return SimpleNamespace(jetstream=lambda: js, close=close)

        publisher = trig.Publisher(path, "nats://x:4222", "seed", connect=connect)

        async def run():
            task = asyncio.ensure_future(publisher.main())
            await asyncio.sleep(0.3)  # a timer at 1 ms would have retried ~a hundred times by now
            assert len(js.failures) == 49
            publisher.wake()  # an arrival or a reconnect
            await asyncio.sleep(0.1)
            assert len(js.failures) == 48
            task.cancel()

        asyncio.run(run())

    def test_the_loop_retries_connect_and_publish_until_the_outbox_is_empty(self, trig, tmp_path, monkeypatch):
        import asyncio

        monkeypatch.setattr(trig, "PUBLISH_RETRY_MIN_SECONDS", 0.01)
        live_record, path = _db(trig, tmp_path)
        self._queue(live_record, path, 11)
        stop = threading.Event()
        js = FakeJetStream(failures=[StreamNotFound("no stream"), TimeoutError()])
        connects = []
        closed = []

        async def close():
            closed.append(1)

        async def connect():
            connects.append(1)
            if len(connects) == 1:
                raise ConnectionRefusedError("nats down")
            return SimpleNamespace(jetstream=lambda: js, close=close)

        publisher = trig.Publisher(path, "nats://x:4222", "seed", connect=connect)
        original = js.publish

        async def publish_then_stop(*a, **kw):
            try:
                await original(*a, **kw)
            except StreamNotFound:
                # Held until a wake: the reconnect callback stands in for plex-bot creating the stream.
                asyncio.get_running_loop().call_later(0.05, lambda: asyncio.ensure_future(publisher._on_reconnect()))
                raise
            stop.set()
            publisher.wake()

        js.publish = publish_then_stop
        asyncio.run(asyncio.wait_for(publisher.main(stop), 10))
        assert len(connects) == 2
        assert [p["rating_key"] for _, p, _ in js.published] == [11]
        assert closed == [1]  # main() leaving closes its connection, so a restart never leaks one

    def test_a_connect_error_never_logs_its_text(self, trig, tmp_path, monkeypatch, caplog):
        import asyncio

        monkeypatch.setattr(trig, "PUBLISH_RETRY_MIN_SECONDS", 0.01)
        stop = threading.Event()

        async def connect():
            stop.set()
            raise ValueError("SUAMSEEDSEEDSEED")

        publisher = trig.Publisher(str(tmp_path / "db"), "nats://x:4222", "SUAMSEEDSEEDSEED", connect=connect)
        asyncio.run(asyncio.wait_for(publisher.main(stop), 10))
        assert "SUAMSEEDSEEDSEED" not in caplog.text
        assert "ValueError" in caplog.text

    def test_a_held_arrival_is_not_published_until_released(self, trig, tmp_path, monkeypatch):
        import asyncio

        live_record, path = _db(trig, tmp_path)
        monkeypatch.setattr(trig, "RECORD_PATH", path)
        conn = live_record.connect(path)
        live_record.queue_arrival(conn, "Movies", 11, {"v": 1, "rating_key": "11", "collections": []}, complete=False)
        conn.close()
        js = FakeJetStream()
        publisher = trig.Publisher(path, "nats://x:4222", "seed")
        assert asyncio.run(publisher.drain(js)) == trig.DRAIN_EMPTY and js.published == []
        # The trigger runs out of retries for the item: what its last run managed goes out.
        trig.announce_unfinished(None, "11")
        assert asyncio.run(publisher.drain(js)) == trig.DRAIN_EMPTY
        assert [h["Nats-Msg-Id"] for _, _, h in js.published] == ["arrived:Movies:11"]

    def test_no_seed_keeps_the_outbox_and_says_so(self, trig, monkeypatch, caplog):
        monkeypatch.delenv("NATS_NKEY_SEED", raising=False)
        assert trig.start_publisher() is None
        assert any(r.levelname == "ERROR" and "NATS_NKEY_SEED" in r.getMessage() for r in caplog.records)

    def test_the_seed_leaves_the_environment_before_any_kometa_run(self, trig, monkeypatch, caplog):
        started = threading.Event()
        monkeypatch.setenv("NATS_NKEY_SEED", "SUAMSEEDSEEDSEED")
        monkeypatch.setattr(trig.Publisher, "run_forever", lambda self: started.set())
        publisher = trig.start_publisher()
        assert publisher is not None and "NATS_NKEY_SEED" not in os.environ
        assert "SUAMSEEDSEEDSEED" not in caplog.text
        assert started.wait(10)

    def test_wake_before_the_loop_starts_is_harmless(self, trig, tmp_path):
        trig.Publisher(str(tmp_path / "db"), "nats://x:4222", "seed").wake()


WATCH_URL = "https://letterboxd.com/someone/list/top/"


def _watched(trig, tmp_path, urls=(WATCH_URL,), name="Top 100"):
    from tests.test_live_record import snapshot

    live_record, path = _db(trig, tmp_path)
    conn = live_record.connect(path)
    live_record.write_collection(conn, snapshot(name, watch_urls=list(urls)))
    conn.close()
    return live_record, path


def _event(url=WATCH_URL, content_hash="abc123def456"):
    return json.dumps({"type": "watchedlist.updated", "url": url, "hash": content_hash, "items": [], "changed_at": "2026-11-12T12:00:00.000Z"}).encode()


class FakeMsg:
    def __init__(self, data, delivered=1):
        self.data = data
        self.acked = self.termed = False
        self.metadata = SimpleNamespace(num_delivered=delivered)

    async def ack(self):
        self.acked = True

    async def term(self):
        self.termed = True


class FakeSub:
    def __init__(self, batches):
        self.batches = list(batches)

    async def fetch(self, batch=1, timeout=None):
        item = self.batches.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class TestWatchedLists:
    def test_an_event_queues_a_scoped_run_of_the_collection_and_a_redelivery_does_not(self, trig, tmp_path):
        live_record, path = _watched(trig, tmp_path)
        assert trig.queue_watched_list(_event(), path) == trig.WATCH_QUEUED
        conn = live_record.connect(path)
        assert [(b["library"], b["mapping_name"]) for b in live_record.pending_builds(conn)] == [("Movies", "Top 100")]
        # The builder runs it: exactly one scoped run, through the same lock-aware path.
        runs = []
        trig.run_builds(path, run=lambda library, name: runs.append((library, name)) or True)
        assert runs == [("Movies", "Top 100")] and live_record.pending_builds(conn) == []
        assert trig.queue_watched_list(_event(), path) == trig.WATCH_DUPLICATE
        trig.run_builds(path, run=lambda library, name: runs.append((library, name)) or True)
        assert runs == [("Movies", "Top 100")]
        conn.close()

    def test_a_busy_run_lock_requeues_without_losing_the_run(self, trig, tmp_path, monkeypatch):
        live_record, path = _watched(trig, tmp_path)
        monkeypatch.setattr(trig, "wait_for_run_lock", lambda path=None: True)  # a sweep held it, and let go
        trig.queue_watched_list(_event(), path)
        results = [trig.RUN_BUSY, True]
        runs = []
        trig.run_builds(path, run=lambda library, name: runs.append(1) or results.pop(0))
        # exit 75: run_unless_busy waits for the lock to come free and runs it again, uncharged.
        assert len(runs) == 2

    def test_an_unknown_url_is_warned_about_and_queues_nothing(self, trig, tmp_path, caplog):
        live_record, path = _watched(trig, tmp_path)
        assert trig.queue_watched_list(_event(url="https://letterboxd.com/x/list/nope/"), path) == trig.WATCH_UNKNOWN
        assert any(r.levelname == "WARNING" and "nope" in r.getMessage() for r in caplog.records)
        conn = live_record.connect(path)
        assert live_record.pending_builds(conn) == []
        conn.close()

    @pytest.mark.parametrize("body", [b"not json", b"[]", b"{}", json.dumps({"url": "u"}).encode(), json.dumps({"url": "", "hash": "h"}).encode(), json.dumps({"url": 3, "hash": "h"}).encode()])
    def test_an_unreadable_event_is_poison_and_logs_error(self, trig, tmp_path, caplog, body):
        _, path = _watched(trig, tmp_path)
        assert trig.queue_watched_list(body, path) == trig.WATCH_POISON
        assert any(r.levelname == "ERROR" for r in caplog.records)

    def _consumer(self, trig, tmp_path, woke):
        _, path = _watched(trig, tmp_path)
        return trig.WatchConsumer(path, "nats://x:4222", "seed", lambda: woke.append(1)), path

    def test_a_failed_ack_after_the_commit_still_runs_the_build_on_redelivery(self, trig, tmp_path):
        import asyncio

        woke = []
        consumer, path = self._consumer(trig, tmp_path, woke)
        msg = FakeMsg(_event())

        async def broken_ack():
            raise ConnectionError("ack lost")

        msg.ack = broken_ack
        with pytest.raises(ConnectionError):
            asyncio.run(consumer.handle(msg))
        assert woke == []  # committed and queued, the builder never woken
        again = FakeMsg(_event(), delivered=2)
        asyncio.run(consumer.handle(again))
        assert again.acked and woke == [1]

    def test_the_message_is_acked_only_after_the_run_is_queued_and_wakes_the_builder(self, trig, tmp_path):
        import asyncio

        woke = []
        consumer, path = self._consumer(trig, tmp_path, woke)
        msg = FakeMsg(_event())
        asyncio.run(consumer.handle(msg))
        assert msg.acked and not msg.termed and woke == [1]
        again = FakeMsg(_event(), delivered=2)
        asyncio.run(consumer.handle(again))
        assert again.acked and woke == [1, 1]  # a duplicate wakes the builder too

    def test_poison_is_terminated_not_redelivered(self, trig, tmp_path):
        import asyncio

        consumer, _ = self._consumer(trig, tmp_path, [])
        msg = FakeMsg(b"garbage")
        asyncio.run(consumer.handle(msg))
        assert msg.termed and not msg.acked

    def test_an_unwritable_record_leaves_the_message_unacked_and_the_last_delivery_logs_error(self, trig, tmp_path, caplog, monkeypatch):
        import asyncio

        woke = []
        consumer, _ = self._consumer(trig, tmp_path, woke)

        def boom(*_a, **_k):
            raise OSError("disk gone")

        monkeypatch.setattr(trig, "queue_watched_list", boom)
        first = FakeMsg(_event(), delivered=1)
        asyncio.run(consumer.handle(first))
        assert not first.acked and not first.termed and woke == []
        assert not any(r.levelname == "ERROR" for r in caplog.records)
        last = FakeMsg(_event(), delivered=trig.WATCH_MAX_DELIVER)
        asyncio.run(consumer.handle(last))
        assert not last.acked and any(r.levelname == "ERROR" and "dropped" in r.getMessage() for r in caplog.records)

    def test_an_idle_pull_asks_again_and_a_message_is_handled(self, trig, tmp_path):
        import asyncio

        woke = []
        consumer, _ = self._consumer(trig, tmp_path, woke)
        stop = threading.Event()
        msg = FakeMsg(_event())

        class Timeout(Exception):
            pass

        Timeout.__name__ = "TimeoutError"
        sub = FakeSub([Timeout(), [msg], Timeout()])
        orig = sub.fetch

        async def fetch(batch=1, timeout=None):
            if not sub.batches:
                stop.set()
                return []
            return await orig(batch, timeout)

        sub.fetch = fetch
        asyncio.run(consumer.consume(sub, stop))
        assert msg.acked and woke == [1]

    def test_main_binds_the_bridge_durable_and_rebinds_when_it_does_not_exist_yet(self, trig, tmp_path, monkeypatch, caplog):
        import asyncio

        monkeypatch.setattr(trig, "PUBLISH_RETRY_MIN_SECONDS", 0.01)
        woke = []
        consumer, _ = self._consumer(trig, tmp_path, woke)
        stop = threading.Event()
        msg = FakeMsg(_event())
        binds = []

        class ServiceUnavailableError(Exception):
            """nats-py: the first pull on a durable that does not exist gets a 503 (bind itself sends nothing)."""

        unsubscribed = []

        class Sub(FakeSub):
            async def unsubscribe(self):
                unsubscribed.append(1)

        class Js:
            async def pull_subscribe_bind(self, durable, stream=None):
                binds.append((durable, stream))
                if len(binds) == 1:
                    return Sub([ServiceUnavailableError("503")])
                return Sub([[msg]])

        class Nc:
            def jetstream(self):
                return Js()

            async def close(self):
                pass

        async def connect():
            return Nc()

        consumer._connect = connect
        original = consumer.handle

        async def handle(m):
            await original(m)
            stop.set()

        consumer.handle = handle
        asyncio.run(consumer.main(stop))
        assert binds == [("kometa-trigger-watchedlists", "LETTERBOXD")] * 2
        assert msg.acked and unsubscribed == [1]
        assert any(r.levelname == "WARNING" and "binding again" in r.getMessage() for r in caplog.records)

    def test_the_seed_is_shared_and_never_logged_and_no_seed_says_so(self, trig, monkeypatch, caplog):
        started = threading.Event()
        monkeypatch.setattr(trig.WatchConsumer, "run_forever", lambda self: started.set())
        assert trig.start_watch_consumer("", lambda: None) is None
        assert any(r.levelname == "ERROR" and "watchedlist" in r.getMessage() for r in caplog.records)
        consumer = trig.start_watch_consumer("SUAMSEEDSEEDSEED", lambda: None)
        assert consumer is not None and started.wait(10) and "SUAMSEEDSEEDSEED" not in caplog.text

    def test_a_change_that_arrives_while_its_run_is_going_is_run_after_it(self, trig, tmp_path):
        live_record, path = _watched(trig, tmp_path)
        trig.queue_watched_list(_event(content_hash="h1"), path)
        runs = []

        def run(library, name):
            runs.append(name)
            if len(runs) == 1:
                # h2 lands (and is acked) while h1's run is going.
                assert trig.queue_watched_list(_event(content_hash="h2"), path) == trig.WATCH_QUEUED
            return True

        trig.run_builds(path, run=run)
        conn = live_record.connect(path)
        assert len(live_record.pending_builds(conn)) == 1  # h2 survived h1's run
        trig.run_builds(path, run=run)
        assert runs == ["Top 100", "Top 100"] and live_record.pending_builds(conn) == []
        conn.close()

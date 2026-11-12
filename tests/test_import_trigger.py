"""Tests for deploy/trigger/import_trigger.py, the websocket-driven import trigger.

The trigger is a standalone script bind-mounted into its container, so it is
loaded here by path. Plex is faked: nothing here opens a socket.
"""

from __future__ import annotations

import importlib.util
import json
import os
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
        monkeypatch.setattr(trig.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0, stdout="", stderr=""))
        assert trig.run_kometa("Movies", ["10", "11"])
        cmd = calls[0]
        assert cmd[cmd.index("--run-items") + 1] == "10|11"
        assert "--operations-only" in cmd and "--read-only-config" in cmd
        assert cmd[cmd.index("--run-libraries") + 1] == "Movies"

    @pytest.mark.parametrize("stream", ["stdout", "stderr"])
    def test_run_on_no_library_is_an_error_not_a_retry(self, trig, monkeypatch, caplog, stream):
        # Live 11-12: Kometa exited 0 having matched no library; that was logged as finished.
        out = {"stdout": "", "stderr": "", stream: "[CRITICAL] | Config Error: No libraries were found in config"}
        monkeypatch.setattr(trig.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=0, **out))
        assert trig.run_kometa("Involuntary Film Club", ["10"])  # settled: retrying cannot help
        assert any(r.levelname == "ERROR" for r in caplog.records)
        assert not any("finished operations" in r.getMessage() for r in caplog.records)

    def test_unstartable_kometa_returns_false(self, trig, monkeypatch):
        def missing(cmd, **kw):
            raise FileNotFoundError("python")

        monkeypatch.setattr(trig.subprocess, "run", missing)
        assert not trig.run_kometa("Movies", ["10"])

    def test_failure_and_timeout_return_false(self, trig, monkeypatch):
        monkeypatch.setattr(trig.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=1, stderr="boom"))
        assert not trig.run_kometa("Movies", ["10"])

        def timeout(cmd, **kw):
            raise trig.subprocess.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(trig.subprocess, "run", timeout)
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

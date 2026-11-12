"""Tests for deploy/trigger/import_trigger.py, the websocket-driven import trigger.

The trigger is a standalone script bind-mounted into its container, so it is
loaded here by path. Plex is faked: nothing here opens a socket.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from plexapi.exceptions import NotFound

TRIGGER_PATH = Path(__file__).resolve().parent.parent / "deploy" / "trigger" / "import_trigger.py"


@pytest.fixture
def trig(monkeypatch):
    spec = importlib.util.spec_from_file_location("import_trigger_under_test", TRIGGER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DEBOUNCE_SECONDS", 90)
    monkeypatch.setattr(module, "RETRY_SECONDS", 150)
    monkeypatch.setattr(module, "NEW_ITEM_WINDOW_SECONDS", 6 * 3600)
    return module


@pytest.fixture
def pending(trig, tmp_path):
    return trig.Pending(str(tmp_path / "queue.json"))


NOW = time.time()


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
        assert pending.claim("10", NOW - 100, now=NOW)
        later = NOW + trig.NEW_ITEM_WINDOW_SECONDS
        assert pending.claim("11", later - 1, now=later)
        assert "10" not in pending.seen

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
        trig.handle_notification(server, pending, timeline(entry(99), entry(12, section="7"), entry(12)))
        assert pending.items == {"Movies": {"12": 0}}


class TestCatchUp:
    def test_queues_unseen_recent_items_only(self, trig, pending):
        movies = FakeSection(2, "Movies", "movie", [movie(10), movie(11, added=NOW - 2 * 86400), movie(12)])
        shows = FakeSection(1, "TV Shows", "show", [episode(501, show=500)])
        music = FakeSection(3, "Music", "artist", [movie(900)])
        server = FakeServer(sections=[movies, shows, music])
        pending.claim("12", NOW - 60, now=NOW)  # already handled before the restart
        assert trig.catch_up(server, pending, now=NOW) == 2
        assert pending.items == {"Movies": {"10": 0}, "TV Shows": {"500": 0}}
        assert movies.searches[0][0] == "movie" and shows.searches[0][0] == "episode"
        assert music.searches == []

    def test_second_sweep_queues_nothing(self, trig, pending):
        server = FakeServer(sections=[FakeSection(2, "Movies", "movie", [movie(10)])])
        assert trig.catch_up(server, pending, now=NOW) == 1
        assert trig.catch_up(server, pending, now=NOW) == 0


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
        seen = {}

        class FakeApp:
            def __init__(self, url, on_message, on_error):
                seen["url"] = url

            def run_forever(self, **kwargs):
                seen["kwargs"] = kwargs

        monkeypatch.setitem(sys.modules, "websocket", SimpleNamespace(WebSocketApp=FakeApp))
        trig.Listener(FakeServer(), lambda d: None).run()
        assert seen["url"].startswith("ws://10.0.0.1:32400/:/websockets/notifications?")
        assert seen["kwargs"]["ping_interval"] > seen["kwargs"]["ping_timeout"] > 0

    def test_token_never_logged(self, trig, monkeypatch, caplog):
        class FakeApp:
            def __init__(self, url, on_message, on_error):
                on_error(self, ConnectionResetError("reset by peer"))

            def run_forever(self, **kwargs):
                pass

        monkeypatch.setitem(sys.modules, "websocket", SimpleNamespace(WebSocketApp=FakeApp))
        with caplog.at_level("DEBUG"):
            trig.Listener(FakeServer(), lambda d: None).run()
        assert "SECRET" not in caplog.text
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

            def run(self):
                events.append(("socket-open",))
                self.on_message(timeline())  # a notification arrives, then the socket drops
                events.append(("socket-closed",))

        trig.listen_forever(pending, connect=connect, listener_cls=FakeListener, stop=stop)
        return events

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
        pending.add("Movies", "10")
        assert pending.take_due() == {}

    def test_due_after_debounce_and_queue_empties(self, trig, pending):
        pending.add("Movies", "10")
        pending.due_at = time.time() - 1
        assert pending.take_due() == {"Movies": {"10": 0}}
        assert pending.items == {}
        assert json.loads(Path(pending.path).read_text())["items"] == {}

    def test_wait_due_wakes_on_add(self, trig, pending, monkeypatch):
        monkeypatch.setattr(trig, "DEBOUNCE_SECONDS", 0)
        result = {}
        thread = threading.Thread(target=lambda: result.update(pending.wait_due()), daemon=True)
        thread.start()
        pending.add("Movies", "10")
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert result == {"Movies": {"10": 0}}

    def test_recovered_queue_is_due_promptly(self, trig, tmp_path, monkeypatch):
        path = tmp_path / "queue.json"
        path.write_text(json.dumps({"items": {"Movies": {"10": 2}, "Empty": {}}}))
        recovered = trig.Pending(str(path))
        assert recovered.items == {"Movies": {"10": 2}}
        assert recovered.due_at <= time.time() + trig.DEBOUNCE_SECONDS

    def test_corrupt_queue_file_starts_empty(self, trig, tmp_path):
        path = tmp_path / "queue.json"
        path.write_text("{not json")
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
        pending.claim("10", NOW - 60)
        runs = []
        trig.process_due(pending, {"Movies": {"10": 0}}, connect=lambda: server, run=lambda *a: runs.append(a))
        assert pending.items == {"Movies": {"10": 1}}
        assert pending.due_at >= time.time() + trig.RETRY_SECONDS - 5
        pending.items.clear()
        trig.process_due(pending, {"Movies": {"10": 2}}, connect=lambda: server, run=lambda *a: runs.append(a))
        assert pending.items == {}
        assert "10" not in pending.seen  # a later match event may queue it again
        assert runs == []

    def test_vanished_item_is_dropped(self, trig, pending):
        runs = []
        trig.process_due(pending, {"Movies": {"99": 0}}, connect=lambda: FakeServer(), run=lambda *a: runs.append(a))
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
        trig.process_due(pending, {"Movies": {"10": 0, "11": 0}}, connect=lambda: server, run=lambda lib, keys: runs.append(keys))
        assert runs == [["11"]]
        assert pending.items == {"Movies": {"10": 0}}


class TestRunKometa:
    def test_command_is_item_scoped(self, trig, monkeypatch):
        calls = []
        monkeypatch.setattr(trig.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0, stderr=""))
        assert trig.run_kometa("Movies", ["10", "11"])
        cmd = calls[0]
        assert cmd[cmd.index("--run-items") + 1] == "10|11"
        assert "--operations-only" in cmd and "--read-only-config" in cmd
        assert cmd[cmd.index("--run-libraries") + 1] == "Movies"

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

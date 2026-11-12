"""Apply Kometa library operations to an item as soon as Plex imports it.

Listens on Plex Media Server's local websocket (``/:/websockets/notifications``)
and runs ``kometa.py --operations-only --run-items`` for newly added items, so a
new import gets its poster, background, content rating and labels immediately
instead of waiting for the next scheduled sweep.

Why the websocket: Plex's ``library.new`` webhook never fired on this server
(138 films in 14 days, zero deliveries), so the old HTTP receiver was deleted.
The websocket's library timeline is the event Plex actually emits.

Runs from the Kometa image itself, so it is always the same build as the
scheduler and needs no Docker socket to reach it: it runs kometa.py as a
subprocess against the same /config.

What makes it more than a shell-out:

* **New items only.** A timeline entry in state 5 ("processed") fires on every
  refresh and edit too, not just on import. An item counts as new only if Plex
  added it after the earlier of (now - NEW_ITEM_WINDOW_SECONDS) and the start of
  the last catch-up sweep, never more than MAX_CATCH_UP_SECONDS ago, and we have
  not queued it before (a persisted seen-set). The sweep runs on every reconnect,
  so on a long-lived socket that window can span days; the seen-set spans the
  same window, so a refresh of anything already queued is still ignored.
* **Shows, not episodes.** Library operations are defined per top-level item;
  an episode or season is queued as its show.
* **Debounce.** Kometa costs ~17s of startup per invocation regardless of how
  many items it is given, so a batch import should be one run, not twenty.
  Keys collected within DEBOUNCE_SECONDS of each other go out together.
* **Match verification.** An item can be processed before Plex's agent has
  matched it, so its GUID may still be ``local://``. Operations sourced from
  TMDb would find nothing and lock in that nothing. Unmatched keys are retried
  on a backoff instead.
* **Durable queue, no gaps.** Pending keys, the batch being run and the
  seen-set are written to disk. A failed run or a crash mid-run requeues its
  batch. Every (re)connect sweeps the libraries for items added since the last
  completed sweep (up to MAX_CATCH_UP_SECONDS back), so a restart, a dropped
  socket or a Plex outage does not lose an import.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess  # nosec B404 - runs kometa.py with a fixed argv, no shell
import threading
import time
from datetime import datetime
from typing import Any, Callable

import yaml

log = logging.getLogger("kometa-import-trigger")

CONFIG_PATH = os.environ.get("KOMETA_CONFIG", "/config/config.yml")
KOMETA_PY = os.environ.get("KOMETA_PY", "/kometa.py")
QUEUE_PATH = os.environ.get("QUEUE_PATH", "/config/.kometa-trigger-queue.json")
DEBOUNCE_SECONDS = int(os.environ.get("DEBOUNCE_SECONDS", "90"))
# A Plex agent match usually lands within a minute; give up after roughly half an
# hour and leave the item to the scheduled sweep.
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "12"))
RETRY_SECONDS = int(os.environ.get("RETRY_SECONDS", "150"))
RUN_TIMEOUT_SECONDS = int(os.environ.get("RUN_TIMEOUT_SECONDS", "1800"))
# How recently Plex must have added an item for a "processed" event to count as
# an import rather than a refresh. Also the reach of the catch-up sweep.
NEW_ITEM_WINDOW_SECONDS = int(os.environ.get("NEW_ITEM_WINDOW_SECONDS", str(6 * 3600)))
# How far back a catch-up sweep may reach after an outage (trigger or Plex down).
MAX_CATCH_UP_SECONDS = int(os.environ.get("MAX_CATCH_UP_SECONDS", str(7 * 86400)))
RECONNECT_MIN_SECONDS = int(os.environ.get("RECONNECT_MIN_SECONDS", "5"))
RECONNECT_MAX_SECONDS = int(os.environ.get("RECONNECT_MAX_SECONDS", "300"))
# A connection that stayed up this long was healthy; reset the backoff after it.
STABLE_CONNECTION_SECONDS = 60
# A websocket handshake that has not finished by then is treated as hung.
OPEN_TIMEOUT_SECONDS = 60
PLEX_TIMEOUT_SECONDS = 30
CLOCK_SKEW_SECONDS = 300

LIBRARY_IDENTIFIER = "com.plexapp.plugins.library"
STATE_PROCESSED = 5
# Plex metadata type numbers in timeline entries. Seasons and shows are not
# imported on their own; their episodes are, and resolve to the show.
TIMELINE_TYPES = {1: "movie", 4: "episode"}
# Library section type -> the item type that a new import shows up as.
SECTION_LIBTYPES = {"movie": "movie", "show": "episode"}

# Plex writes a placeholder GUID until its agent has matched the item.
UNMATCHED_GUID_PREFIX = "local://"


class Pending:
    """Rating keys waiting to be sent to Kometa, grouped by library.

    Three persisted parts:

    * ``items``: keys waiting for their batch, with how many attempts they have had.
    * ``inflight``: the batch currently being run. It is cleared only once the batch
      is settled, so a crash or a killed container mid-run puts it back in the queue.
    * ``seen``: every import already admitted, keyed by the item Plex reported (an
      episode, say) with the key it was queued as (its show). It stops a refresh of
      an already-queued item from queueing it again.
    * ``horizon``: when the last completed catch-up sweep started. Everything added
      after it was either swept or heard live, so the next sweep starts there and an
      outage of any length (up to MAX_CATCH_UP_SECONDS) loses no arrival.
    """

    def __init__(self, path: str):
        self.path = path
        self.cond = threading.Condition()
        self.due_at = 0.0
        # {library: {rating_key: attempts}}
        self.items: dict[str, dict[str, int]] = {}
        self.inflight: dict[str, dict[str, int]] = {}
        # {source rating_key: [addedAt epoch seconds, queued rating_key]}
        self.seen: dict[str, list] = {}
        self.horizon: float | None = None
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return  # first start
        except (OSError, ValueError) as e:
            log.error("queue file %s is unreadable (%s: %s); starting with an empty queue - the catch-up sweep re-finds recent arrivals", self.path, type(e).__name__, e)
            return
        try:
            for part in ("items", "inflight"):
                for lib, keys in (data.get(part) or {}).items():
                    for key, attempts in keys.items():
                        self.items.setdefault(str(lib), {})[str(key)] = max(int(attempts), self.items.get(str(lib), {}).get(str(key), 0))
            for source, value in (data.get("seen") or {}).items():
                if isinstance(value, list) and len(value) == 2:
                    self.seen[str(source)] = [float(value[0]), str(value[1])]
            if isinstance(data.get("horizon"), (int, float)):
                self.horizon = float(data["horizon"])
        except (AttributeError, TypeError, ValueError) as e:
            log.error("queue file %s is malformed (%s: %s); starting with an empty queue - the catch-up sweep re-finds recent arrivals", self.path, type(e).__name__, e)
            self.items, self.seen, self.horizon = {}, {}, None
            return
        if self.items:
            # Anything recovered from disk was already accepted (or was mid-run when
            # the process died); send it promptly rather than waiting for an event.
            self.due_at = time.time() + DEBOUNCE_SECONDS
            log.info("recovered %d pending items from %s", sum(len(v) for v in self.items.values()), self.path)

    def _save_locked(self) -> None:
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"items": self.items, "inflight": self.inflight, "seen": self.seen, "horizon": self.horizon}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except (OSError, TypeError, ValueError) as e:
            log.warning("could not persist queue: %s", e)

    def cutoff(self, now: float) -> float:
        """Oldest addedAt that still counts as new: the normal window, stretched back
        to the last sweep's start after an outage, never past MAX_CATCH_UP_SECONDS."""
        cutoff = now - NEW_ITEM_WINDOW_SECONDS
        if self.horizon is not None:
            cutoff = min(cutoff, self.horizon - CLOCK_SKEW_SECONDS)
        return max(cutoff, now - MAX_CATCH_UP_SECONDS)

    def mark_caught_up(self, started: float) -> None:
        with self.cond:
            self.horizon = started
            self._save_locked()

    def admit(self, library: str, source_key: str, target: str, added_at: float, now: float | None = None) -> bool:
        """Queue a new import under ``target``. False if it is not new: too old, pinned, or already seen.

        Marking it seen and queueing it are one locked, persisted step, so a crash
        cannot leave an item seen but never queued.
        """
        now = time.time() if now is None else now
        with self.cond:
            cutoff = self.cutoff(now)
            self.seen = {k: v for k, v in self.seen.items() if v[0] >= cutoff}
            # A future addedAt is a pin (this server parks ~20 old films at 2097
            # to hold them atop Recently Added), never an import.
            if added_at < cutoff or added_at > now + CLOCK_SKEW_SECONDS or str(source_key) in self.seen:
                return False
            self.seen[str(source_key)] = [added_at, str(target)]
            self.items.setdefault(library, {}).setdefault(str(target), 0)
            # max(): a pending retry backoff is not cut short by a new import.
            self.due_at = max(self.due_at, time.time() + DEBOUNCE_SECONDS)
            self._save_locked()
            self.cond.notify_all()
            return True

    def forget(self, target: str) -> None:
        """Drop every seen entry queued as ``target``, so a later event (say, Plex
        finally matching it) can queue it again. Used when we give up on it."""
        with self.cond:
            before = len(self.seen)
            self.seen = {k: v for k, v in self.seen.items() if v[1] != str(target)}
            if len(self.seen) != before:
                self._save_locked()

    def requeue(self, library: str, rating_key: str, attempts: int) -> None:
        with self.cond:
            self.items.setdefault(library, {})[str(rating_key)] = attempts
            self.due_at = max(self.due_at, time.time() + RETRY_SECONDS)
            self._save_locked()
            self.cond.notify_all()

    def take_due(self) -> dict[str, dict[str, int]]:
        with self.cond:
            return self._take_due_locked()

    def _take_due_locked(self) -> dict[str, dict[str, int]]:
        if not self.items or time.time() < self.due_at:
            return {}
        taken = self.items
        self.items = {}
        for lib, keys in taken.items():
            self.inflight.setdefault(lib, {}).update(keys)
        self._save_locked()
        return taken

    def settle(self) -> None:
        """The in-flight batch is done: every key in it ran, was requeued, or was given up on."""
        with self.cond:
            self.inflight = {}
            self._save_locked()

    def restore_inflight(self) -> None:
        """Put an unfinished batch back in the queue (the worker hit an unexpected error).

        Each key is charged an attempt, so a batch that keeps hitting the same bug
        is given up on after MAX_ATTEMPTS instead of blocking the queue forever.
        """
        with self.cond:
            for lib, keys in self.inflight.items():
                for key, attempts in keys.items():
                    attempts = max(attempts, self.items.get(lib, {}).get(key, 0)) + 1
                    if attempts >= MAX_ATTEMPTS:
                        log.warning("giving up on %s: batch failed %d times", key, attempts)
                        self.items.get(lib, {}).pop(key, None)
                        self.seen = {k: v for k, v in self.seen.items() if v[1] != key}
                        continue
                    self.items.setdefault(lib, {})[key] = attempts
            self.items = {lib: keys for lib, keys in self.items.items() if keys}
            self.inflight = {}
            if self.items:
                self.due_at = max(self.due_at, time.time() + RETRY_SECONDS)
            self._save_locked()
            self.cond.notify_all()

    def wait_due(self) -> dict[str, dict[str, int]]:
        """Block until a batch is due. Wakes on admit/requeue or at the deadline, never on a timer."""
        with self.cond:
            while True:
                taken = self._take_due_locked()
                if taken:
                    return taken
                self.cond.wait(max(0.0, self.due_at - time.time()) if self.items else None)

    def counts(self) -> dict[str, int]:
        with self.cond:
            return {lib: len(keys) for lib, keys in self.items.items()}


def load_plex() -> tuple[str, str]:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    plex = config["plex"]
    return plex["url"].rstrip("/"), plex["token"]


def connect_plex() -> Any:
    from plexapi.server import PlexServer

    url, token = load_plex()
    return PlexServer(url, token, timeout=PLEX_TIMEOUT_SECONDS)


def epoch(value: Any) -> float | None:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    return None


def target_key(item: Any) -> str:
    """Map a season or episode onto its show.

    Library operations are defined per top-level item; the sweep walks shows and
    descends into their seasons and episodes itself. Handing it a bare episode
    key would apply show-level operations to an episode.
    """
    if item.type == "episode":
        return str(item.grandparentRatingKey or item.ratingKey or "")
    if item.type == "season":
        return str(item.parentRatingKey or item.ratingKey or "")
    return str(item.ratingKey or "")


def is_matched(item: Any) -> bool:
    guid = item.guid or ""
    return bool(guid) and not guid.startswith(UNMATCHED_GUID_PREFIX)


def consider(pending: Pending, library: str, item: Any, now: float | None = None) -> str | None:
    """Queue an item if it is a new import. Returns the queued key, or None."""
    added_at = epoch(getattr(item, "addedAt", None))
    if added_at is None or not library:
        return None
    key = target_key(item)
    if not key or not pending.admit(library, str(item.ratingKey), key, added_at, now=now):
        return None
    via = f" via {item.type} {item.ratingKey}" if key != str(item.ratingKey) else ""
    log.info("queued %s (%s%s) from %s", key, getattr(item, "grandparentTitle", None) or item.title, via, library)
    return key


def processed_entries(data: dict) -> list[dict]:
    """The library timeline entries in a websocket notification that report a finished item."""
    if data.get("type") != "timeline":
        return []
    return [
        entry
        for entry in data.get("TimelineEntry") or []
        if entry.get("identifier") == LIBRARY_IDENTIFIER and entry.get("state") == STATE_PROCESSED and entry.get("type") in TIMELINE_TYPES and entry.get("itemID") and entry.get("sectionID") not in (None, "", "-1")
    ]


def fetch_item(server: Any, rating_key: str) -> Any | None:
    from plexapi.exceptions import NotFound

    try:
        return server.fetchItem(int(rating_key))
    except NotFound:
        return None


def handle_notification(server: Any, pending: Pending, data: dict) -> list[dict]:
    """Queue the new imports in a notification. Returns the entries whose lookup failed."""
    failed = []
    for entry in processed_entries(data):
        try:
            item = fetch_item(server, str(entry["itemID"]))
            if item is None:
                continue
            # The item names its own library; the section map is cached per
            # session and misses a library created after connect.
            library = getattr(item, "librarySectionTitle", None) or server.library.sectionByID(int(entry["sectionID"])).title
        except Exception as e:  # one bad entry must not stop the rest
            log.warning("could not look up %s %s: %s", TIMELINE_TYPES[entry["type"]], entry["itemID"], e)
            failed.append(entry)
            continue
        consider(pending, library, item)
    return failed


def resolve_forever(inbox: queue.Queue, pending: Pending) -> None:
    """Drain websocket notifications off the socket's read thread.

    Looking items up in Plex can take seconds; doing it inside the websocket
    callback would stall the read loop (and its ping/pong) during a bulk import.
    """
    while True:
        server, data, attempt = inbox.get()
        try:
            failed = handle_notification(server, pending, data)
        except Exception as e:
            log.exception("notification handler failed: %s", e)
            failed = processed_entries(data)
        if failed:
            retry_lookup(inbox, server, failed, attempt)


def retry_lookup(inbox: queue.Queue, server: Any, entries: list[dict], attempt: int) -> None:
    """Look failed entries up again after a backoff (a one-shot timer, not a poll)."""
    keys = ", ".join(str(e["itemID"]) for e in entries)
    if attempt + 1 >= MAX_ATTEMPTS:
        log.error("giving up looking up %s after %d attempts; the next reconnect sweep still covers them", keys, attempt + 1)
        return
    log.info("retrying lookup of %s in %ds (attempt %d)", keys, RETRY_SECONDS, attempt + 1)
    timer = threading.Timer(RETRY_SECONDS, inbox.put, args=((server, {"type": "timeline", "TimelineEntry": entries}, attempt + 1),))
    timer.daemon = True
    timer.start()


def catch_up(server: Any, pending: Pending, now: float | None = None) -> int:
    """Queue anything added since the last sweep that we have not seen: covers restarts,
    dropped sockets and Plex outages. Advances the horizon only when every section was swept."""
    now = time.time() if now is None else now
    with pending.cond:
        since = datetime.fromtimestamp(pending.cutoff(now))
    queued = 0
    for section in server.library.sections():
        libtype = SECTION_LIBTYPES.get(section.type)
        if not libtype:
            continue
        for item in section.search(libtype=libtype, filters={"addedAt>>": since}):
            if consider(pending, section.title, item, now=now):
                queued += 1
    # The socket was already listening when this sweep began, so from here on it
    # covers every arrival; a sweep that threw part-way leaves the old horizon.
    pending.mark_caught_up(now)
    return queued


class Listener:
    """One websocket session with Plex. ``run()`` returns when the socket closes or errors."""

    key = "/:/websockets/notifications"
    PING_INTERVAL = 60
    PING_TIMEOUT = 30

    def __init__(self, server: Any, on_message: Callable[[dict], None]):
        self.server = server
        self.on_message = on_message
        self.ws: Any = None
        # Set once the handshake succeeds or the attempt ends; ``opened`` says which.
        self.ready = threading.Event()
        self.opened = False
        self.abandoned = False

    def _open(self, ws: Any) -> None:
        if self.abandoned:  # opened after we gave up on it: a newer session owns events
            ws.close()
            return
        self.opened = True
        self.ready.set()

    def abandon(self) -> None:
        """Give up on a hung handshake: drop anything it delivers and close it if it can be."""
        self.abandoned = True
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception as e:
            log.warning("could not close hung websocket: %s", e)

    def _message(self, _ws: Any, message: str) -> None:
        if self.abandoned:
            return
        try:
            data = json.loads(message)["NotificationContainer"]
        except (ValueError, KeyError, TypeError) as e:
            log.warning("unreadable websocket message: %s", e)
            return
        try:
            self.on_message(data)
        except Exception as e:  # never let a handler bug drop the connection
            log.exception("handler failed: %s", e)

    def _error(self, _ws: Any, error: Any) -> None:
        log.warning("websocket error: %s: %s", type(error).__name__, error)

    def run(self) -> None:
        import websocket

        # The URL carries the token as a query parameter: it is never logged.
        url = self.server.url(self.key, includeToken=True).replace("http", "ws", 1)
        self.ws = websocket.WebSocketApp(url, on_open=self._open, on_message=self._message, on_error=self._error)
        # Pings detect a half-open socket; run_forever returns on close or error.
        try:
            self.ws.run_forever(ping_interval=self.PING_INTERVAL, ping_timeout=self.PING_TIMEOUT)
        finally:
            self.ready.set()


def listen_forever(
    pending: Pending,
    connect: Callable[[], Any] = connect_plex,
    listener_cls: type = Listener,
    stop: threading.Event | None = None,
) -> None:
    """Hold a websocket session open; on a drop, reconnect (with backoff) and catch up."""
    stop = stop or threading.Event()
    inbox: queue.Queue = queue.Queue()
    threading.Thread(target=resolve_forever, args=(inbox, pending), daemon=True, name="plex-resolver").start()
    delay = RECONNECT_MIN_SECONDS
    while not stop.is_set():
        started = time.time()
        try:
            server = connect()
            listener = listener_cls(server, lambda data, server=server: inbox.put((server, data, 0)))
            thread = threading.Thread(target=listener.run, daemon=True, name="plex-websocket")
            thread.start()
            # Sweep only once the socket is open, so nothing added in between is
            # missed and the horizon never advances while we are deaf. A failed
            # sweep must not abandon the live socket (a second listener would
            # double-queue), so it is contained here.
            if not listener.ready.wait(OPEN_TIMEOUT_SECONDS):
                # Hung handshake. Reconnect rather than go deaf; the abandoned
                # listener drops anything it might still deliver.
                log.warning("Plex websocket did not open within %ds", OPEN_TIMEOUT_SECONDS)
                listener.abandon()
            elif listener.opened:
                log.info("listening to Plex websocket (debounce %ds, new-item window %ds)", DEBOUNCE_SECONDS, NEW_ITEM_WINDOW_SECONDS)
                try:
                    caught = catch_up(server, pending)
                    if caught:
                        log.info("catch-up queued %d item(s) added while not listening", caught)
                except Exception as e:
                    log.warning("catch-up sweep failed: %s: %s", type(e).__name__, e)
                thread.join()
            log.warning("Plex websocket closed")
        except Exception as e:
            log.warning("Plex connection failed: %s: %s", type(e).__name__, e)
        if time.time() - started >= STABLE_CONNECTION_SECONDS:
            delay = RECONNECT_MIN_SECONDS
        log.info("reconnecting in %ds", delay)
        stop.wait(delay)
        delay = min(delay * 2, RECONNECT_MAX_SECONDS)


def run_kometa(library: str, rating_keys: list[str]) -> bool:
    command = [
        "python3",
        KOMETA_PY,
        "-c",
        CONFIG_PATH,
        "--operations-only",
        "--run-libraries",
        library,
        "--run-items",
        "|".join(rating_keys),
        "--ignore-schedules",
        "--read-only-config",
    ]
    log.info("running operations for %d item(s) in %s: %s", len(rating_keys), library, ",".join(rating_keys))
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS)  # nosec B603 - fixed argv
    except subprocess.TimeoutExpired:
        log.error("Kometa run for %s timed out after %ss", library, RUN_TIMEOUT_SECONDS)
        return False
    except OSError as e:
        log.error("could not start Kometa for %s: %s", library, e)
        return False
    if result.returncode != 0:
        log.error("Kometa exited %s for %s: %s", result.returncode, library, result.stderr[-2000:])
        return False
    log.info("finished operations for %s", library)
    return True


def give_up_or_retry(pending: Pending, library: str, rating_key: str, attempts: int, why: str) -> None:
    if attempts + 1 >= MAX_ATTEMPTS:
        log.warning("giving up on %s: %s after %d attempts", rating_key, why, attempts + 1)
        # Clear it from the seen-set (every episode queued as this show, too), so a
        # later processed event - Plex finally matching it, say - queues it again.
        pending.forget(rating_key)
        return
    log.info("%s %s, retrying (attempt %d)", rating_key, why, attempts + 1)
    pending.requeue(library, rating_key, attempts + 1)


def process_due(
    pending: Pending,
    due: dict[str, dict[str, int]],
    connect: Callable[[], Any] = connect_plex,
    run: Callable[[str, list[str]], bool] = run_kometa,
) -> None:
    """Run one batch. Every key ends up run, requeued or given up on; then the batch is settled."""
    try:
        server = connect()
    except Exception as e:
        log.warning("Plex unreachable, holding %d item(s): %s", sum(len(k) for k in due.values()), e)
        for library, keys in due.items():
            for rating_key, attempts in keys.items():
                pending.requeue(library, rating_key, attempts)
        pending.settle()
        return
    for library, keys in due.items():
        ready: dict[str, int] = {}
        for rating_key, attempts in keys.items():
            try:
                item = fetch_item(server, rating_key)
            except Exception as e:
                log.warning("Plex lookup for %s failed, retrying: %s", rating_key, e)
                pending.requeue(library, rating_key, attempts)
                continue
            if item is None:
                # Item vanished between the event and now (a failed import, a
                # replaced file). Nothing to apply.
                log.info("dropping %s: no longer in Plex", rating_key)
                pending.forget(rating_key)
                continue
            if not is_matched(item):
                give_up_or_retry(pending, library, rating_key, attempts, "not matched yet")
                continue
            target = target_key(item)
            if target:
                ready[target] = max(attempts, ready.get(target, 0))
        if ready and not run(library, list(ready)):
            for target, attempts in ready.items():
                give_up_or_retry(pending, library, target, attempts, "Kometa run failed")
    pending.settle()


def worker_step(pending: Pending, connect: Callable[[], Any] = connect_plex, run: Callable[[str, list[str]], bool] = run_kometa) -> None:
    due = pending.wait_due()
    try:
        process_due(pending, due, connect, run)
    except Exception as e:
        # Never let one bad batch kill the only thread that runs Kometa.
        log.exception("batch failed, requeueing it: %s", e)
        pending.restore_inflight()


def worker(pending: Pending, connect: Callable[[], Any] = connect_plex) -> None:
    while True:
        worker_step(pending, connect)


def assert_run_items_supported() -> None:
    """Refuse to start against a Kometa build that predates --run-items.

    Kometa does not reject unrecognised flags; it ignores them and starts a normal
    run. So on a version skew this service would silently kick off a full library
    sweep for every imported file instead of a one-item run.
    """
    try:
        with open(KOMETA_PY, encoding="utf-8") as fh:
            source = fh.read()
    except OSError as e:
        raise SystemExit(f"cannot read {KOMETA_PY}: {e}")
    if '"run-items"' not in source:
        raise SystemExit(f"{KOMETA_PY} has no --run-items flag. Kometa ignores unknown flags and would run a " f"FULL library sweep per import. Rebuild the image from a fork revision that has it.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # plexapi logs request URLs, which carry the token; keep it to warnings.
    logging.getLogger("plexapi").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)
    assert_run_items_supported()
    pending = Pending(QUEUE_PATH)
    threading.Thread(target=worker, args=(pending,), daemon=True, name="kometa-runner").start()
    listen_forever(pending)


if __name__ == "__main__":
    main()

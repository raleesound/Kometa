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
* **Arrivals (#5634 S3).** The item run itself puts a new item into every
  collection the live record says is waiting for it (kometa.py's arrival step,
  modules/arrivals.py) and leaves one kometa.item.arrived event per item in the
  record's outbox. This process publishes that outbox to JetStream from its own
  thread, acknowledged, deduplicated by Nats-Msg-Id. A row leaves the outbox only
  once JetStream acknowledged it: a NATS outage, or no stream yet, only delays it.
  A waiting collection that is not in Plex yet is queued as a scoped Kometa run
  of that collection, which this process runs after the batch.
* **Watched lists (#5634 S6).** A Letterboxd list a live_watch collection is built from changes:
  the bridge publishes letterboxd.watchedlist.updated, this process pulls it from its durable
  (kometa-trigger-watchedlists) on the same NATS identity and queues a scoped run of just the
  collections the live record says use that list url, through the same builder as above. The
  message is acknowledged once the run is in the record's durable builds queue (not after it);
  (url, hash) is remembered in the same transaction, so a redelivery queues nothing.
* **Collection definitions (#5634 S8).** A collection block added to, or edited in, a collection file config.yml lists (a deploy.sh
  rsync, a hand edit, plex-bot /addlist: all just write the file) is noticed by inotify on those files' directories. Once the writes
  go quiet for COLLECTION_QUIET_SECONDS every listed file is parsed (one that does not parse is held until its next write) and each
  collection's definition is hashed (modules/collection_defs.py); a new or changed one is queued as a scoped run through the same
  builder. The first start only records the hashes; a removed collection only loses its row.
* **One Kometa run at a time.** An item run that finds a sweep holding the run
  lock exits 75; this process waits for the lock to come free and runs it again
  without charging it an attempt.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import queue
import signal
import subprocess  # nosec B404 - runs kometa.py with a fixed argv, no shell
import sys
import threading
import time
from datetime import datetime, timezone
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
# kometa.py's exit code when another run holds the run lock (EX_TEMPFAIL).
EXIT_LOCK_BUSY = 75
RUN_LOCK_PATH = os.environ.get("RUN_LOCK_PATH", os.path.join(os.path.dirname(CONFIG_PATH), ".kometa.run.lock"))
# A run that reports the lock busy while nobody holds it (it could not open the lock file, say)
# is a failure, not a reason to run it again forever.
MAX_SPURIOUS_BUSY = 3
# A scoped run of a collection an arrival is waiting for; past this many failures the scheduled
# sweep builds it instead.
BUILD_MAX_ATTEMPTS = int(os.environ.get("BUILD_MAX_ATTEMPTS", "3"))
# NATS (#5634): the per-client nkey user kometa-trigger. The seed comes from an env file
# (docker --env-file) and is never logged; the trigger drops it from its environment so the
# Kometa runs it starts never inherit it.
NATS_URL = os.environ.get("NATS_URL", "nats://10.43.155.192:4222")
NATS_CLIENT_NAME = "kometa-trigger"
NATS_INBOX_PREFIX = "_INBOX_kometa_trigger"
ARRIVED_SUBJECT = "kometa.item.arrived"
WATCHED_SUBJECT = "letterboxd.watchedlist.updated"
# The watched-list durable letterboxd-bridge creates and owns (@ai-app-factory/letterboxd-contract);
# this identity may only read it: consumer info, pull, ack.
WATCH_STREAM = "LETTERBOXD"
WATCH_DURABLE = "kometa-trigger-watchedlists"
# The consumer's max_deliver: a delivery that fails on its last turn is dropped by the server.
WATCH_MAX_DELIVER = 4
# One pull request the server holds open until a message exists or this long passes; an idle
# expiry just asks again. It is a long poll, not a timer: the server wakes the pull on a message.
WATCH_PULL_SECONDS = int(os.environ.get("WATCH_PULL_SECONDS", "300"))
# A write to a collection file is acted on once the files have been quiet this long (a deploy rsyncs many in a burst).
COLLECTION_QUIET_SECONDS = float(os.environ.get("COLLECTION_QUIET_SECONDS", "5"))
PUBLISH_TIMEOUT_SECONDS = 10
PUBLISH_RETRY_MIN_SECONDS = 5
PUBLISH_RETRY_MAX_SECONDS = 300
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

# The live record is read and drained with the fork's own module, from the same image as the
# kometa.py this runs (stdlib only). Appended, so nothing in the image root shadows a stdlib module.
KOMETA_DIR = os.path.dirname(os.path.abspath(KOMETA_PY))
if KOMETA_DIR not in sys.path:
    sys.path.append(KOMETA_DIR)
from modules import collection_defs, live_record  # noqa: E402

RECORD_PATH = os.environ.get("RECORD_PATH", live_record.db_path(os.path.dirname(CONFIG_PATH)))

LIBRARY_IDENTIFIER = "com.plexapp.plugins.library"
STATE_PROCESSED = 5
# Plex metadata type numbers in timeline entries. Seasons and shows are not
# imported on their own; their episodes are, and resolve to the show.
TIMELINE_TYPES = {1: "movie", 4: "episode"}
# Library section type -> the item type that a new import shows up as.
SECTION_LIBTYPES = {"movie": "movie", "show": "episode"}

# Plex writes a placeholder GUID until its agent has matched the item.
UNMATCHED_GUID_PREFIX = "local://"
NO_LIBRARIES_MESSAGE = "No libraries were found in config"


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
        except (AttributeError, TypeError, ValueError, ArithmeticError) as e:
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
            # Make the rename itself durable.
            dir_fd = os.open(os.path.dirname(os.path.abspath(self.path)), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
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


_managed: tuple[float, dict[str, str] | None] = (-1.0, None)
_config_error: Any = None
_ignored_announced: set[str] = set()


def describe_config_error(e: BaseException) -> str:
    """Name a config read failure without echoing the file.

    A YAML error's text quotes the offending line, which can be the token line.
    """
    mark = getattr(e, "problem_mark", None)
    if mark is not None:
        return f"{type(e).__name__} at line {mark.line + 1}, column {mark.column + 1}"
    errno = getattr(e, "errno", None)
    return f"{type(e).__name__} (errno {errno})" if errno is not None else type(e).__name__


def managed_libraries() -> dict[str, str] | None:
    """Map each Plex library Kometa runs on to its config.yml ``libraries`` key.

    ``--run-libraries`` matches the key; the Plex name is the key unless the
    entry sets ``library_name``. Re-read when the file changes. A config that
    cannot be read keeps the last good map; with none, returns None so the
    trigger queues every library rather than none. Each failure is logged once.
    """
    global _managed, _config_error
    mtime = None
    try:
        mtime = os.stat(CONFIG_PATH).st_mtime
        if mtime == _managed[0]:
            return _managed[1]
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            libraries = (yaml.safe_load(fh) or {}).get("libraries") or {}
        mapping = {str((attrs.get("library_name") if isinstance(attrs, dict) else None) or key).strip(): str(key) for key, attrs in libraries.items()}
    except (OSError, yaml.YAMLError, AttributeError, TypeError) as e:
        state = (mtime, type(e).__name__)
        if state != _config_error:
            _config_error = state
            fallback = "keeping the last good library list" if _managed[1] is not None else "queueing arrivals from every library"
            log.error("cannot read the Kometa libraries from %s (%s); %s", CONFIG_PATH, describe_config_error(e), fallback)
        return _managed[1]
    _managed = (mtime, mapping)
    _config_error = None
    if not mapping:
        log.warning("%s lists no libraries: no arrival will be queued", CONFIG_PATH)
    else:
        log.info("Kometa libraries: %s", ", ".join(f"{name} ({key})" if name != key else key for name, key in sorted(mapping.items())))
    return mapping


def kometa_library(library: str) -> str | None:
    """The config key to run for a Plex library, or None if Kometa does not run on it."""
    managed = managed_libraries()
    if managed is None:
        return library
    return managed.get(library.strip())


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
    # Kometa would run on nothing for another library, exit 0, and look done.
    plex_library, library = library, kometa_library(library)
    if library is None:
        if plex_library not in _ignored_announced:
            _ignored_announced.add(plex_library)
            log.info("not queueing arrivals from %s: it is not a library in %s", plex_library, CONFIG_PATH)
        return None
    key = target_key(item)
    if not key or not pending.admit(library, str(item.ratingKey), key, added_at, now=now):
        return None
    via = f" via {item.type} {item.ratingKey}" if key != str(item.ratingKey) else ""
    log.info("queued %s (%s%s) from %s", key, getattr(item, "grandparentTitle", None) or item.title, via, library)
    return key


def processed_entries(data: dict) -> list[dict]:
    """The library timeline entries in a websocket notification that report a finished item."""
    if not isinstance(data, dict) or data.get("type") != "timeline":
        return []
    entries = data.get("TimelineEntry")
    if not isinstance(entries, list):
        return []
    return [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("identifier") == LIBRARY_IDENTIFIER and entry.get("state") == STATE_PROCESSED and entry.get("type") in TIMELINE_TYPES and entry.get("itemID") and entry.get("sectionID") not in (None, "", "-1")
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
        # Nothing here may escape: this is the only consumer of the inbox.
        try:
            failed = handle_notification(server, pending, data)
        except Exception as e:
            log.exception("notification handler failed: %s", e)
            try:
                failed = processed_entries(data)
            except Exception:
                failed = []
        try:
            if failed:
                retry_lookup(inbox, server, failed, attempt)
        except Exception as e:
            log.exception("could not schedule a lookup retry: %s", e)


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
        if not libtype or kometa_library(section.title) is None:
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

        # Bounds the TCP/TLS connect and handshake, so a hung connect ends this
        # thread instead of leaking it. Reads happen only once select reports
        # data, so an idle socket is not timed out by this.
        websocket.setdefaulttimeout(PLEX_TIMEOUT_SECONDS)
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


class _Busy:
    def __repr__(self) -> str:
        return "RUN_BUSY"


# A run's result when another Kometa run held the lock: neither success nor failure.
RUN_BUSY: Any = _Busy()


class _Stopped:
    def __repr__(self) -> str:
        return "RUN_STOPPED"

    def __bool__(self) -> bool:
        return False


# A queued collection run that was stopped (SIGTERM/SIGKILL sent) because an arrival was due.
RUN_STOPPED: Any = _Stopped()


class _Deferred:
    def __repr__(self) -> str:
        return "RUN_DEFERRED"

    def __bool__(self) -> bool:
        return False


# A queued collection run that never started: the item worker had work. No process was signalled, so
# it is not a stop and does not count toward BUILD_MAX_STOPS.
RUN_DEFERRED: Any = _Deferred()
# A stopped run gets SIGTERM; one still running this long after it gets SIGKILL.
STOP_GRACE_SECONDS = 30
# A queued collection run stopped this many times for arrivals runs to completion on its next pass:
# arrivals first, but a build longer than the gap between arrivals must still finish.
BUILD_MAX_STOPS = 3


def _spawn(command: list[str]) -> Any:
    # Its own session: Kometa does its work, and holds the run lock, in a ProcessPoolExecutor child.
    return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)  # nosec B603 - fixed argv


def _signal_group(proc: Any, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


class BuildGate:
    """Arrivals first. A queued collection run (up to RUN_TIMEOUT_SECONDS under the run lock) starts
    only while the item worker is waiting for work, and the worker stops one it finds running before
    it runs a batch. A stopped or deferred run stays queued, uncharged, and runs again once the worker is idle."""

    def __init__(self) -> None:
        self.woken = threading.Event()
        self._mu = threading.Lock()
        self._idle = False
        self._proc: Any = None
        self._protected = False
        self._stopped: Any = None
        self._stops: dict[tuple[str, str], int] = {}
        self._idle_hooks: list[Callable[[], None]] = []

    def on_idle(self, hook: Callable[[], None]) -> None:
        """Call ``hook`` each time the item worker goes idle (after its own wake)."""
        self._idle_hooks.append(hook)

    def note_stop(self, key: tuple[str, str]) -> int:
        with self._mu:
            self._stops[key] = self._stops.get(key, 0) + 1
            return self._stops[key]

    def stops(self, key: tuple[str, str]) -> int:
        with self._mu:
            return self._stops.get(key, 0)

    def forget(self, key: tuple[str, str]) -> None:
        with self._mu:
            self._stops.pop(key, None)

    def worker_idle(self) -> None:
        with self._mu:
            self._idle = True
        self.woken.set()
        for hook in self._idle_hooks:
            try:
                hook()
            except Exception as e:
                log.error("idle hook failed: %s", e)

    def worker_busy(self) -> None:
        with self._mu:
            self._idle = False
            proc = self._proc
            if proc is not None and proc.poll() is None and not self._protected:
                log.info("stopping the queued collection run: an arrival is due")
                self._stopped = proc
                _signal_group(proc, signal.SIGTERM)
                # Kometa has no SIGTERM handler, but nothing waits on a run that ignores it.
                backstop = threading.Timer(STOP_GRACE_SECONDS, self._kill_if_unfinished, (proc,))
                backstop.daemon = True
                backstop.start()

    def _kill_if_unfinished(self, proc: Any) -> None:
        with self._mu:
            if self._stopped is proc:
                log.warning("the stopped collection run outlived SIGTERM by %ds; killing it", STOP_GRACE_SECONDS)
                _signal_group(proc, signal.SIGKILL)

    def start(self, command: list[str], protected: bool = False) -> Any:
        """The started run, or None while the worker has work. A protected run is never stopped."""
        with self._mu:
            if not self._idle:
                return None
            self._proc = _spawn(command)
            self._protected = protected
            return self._proc

    def finish(self, proc: Any) -> bool:
        """The run ended. True when the worker stopped it."""
        with self._mu:
            if self._proc is proc:
                self._proc = None
                self._protected = False
            stopped = self._stopped is proc
            if stopped:
                self._stopped = None
            return stopped


GATE = BuildGate()


def run_command(command: list[str], gate: BuildGate | None = None, protected: bool = False) -> Any:
    """Run Kometa; the finished process (returncode, stdout, stderr), RUN_DEFERRED when ``gate``
    kept it from starting, or RUN_STOPPED when it stopped it. A timeout kills the whole process group (the child holding
    the run lock with it) and raises subprocess.TimeoutExpired."""
    proc = _spawn(command) if gate is None else gate.start(command, protected=protected)
    if proc is None:
        return RUN_DEFERRED
    try:
        try:
            stdout, stderr = proc.communicate(timeout=RUN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGKILL)
            proc.communicate()
            raise
    finally:
        stopped = gate is not None and gate.finish(proc)
    # A child left behind (the parent exited first) must not keep the run lock.
    _signal_group(proc, signal.SIGKILL)
    if stopped:
        return RUN_STOPPED
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


def wait_for_run_lock(path: str | None = None) -> bool:
    """Block until no Kometa run holds the run lock. Returns True when it actually had to wait.
    A shared lock is granted only once the holder's exclusive lock is released, so this waits on
    the kernel, not on a timer; it is released at once, leaving the lock to the next run."""
    with open(path or RUN_LOCK_PATH, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
            waited = False
        except OSError:
            log.info("a Kometa run holds the run lock; waiting for it to finish")
            fcntl.flock(fh, fcntl.LOCK_SH)
            waited = True
        fcntl.flock(fh, fcntl.LOCK_UN)
    return waited


def run_unless_busy(run: Callable[..., Any], *args: Any, wait: Callable[[], bool] | None = None) -> Any:
    """Run, and while the run reports the lock busy, wait for the lock and run again. A busy
    result is never charged as a failed attempt. Passes RUN_STOPPED and RUN_DEFERRED through; otherwise a bool."""
    wait = wait or wait_for_run_lock
    spurious = 0
    while True:
        result = run(*args)
        if result is RUN_STOPPED or result is RUN_DEFERRED:
            return result
        if result is not RUN_BUSY:
            return bool(result)
        try:
            waited = wait()
        except OSError as e:
            log.error("cannot wait on the run lock %s: %s; counting it as a failed run", RUN_LOCK_PATH, e)
            return False
        if not waited:
            spurious += 1
            if spurious >= MAX_SPURIOUS_BUSY:
                log.error("Kometa keeps reporting the run lock busy while nobody holds it (%s); counting it as a failed run", RUN_LOCK_PATH)
                return False


def run_kometa(library: str, rating_keys: list[str]) -> Any:
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
        result = run_command(command)
    except subprocess.TimeoutExpired:
        log.error("Kometa run for %s timed out after %ss", library, RUN_TIMEOUT_SECONDS)
        return False
    except OSError as e:
        log.error("could not start Kometa for %s: %s", library, e)
        return False
    if result.returncode == EXIT_LOCK_BUSY:
        return RUN_BUSY
    if result.returncode != 0:
        log.error("Kometa exited %s for %s: %s", result.returncode, library, result.stderr[-2000:])
        return False
    # Kometa exits 0 when --run-libraries matches nothing in the config. Retrying
    # cannot fix that, so it is settled - loudly - rather than requeued.
    if NO_LIBRARIES_MESSAGE in (result.stdout or "") + (result.stderr or ""):
        log.error("Kometa ran on nothing for %s: no library in %s has that key; not retrying %s", library, CONFIG_PATH, ",".join(rating_keys))
        return True
    log.info("finished operations for %s", library)
    return True


def release_arrivals(rating_keys: list[str], record_path: str | None = None) -> None:
    """Let the publisher send events a failed run held back: the item ran again and succeeded, ran
    out of retries, or left Plex. Never raises."""
    try:
        conn = live_record.connect(record_path or RECORD_PATH)
        try:
            for rating_key in rating_keys:
                if live_record.release_arrival(conn, rating_key):
                    log.warning("publishing the arrival of %s with the collections its runs managed", rating_key)
        finally:
            conn.close()
    except Exception as e:
        log.warning("could not release held arrivals %s: %s: %s", ",".join(rating_keys), type(e).__name__, e)


def _plex_ids(item: Any) -> dict[str, Any]:
    ids: dict[str, Any] = {"tmdb_id": None, "tvdb_id": None, "imdb_id": None}
    for guid in getattr(item, "guids", None) or []:
        scheme, _, value = str(getattr(guid, "id", "")).partition("://")
        if scheme in ("tmdb", "tvdb") and value.isdigit() and int(value) > 0:
            ids[f"{scheme}_id"] = ids[f"{scheme}_id"] or int(value)
        elif scheme == "imdb" and value.startswith("tt") and value[2:].isdigit():
            ids["imdb_id"] = ids["imdb_id"] or value
    return ids


def _utc_stamp(value: Any) -> str:
    seconds = epoch(value)
    moment = datetime.fromtimestamp(seconds if seconds is not None else time.time(), timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def plain_payload(item: Any) -> dict[str, Any] | None:
    """A kometa.item.arrived event with no collections, from Plex alone: for an item whose Kometa runs
    never reached the arrival step. None when the contract cannot carry it (no id, not a movie/show)."""
    media_type = getattr(item, "type", None)
    ids = _plex_ids(item)
    if media_type not in ("movie", "show") or not any(ids.values()):
        return None
    year = getattr(item, "year", None)
    return {
        "v": 1,
        "library": item.librarySectionTitle,
        "rating_key": str(item.ratingKey),
        "media_type": media_type,
        **ids,
        "title": item.title or str(item.ratingKey),
        "year": year if isinstance(year, int) and 1870 <= year <= 2200 else None,
        "added_at": _utc_stamp(getattr(item, "addedAt", None)),
        "arrived_at": _utc_stamp(None),
        "collections": [],
    }


def announce_unfinished(server: Any, rating_key: str, record_path: str | None = None) -> None:
    """Out of retries: the arrival is announced anyway. A held event goes out with what its runs
    managed; with no event at all (Kometa never reached the arrival step), a plain one from Plex.
    Never raises."""
    try:
        conn = live_record.connect(record_path or RECORD_PATH)
        try:
            if live_record.release_arrival(conn, rating_key):
                log.warning("publishing the arrival of %s with the collections its runs managed", rating_key)
                return
            if live_record.has_arrival(conn, rating_key):
                return
            item = fetch_item(server, rating_key)
            payload = None if item is None else plain_payload(item)
            if payload is None:
                log.error("cannot announce %s: not in Plex, or Plex has no TMDb, TVDb or IMDb id for it", rating_key)
                return
            if live_record.queue_arrival(conn, payload["library"], rating_key, payload):
                log.warning("Kometa never finished %s; announcing it with no collections", rating_key)
        finally:
            conn.close()
    except Exception as e:
        log.warning("could not announce %s: %s: %s", rating_key, type(e).__name__, e)


def give_up_or_retry(pending: Pending, library: str, rating_key: str, attempts: int, why: str, on_give_up: Callable[[str], None] | None = None) -> None:
    if attempts + 1 >= MAX_ATTEMPTS:
        log.warning("giving up on %s: %s after %d attempts", rating_key, why, attempts + 1)
        if on_give_up is not None:
            on_give_up(rating_key)
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
    run: Callable[[str, list[str]], Any] = run_kometa,
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
                release_arrivals([rating_key])
                continue
            if not is_matched(item):
                give_up_or_retry(pending, library, rating_key, attempts, "not matched yet", on_give_up=lambda key: announce_unfinished(server, key))
                continue
            target = target_key(item)
            if target:
                ready[target] = max(attempts, ready.get(target, 0))
        if not ready:
            continue
        if run_unless_busy(run, library, list(ready)):
            # A run that succeeded without re-queueing an event held by an earlier failure.
            release_arrivals(list(ready))
        else:
            for target, attempts in ready.items():
                give_up_or_retry(pending, library, target, attempts, "Kometa run failed", on_give_up=lambda key: announce_unfinished(server, key))
    pending.settle()


def run_build(library: str, mapping_name: str, gate: BuildGate | None = None) -> Any:
    """A scoped Kometa run of one collection (an arrival is waiting for it and it is not in Plex, or a
    list it is built from changed).
    RUN_STOPPED when an arrival stopped it, RUN_DEFERRED when it never started because the worker had work (``gate``, GATE by default)."""
    command = [
        "python3",
        KOMETA_PY,
        "-c",
        CONFIG_PATH,
        "--collections-only",
        "--run-libraries",
        library,
        "--run-collections",
        mapping_name,
        "--ignore-schedules",
        "--read-only-config",
    ]
    gate = gate or GATE
    stops = gate.stops((library, mapping_name))
    protected = stops >= BUILD_MAX_STOPS
    if protected:
        log.info("running collection %s in %s to completion: arrivals stopped it %d times, they wait for it this once", mapping_name, library, stops)
    else:
        log.info("running collection %s in %s (queued: an arrival is waiting on it, or a watched list changed)", mapping_name, library)
    try:
        result = run_command(command, gate=gate, protected=protected)
    except subprocess.TimeoutExpired:
        log.error("Kometa run of %s timed out after %ss", mapping_name, RUN_TIMEOUT_SECONDS)
        return False
    except OSError as e:
        log.error("could not start Kometa for %s: %s", mapping_name, e)
        return False
    if result is RUN_STOPPED or result is RUN_DEFERRED:
        return result
    if result.returncode == EXIT_LOCK_BUSY:
        return RUN_BUSY
    if result.returncode != 0:
        log.error("Kometa exited %s for collection %s: %s", result.returncode, mapping_name, result.stderr[-2000:])
        return False
    return True


def run_builds(record_path: str | None = None, run: Callable[[str, str], Any] = run_build) -> None:
    """Run every collection build the arrival step queued. Never raises."""
    try:
        conn = live_record.connect(record_path or RECORD_PATH)
    except Exception as e:
        log.warning("cannot open the live record for queued collection runs: %s: %s", type(e).__name__, e)
        return
    try:
        for build in live_record.pending_builds(conn):
            library, mapping_name = build["library"], build["mapping_name"]
            result = run_unless_busy(run, library, mapping_name)
            if result is RUN_DEFERRED:
                log.info("arrivals first: running %s once the item runs are done", mapping_name)
                return
            if result is RUN_STOPPED:
                stops = GATE.note_stop((library, mapping_name))
                log.info("arrivals first: running %s once the item runs are done (stopped %d of %d times)", mapping_name, stops, BUILD_MAX_STOPS)
                return
            GATE.forget((library, mapping_name))
            if result:
                live_record.finish_build(conn, library, mapping_name, build["gen"])
                log.info("built %s in %s", mapping_name, library)
            elif live_record.fail_build(conn, library, mapping_name, BUILD_MAX_ATTEMPTS, build["gen"]):
                log.error("giving up on running %s after %d attempts; the scheduled sweep builds it", mapping_name, BUILD_MAX_ATTEMPTS)
    except Exception as e:
        log.exception("queued collection runs failed: %s", e)
    finally:
        conn.close()


def builder(woken: threading.Event, stop: threading.Event | None = None, run_all: Callable[[], None] = run_builds) -> None:
    """Runs queued collection builds on its own thread, so arrivals never queue behind one. Starts
    with whatever a crash left queued, then sleeps until a batch queues more."""
    while True:
        try:
            run_all()
        except Exception as e:
            log.exception("queued collection runs failed: %s", e)
        if stop is not None and stop.is_set():
            return
        woken.wait()
        woken.clear()


def start_builder(gate: BuildGate = GATE) -> None:
    """The builder wakes each time the item worker goes idle (gate.worker_idle)."""
    threading.Thread(target=builder, args=(gate.woken,), daemon=True, name="collection-builder").start()


def worker_step(
    pending: Pending,
    connect: Callable[[], Any] = connect_plex,
    run: Callable[[str, list[str]], Any] = run_kometa,
    after: Callable[[], None] | None = None,
    gate: BuildGate | None = None,
) -> None:
    if gate is not None:
        gate.worker_idle()
    due = pending.wait_due()
    if gate is not None:
        # Stop a queued collection run first: it would hold the run lock for the whole batch.
        gate.worker_busy()
    try:
        process_due(pending, due, connect, run)
    except Exception as e:
        # Never let one bad batch kill the only thread that runs Kometa.
        log.exception("batch failed, requeueing it: %s", e)
        pending.restore_inflight()
    if after is not None:
        try:
            after()
        except Exception as e:
            log.exception("after-batch step failed: %s", e)


def worker(pending: Pending, connect: Callable[[], Any] = connect_plex, after: Callable[[], None] | None = None, gate: BuildGate | None = None) -> None:
    if after is not None:
        # Outbox rows a crash left behind. Queued collection runs start once this goes idle.
        after()
    while True:
        worker_step(pending, connect, after=after, gate=gate)


# Publisher.drain() results.
DRAIN_EMPTY = "empty"
DRAIN_RETRY = "retry"
DRAIN_WAIT = "wait"
# An ack that did not come in time is worth a retry on a backoff; nothing else is.
TIMEOUT_ERRORS = {"TimeoutError", "FlushTimeoutError"}
# NATS is down or the stream does not exist yet (plex-bot creates it): expected, waits for a wake.
# Any other failure (a permissions violation, a bad request) is a misconfiguration and logs ERROR.
NOT_YET_ERRORS = {
    "NoStreamResponseError",  # nats-py's JetStream publish with no stream (it re-raises NoRespondersError)
    "NoRespondersError",
    "NotFoundError",
    "ServiceUnavailableError",
    "ConnectionClosedError",
    "ConnectionDrainingError",
    "ConnectionReconnectingError",
    "NoServersError",
    "StaleConnectionError",
    "OutboundBufferLimitError",
}


def _refusal(e: BaseException) -> str | None:
    """The reason when the NATS server refused this client (authorization, permissions), else None.
    nats-py reports a publish permissions violation only to error_cb; the publish itself just gets
    no ack. The server's text names the subject, never a credential."""
    if type(e).__name__ == "AuthorizationError":
        return "authorization failed"
    text = str(e)
    if "Permissions Violation" in text or "Authorization Violation" in text:
        return text
    return None


class NatsClient:
    """The kometa-trigger NATS identity's connection handling: a connect with the seed, wake-ups
    from other threads, a sleep that ends on a wake or a deadline. One asyncio loop per client."""

    what = "events"

    def __init__(self, record_path: str, url: str, seed: str, connect: Callable[..., Any] | None = None):
        self.record_path = record_path
        self.url = url
        self._seed = seed
        self._connect = connect
        self.loop: asyncio.AbstractEventLoop | None = None
        self._woken: asyncio.Event | None = None
        self.nc: Any = None
        self._refused: str | None = None

    def wake(self) -> None:
        """Thread-safe: run the client's work now."""
        loop, woken = self.loop, self._woken
        if loop is not None and woken is not None:
            loop.call_soon_threadsafe(woken.set)

    async def _on_reconnect(self) -> None:
        log.info("NATS reconnected")
        if self._woken is not None:
            self._woken.set()

    async def _on_disconnect(self) -> None:
        log.warning("NATS disconnected; %s wait", self.what)

    async def _on_error(self, e: Exception) -> None:
        refused = _refusal(e)
        if refused:
            self._refused = refused
            log.error("NATS refused %s: %s", NATS_CLIENT_NAME, refused)
        else:
            log.warning("NATS error: %s", type(e).__name__)

    async def _open(self) -> Any:
        if self._connect is not None:
            return await self._connect()
        import nats

        return await nats.connect(
            servers=[self.url],
            name=NATS_CLIENT_NAME,
            nkeys_seed_str=self._seed,
            inbox_prefix=NATS_INBOX_PREFIX,
            allow_reconnect=True,
            max_reconnect_attempts=-1,
            connect_timeout=10,
            reconnected_cb=self._on_reconnect,
            disconnected_cb=self._on_disconnect,
            error_cb=self._on_error,
        )

    async def _sleep(self, timeout: float | None) -> None:
        """Until woken, or ``timeout`` seconds (None: until woken)."""
        woken = self._woken
        if woken is None:
            return
        try:
            await asyncio.wait_for(woken.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        woken.clear()

    async def _connect_forever(self, stop: threading.Event | None) -> bool:
        """Connect, retrying on a backoff. False when ``stop`` ended it first."""
        self.loop = asyncio.get_running_loop()
        self._woken = asyncio.Event()
        delay = PUBLISH_RETRY_MIN_SECONDS
        while self.nc is None:
            try:
                self.nc = await self._open()
            except Exception as e:
                # The type only: the client's errors are not worth risking next to a seed.
                log.log(logging.ERROR if _refusal(e) else logging.WARNING, "NATS connect to %s failed (%s); retrying in %ds", self.url, type(e).__name__, delay)
                await self._sleep(delay)
                delay = min(delay * 2, PUBLISH_RETRY_MAX_SECONDS)
            if stop is not None and stop.is_set():
                return False
        log.info("connected to NATS at %s as %s", self.url, NATS_CLIENT_NAME)
        return True

    async def _close(self) -> None:
        # A crash restarts main() on a new loop with a new connection: close this one.
        try:
            await self.nc.close()
        except Exception as e:
            log.warning("closing the NATS connection failed: %s", type(e).__name__)

    async def main(self, stop: threading.Event | None = None) -> None:
        raise NotImplementedError

    def run_forever(self) -> None:
        while True:
            try:
                asyncio.run(self.main())
            except Exception as e:
                log.exception("%s crashed, restarting: %s", type(self).__name__, e)
            self.loop = None
            self.nc = None
            time.sleep(PUBLISH_RETRY_MIN_SECONDS)


class Publisher(NatsClient):
    """Publishes the live record's arrival outbox to JetStream as kometa.item.arrived.

    Runs its own asyncio loop on a thread. It drains the outbox when woken (after every batch, on
    every NATS reconnect) and, after a failed publish, again on a backoff. A row is marked published
    only once JetStream acknowledged it, so a NATS outage or a stream that does not exist yet (the
    consumer, plex-bot, creates it) only delays an event, never drops it. Each event carries
    Nats-Msg-Id arrived:<library>:<ratingKey>, so a re-send inside the stream's duplicate window is
    stored once.
    """

    what = "arrival events wait in the outbox"

    async def drain(self, js: Any) -> str:
        """Publish every unpublished arrival, oldest first. DRAIN_EMPTY when the outbox is empty;
        DRAIN_RETRY when JetStream did not ack in time (worth a retry on a backoff); DRAIN_WAIT when
        nothing can take the event until something changes (no stream, NATS down). Failed rows stay
        queued."""
        # sqlite calls go to a worker thread: a busy record must not stall the NATS client's loop.
        try:
            conn = await asyncio.to_thread(live_record.connect, self.record_path, check_same_thread=False)
        except Exception as e:
            log.warning("cannot open the live record to publish arrivals: %s: %s", type(e).__name__, e)
            return DRAIN_RETRY
        try:
            while True:
                rows = await asyncio.to_thread(live_record.unpublished_arrivals, conn)
                if not rows:
                    return DRAIN_EMPTY
                for row in rows:
                    msg_id = f"arrived:{row['library']}:{row['rating_key']}"
                    body = json.dumps(row["payload"], sort_keys=True).encode()
                    try:
                        await js.publish(ARRIVED_SUBJECT, body, timeout=PUBLISH_TIMEOUT_SECONDS, headers={"Nats-Msg-Id": msg_id})
                    except Exception as e:
                        if type(e).__name__ in TIMEOUT_ERRORS or isinstance(e, (asyncio.TimeoutError, TimeoutError)):
                            if self._refused:
                                log.error("%s: no ack from JetStream; NATS refused %s (%s), fix its permissions; retrying on a backoff", msg_id, NATS_CLIENT_NAME, self._refused)
                            else:
                                log.warning("%s: no ack from JetStream (%s); retrying on a backoff", msg_id, type(e).__name__)
                            return DRAIN_RETRY
                        # No stream yet (NoRespondersError / 503), or the connection is down: nothing
                        # a timer would fix. The next reconnect, arrival or restart drains again.
                        level = logging.WARNING if type(e).__name__ in NOT_YET_ERRORS else logging.ERROR
                        log.log(level, "%s held in the outbox until NATS can take it: %s: %s", msg_id, type(e).__name__, e)
                        return DRAIN_WAIT
                    self._refused = None
                    unchanged = await asyncio.to_thread(live_record.mark_published, conn, row["library"], row["rating_key"], row["raw"])
                    log.info("published %s %s", ARRIVED_SUBJECT, msg_id)
                    if not unchanged:
                        log.warning("%s: a later run changed the event while it was being sent; the item is announced once, with what was sent", msg_id)
        except Exception as e:
            log.warning("publishing arrivals failed, will retry: %s: %s", type(e).__name__, e)
            return DRAIN_RETRY
        finally:
            conn.close()

    async def main(self, stop: threading.Event | None = None) -> None:
        if not await self._connect_forever(stop):
            return
        try:
            js = self.nc.jetstream()
            delay = PUBLISH_RETRY_MIN_SECONDS
            while stop is None or not stop.is_set():
                result = await self.drain(js)
                if result == DRAIN_RETRY:
                    await self._sleep(delay)
                    delay = min(delay * 2, PUBLISH_RETRY_MAX_SECONDS)
                else:
                    # Empty, or waiting on NATS: the next wake (an arrival, a reconnect) drains again.
                    delay = PUBLISH_RETRY_MIN_SECONDS
                    await self._sleep(None)
        finally:
            await self._close()


# queue_watched_list() results.
WATCH_QUEUED = "queued"
WATCH_DUPLICATE = "duplicate"
WATCH_UNKNOWN = "unknown"
WATCH_POISON = "poison"


def queue_watched_list(body: bytes, record_path: str | None = None) -> str:
    """Queue a scoped run of every collection built from the list a letterboxd.watchedlist.updated
    event names. One short transaction in the live record that also remembers (url, hash), so the
    same event again queues nothing. Raises only when the record cannot be written (the message is
    then left for redelivery)."""
    try:
        event = json.loads(body)
        url, content_hash = event["url"], event["hash"]
        if not (isinstance(url, str) and url and isinstance(content_hash, str) and content_hash):
            raise ValueError("url and hash must be non-empty strings")
    except (ValueError, KeyError, TypeError) as e:
        log.error("dropping an unreadable %s event (%s): %d bytes", WATCHED_SUBJECT, type(e).__name__, len(body or b""))
        return WATCH_POISON
    conn = live_record.connect(record_path or RECORD_PATH, check_same_thread=False)
    try:
        queued = live_record.queue_watch_builds(conn, url, content_hash)
    finally:
        conn.close()
    if queued is None:
        log.info("watched list %s (%s) was already handled; nothing to run", url, content_hash[:12])
        return WATCH_DUPLICATE
    if not queued:
        log.warning("watched list %s changed but no live_watch collection is built from it; nothing to run", url)
        return WATCH_UNKNOWN
    log.info("watched list %s changed (%s): queued %s", url, content_hash[:12], ", ".join(f"{b['mapping_name']} in {b['library']}" for b in queued))
    return WATCH_QUEUED


class WatchConsumer(NatsClient):
    """Consumes letterboxd.watchedlist.updated from the bridge's durable and queues the scoped runs.

    Pulls with a long request the server answers the moment a message exists. A message is
    acknowledged only after queue_watched_list returned (the run is in the record's durable builds
    queue); a record that cannot be written leaves it unacknowledged for the durable's backoff
    redelivery. An unreadable one is terminated (redelivery cannot fix it). The durable is the
    bridge's to create: until it exists the first pull fails (no responders) and this binds again on a
    backoff and says so.
    """

    what = "watched-list events wait on the durable"

    def __init__(self, record_path: str, url: str, seed: str, on_queued: Callable[[], None], connect: Callable[..., Any] | None = None):
        super().__init__(record_path, url, seed, connect)
        self.on_queued = on_queued
        # A pull answered (even with nothing to deliver) since the last rebind: the backoff starts over.
        self.pulled = False

    async def handle(self, msg: Any) -> None:
        try:
            outcome = await asyncio.to_thread(queue_watched_list, msg.data, self.record_path)
        except Exception as e:
            try:
                delivered = msg.metadata.num_delivered
            except Exception:
                delivered = 0
            last = delivered >= WATCH_MAX_DELIVER
            log.log(logging.ERROR if last else logging.WARNING, "could not queue a watched-list event (delivery %d of %d%s): %s: %s", delivered, WATCH_MAX_DELIVER, ", the last: it is dropped" if last else "; redelivered on a backoff", type(e).__name__, e)
            return
        if outcome == WATCH_POISON:
            await msg.term()
            return
        await msg.ack()
        # A duplicate is a redelivery after an ack that failed post-commit: its builds are queued but may never have woken the builder.
        if outcome in (WATCH_QUEUED, WATCH_DUPLICATE):
            self.on_queued()

    async def consume(self, sub: Any, stop: threading.Event | None) -> None:
        while stop is None or not stop.is_set():
            try:
                msgs = await sub.fetch(1, timeout=WATCH_PULL_SECONDS)
            except Exception as e:
                if type(e).__name__ in TIMEOUT_ERRORS or isinstance(e, (asyncio.TimeoutError, TimeoutError)):
                    continue  # the pull expired with nothing to deliver: ask again
                raise
            self.pulled = True
            for msg in msgs:
                await self.handle(msg)

    async def main(self, stop: threading.Event | None = None) -> None:
        if not await self._connect_forever(stop):
            return
        try:
            js = self.nc.jetstream()
            delay = PUBLISH_RETRY_MIN_SECONDS
            while stop is None or not stop.is_set():
                sub = None
                try:
                    sub = await js.pull_subscribe_bind(WATCH_DURABLE, stream=WATCH_STREAM)
                    await self.consume(sub, stop)
                except Exception as e:
                    # The durable not existing yet (the bridge creates it), NATS down, a refusal: bind again.
                    level = logging.WARNING if type(e).__name__ in NOT_YET_ERRORS or type(e).__name__ in TIMEOUT_ERRORS else logging.ERROR
                    log.log(level, "watched-list consumer %s on %s is not pulling (%s: %s); binding again in %ds", WATCH_DURABLE, WATCH_STREAM, type(e).__name__, e, delay)
                    if sub is not None:
                        try:
                            await sub.unsubscribe()  # a bind makes a new inbox subscription: drop this one
                        except Exception as unsub_error:
                            log.warning("could not drop the old pull subscription: %s", type(unsub_error).__name__)
                    if self.pulled:
                        delay, self.pulled = PUBLISH_RETRY_MIN_SECONDS, False
                    await self._sleep(delay)
                    delay = min(delay * 2, PUBLISH_RETRY_MAX_SECONDS)
        finally:
            await self._close()


def sync_collection_defs(record_path: str | None = None, config_path: str | None = None) -> tuple[list[tuple[str, str]], set[str], set[str]]:
    """Read the configured collection files and queue a scoped run of every collection that is new or whose definition
    changed. Returns (queued, held files, directories to watch). Raises when config.yml cannot be read or the record cannot be written."""
    config_path = config_path or CONFIG_PATH
    root = os.path.dirname(os.path.dirname(config_path))
    found, held, dirs = collection_defs.scan(config_path, root)
    conn = live_record.connect(record_path or RECORD_PATH, check_same_thread=False)
    try:
        queued = live_record.apply_collection_defs(conn, found, held_files=set(held))
    finally:
        conn.close()
    for path, why in sorted(held.items()):
        log.warning("collection file %s cannot be read right now (%s); its collections are judged on its next write", path, why)
    for library, name in queued:
        log.info("collection %s in %s is new or its definition changed: queued a scoped run", name, library)
    return queued, set(held), dirs


def collection_watcher(on_queued: Callable[[], None], quiet: float | None = None, watcher: Any = None, stop: threading.Event | None = None, gate: Any = None) -> None:
    """Notice collection-definition changes: an initial pass (seeding the record on the first complete read), then one pass per
    burst of writes. Event driven: it sleeps in select() on the inotify fd until a YAML file in a watched directory changes.
    A pass that fails (config unreadable, say) is run again at once after the config directories are watched, so a write that
    landed during the failure is not lost, and again each time the item worker goes idle while it is still failing (an event
    from the gate, not a timer). Never raises; an inotify failure backs off and is logged."""
    quiet = COLLECTION_QUIET_SECONDS if quiet is None else quiet
    try:
        watcher = watcher or collection_defs.Watcher(collection_defs.relevant_name)
    except Exception as e:
        log.error("cannot start the collection watcher (%s): new or edited collections wait for the scheduled sweep", e)
        return
    failing = threading.Event()
    if gate is not None:
        gate.on_idle(lambda: watcher.nudge() if failing.is_set() else None)
    config_dir = os.path.dirname(CONFIG_PATH)
    first = True
    retry_now = False
    waits_failed = 0
    while stop is None or not stop.is_set():
        queued: list[tuple[str, str]] = []
        if not first and not retry_now:
            try:
                watcher.wait(None)
                if not watcher.drain():
                    continue
                watcher.settle(quiet)
                waits_failed = 0
            except Exception as e:
                # inotify itself failing must not spin: back off (a stop request ends it), then try again.
                waits_failed += 1
                log.error("collection watcher read failed (%s); backing off", e)
                if stop is not None:
                    stop.wait(min(60.0, 2.0**waits_failed))
                else:
                    time.sleep(min(60.0, 2.0**waits_failed))
                continue
        first, retry_now = False, False
        try:
            queued, _held, dirs = sync_collection_defs()
            failing.clear()
            if dirs - watcher.dirs:
                # Watch before the next read: a write between this pass's read and the watch would otherwise be missed.
                watcher.watch(dirs)
                queued += sync_collection_defs()[0]
            for directory, errno in sorted(getattr(watcher, "failed", {}).items()):
                log.error("cannot watch %s (errno %s): writes there are not noticed", directory, errno)
        except Exception as e:
            log.error("collection definition pass failed (%s); retrying on the next write", describe_config_error(e))
            was_failing = failing.is_set()
            failing.set()
            # Without a readable config the collection directories are unknown: watch the config's own, and its subdirectories.
            try:
                watcher.watch({config_dir} | {entry.path for entry in os.scandir(config_dir) if entry.is_dir() and entry.name not in ("logs", "anidb_cache")})
            except Exception as watch_error:
                log.error("cannot watch %s: %s", config_dir, watch_error)
            # One immediate retry now that the directories are watched (a write during the failed read is otherwise absorbed).
            retry_now = not was_failing
        finally:
            if queued:
                on_queued()


def start_collection_watcher(on_queued: Callable[[], None]) -> None:
    threading.Thread(target=collection_watcher, args=(on_queued,), kwargs={"gate": GATE}, daemon=True, name="collection-defs").start()


def take_seed() -> str:
    """The NATS seed, removed from the environment so no Kometa run inherits it."""
    return os.environ.pop("NATS_NKEY_SEED", "").strip()


def start_publisher(seed: str | None = None) -> Publisher | None:
    """Start the outbox publisher. Without a seed, say so loudly: events stay in the outbox."""
    seed = take_seed() if seed is None else seed
    if not seed:
        log.error("NATS_NKEY_SEED is not set: kometa.item.arrived events stay in the outbox (%s) until it is", RECORD_PATH)
        return None
    publisher = Publisher(RECORD_PATH, NATS_URL, seed)
    threading.Thread(target=publisher.run_forever, daemon=True, name="arrival-publisher").start()
    return publisher


def start_watch_consumer(seed: str, on_queued: Callable[[], None]) -> WatchConsumer | None:
    """Start the watched-list consumer. Without a seed, say so loudly: watched lists are not rebuilt on change."""
    if not seed:
        log.error("NATS_NKEY_SEED is not set: %s events are not consumed, so a changed watched list is not rebuilt until the scheduled sweep", WATCHED_SUBJECT)
        return None
    consumer = WatchConsumer(RECORD_PATH, NATS_URL, seed, on_queued)
    threading.Thread(target=consumer.run_forever, daemon=True, name="watchedlist-consumer").start()
    return consumer


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
    seed = take_seed()
    publisher = start_publisher(seed)
    start_builder(GATE)
    start_watch_consumer(seed, GATE.woken.set)
    start_collection_watcher(GATE.woken.set)

    def after_batch() -> None:
        # The builder is woken when the worker goes idle; a batch that comes due stops its run.
        if publisher is not None:
            publisher.wake()

    threading.Thread(target=worker, args=(pending,), kwargs={"after": after_batch, "gate": GATE}, daemon=True, name="kometa-runner").start()
    listen_forever(pending)


if __name__ == "__main__":
    main()

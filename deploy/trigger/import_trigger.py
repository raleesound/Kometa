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
  added it within NEW_ITEM_WINDOW_SECONDS and we have not queued it before (a
  persisted seen-set keyed by rating key).
* **Shows, not episodes.** Library operations are defined per top-level item;
  an episode or season is queued as its show.
* **Debounce.** Kometa costs ~17s of startup per invocation regardless of how
  many items it is given, so a batch import should be one run, not twenty.
  Keys collected within DEBOUNCE_SECONDS of each other go out together.
* **Match verification.** An item can be processed before Plex's agent has
  matched it, so its GUID may still be ``local://``. Operations sourced from
  TMDb would find nothing and lock in that nothing. Unmatched keys are retried
  on a backoff instead.
* **Durable queue, no gaps.** Pending keys and the seen-set are written to
  disk, and every (re)connect sweeps the libraries for items added inside the
  window, so a restart or a dropped socket does not lose an import.
"""

from __future__ import annotations

import json
import logging
import os
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
RECONNECT_MIN_SECONDS = int(os.environ.get("RECONNECT_MIN_SECONDS", "5"))
RECONNECT_MAX_SECONDS = int(os.environ.get("RECONNECT_MAX_SECONDS", "300"))
# A connection that stayed up this long was healthy; reset the backoff after it.
STABLE_CONNECTION_SECONDS = 60
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
    """Rating keys waiting to be sent to Kometa, grouped by library, plus the
    seen-set that stops a refresh of an already-queued item from re-queueing it."""

    def __init__(self, path: str):
        self.path = path
        self.cond = threading.Condition()
        self.due_at = 0.0
        # {library: {rating_key: attempts}}
        self.items: dict[str, dict[str, int]] = {}
        # {source rating_key: addedAt epoch seconds}
        self.seen: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        self.items = {lib: {str(k): int(v) for k, v in keys.items()} for lib, keys in data.get("items", {}).items()}
        self.items = {lib: keys for lib, keys in self.items.items() if keys}
        self.seen = {str(k): float(v) for k, v in data.get("seen", {}).items()}
        if self.items:
            # Anything recovered from disk was already accepted; send it promptly
            # rather than waiting for another event to start the clock.
            self.due_at = time.time() + DEBOUNCE_SECONDS
            log.info("recovered %d pending items from %s", sum(len(v) for v in self.items.values()), self.path)

    def _save_locked(self) -> None:
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"items": self.items, "seen": self.seen}, fh)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("could not persist queue: %s", e)

    def claim(self, source_key: str, added_at: float, now: float | None = None) -> bool:
        """Record an import as handled. False if it already was, or is too old to be new."""
        now = time.time() if now is None else now
        cutoff = now - NEW_ITEM_WINDOW_SECONDS
        with self.cond:
            self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}
            # A future addedAt is a pin (this server parks ~20 old films at 2097
            # to hold them atop Recently Added), never an import.
            if added_at < cutoff or added_at > now + CLOCK_SKEW_SECONDS or str(source_key) in self.seen:
                return False
            self.seen[str(source_key)] = added_at
            self._save_locked()
            return True

    def forget(self, source_key: str) -> None:
        """Let a later event for this item queue it again (used when we gave up on it)."""
        with self.cond:
            if self.seen.pop(str(source_key), None) is not None:
                self._save_locked()

    def add(self, library: str, rating_key: str) -> None:
        with self.cond:
            self.items.setdefault(library, {}).setdefault(str(rating_key), 0)
            self.due_at = time.time() + DEBOUNCE_SECONDS
            self._save_locked()
            self.cond.notify_all()

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
        self._save_locked()
        return taken

    def wait_due(self) -> dict[str, dict[str, int]]:
        """Block until a batch is due. Wakes on add/requeue or at the deadline, never on a timer."""
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
    if not pending.claim(str(item.ratingKey), added_at, now=now):
        return None
    key = target_key(item)
    if not key:
        return None
    pending.add(library, key)
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


def handle_notification(server: Any, pending: Pending, data: dict) -> None:
    for entry in processed_entries(data):
        try:
            library = server.library.sectionByID(int(entry["sectionID"])).title
            item = fetch_item(server, str(entry["itemID"]))
        except Exception as e:  # one bad entry must not kill the listener thread
            log.warning("could not look up %s %s: %s", TIMELINE_TYPES[entry["type"]], entry["itemID"], e)
            continue
        if item is not None:
            consider(pending, library, item)


def catch_up(server: Any, pending: Pending, now: float | None = None) -> int:
    """Queue anything added inside the window that we have not seen: covers restarts and dropped sockets."""
    now = time.time() if now is None else now
    since = datetime.fromtimestamp(now - NEW_ITEM_WINDOW_SECONDS)
    queued = 0
    for section in server.library.sections():
        libtype = SECTION_LIBTYPES.get(section.type)
        if not libtype:
            continue
        for item in section.search(libtype=libtype, filters={"addedAt>>": since}):
            if consider(pending, section.title, item, now=now):
                queued += 1
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

    def _message(self, _ws: Any, message: str) -> None:
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
        self.ws = websocket.WebSocketApp(url, on_message=self._message, on_error=self._error)
        # Pings detect a half-open socket; run_forever returns on close or error.
        self.ws.run_forever(ping_interval=self.PING_INTERVAL, ping_timeout=self.PING_TIMEOUT)


def listen_forever(
    pending: Pending,
    connect: Callable[[], Any] = connect_plex,
    listener_cls: type = Listener,
    stop: threading.Event | None = None,
) -> None:
    """Hold a websocket session open; on a drop, reconnect (with backoff) and catch up."""
    stop = stop or threading.Event()
    delay = RECONNECT_MIN_SECONDS
    while not stop.is_set():
        started = time.time()
        try:
            server = connect()
            listener = listener_cls(server, lambda data, server=server: handle_notification(server, pending, data))
            thread = threading.Thread(target=listener.run, daemon=True, name="plex-websocket")
            thread.start()
            log.info("listening to Plex websocket (debounce %ds, new-item window %ds)", DEBOUNCE_SECONDS, NEW_ITEM_WINDOW_SECONDS)
            # After the socket is up, so nothing added in between is missed. A
            # failed sweep must not abandon the live socket (a second listener
            # would double-queue), so it is contained here.
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
    if result.returncode != 0:
        log.error("Kometa exited %s for %s: %s", result.returncode, library, result.stderr[-2000:])
        return False
    log.info("finished operations for %s", library)
    return True


def process_due(
    pending: Pending,
    due: dict[str, dict[str, int]],
    connect: Callable[[], Any] = connect_plex,
    run: Callable[[str, list[str]], bool] = run_kometa,
) -> None:
    try:
        server = connect()
    except Exception as e:
        log.warning("Plex unreachable, holding %d item(s): %s", sum(len(k) for k in due.values()), e)
        for library, keys in due.items():
            for rating_key, attempts in keys.items():
                pending.requeue(library, rating_key, attempts)
        return
    for library, keys in due.items():
        ready: list[str] = []
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
                continue
            if not is_matched(item):
                if attempts + 1 >= MAX_ATTEMPTS:
                    log.warning("giving up on %s: still unmatched after %d attempts", rating_key, attempts + 1)
                    # A later match fires another processed event; let it queue the item then.
                    pending.forget(rating_key)
                    continue
                log.info("%s not matched yet, retrying (attempt %d)", rating_key, attempts + 1)
                pending.requeue(library, rating_key, attempts + 1)
                continue
            target = target_key(item)
            if target and target not in ready:
                ready.append(target)
        if ready:
            run(library, ready)


def worker(pending: Pending, connect: Callable[[], Any] = connect_plex) -> None:
    while True:
        process_due(pending, pending.wait_due(), connect)


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

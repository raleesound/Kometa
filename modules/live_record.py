"""Collection membership record for live Kometa (epic #5634, slice S2).

After each collection runs, Kometa writes what the collection now holds and what it still
wants into a SQLite file next to config.yml. The arrival handler (S3) reads it to add a
newly imported film to every collection waiting on it, in its recorded position, without
waiting for the next scheduled sweep.

Two processes write this file: the scheduled/scoped Kometa run and the import trigger. Every
Kometa run (sweep, scoped or item run) holds the run lock, so their writes never interleave; the
trigger process itself only drains the arrival outbox and the scoped-build queue.
WAL mode plus a busy timeout lets them interleave; every write is one short transaction.
Only the stdlib sqlite3 module is used.
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

DB_NAME = "live-collections.db"
# Bump on any schema change: an older record is dropped and rebuilt (it is derived data; the next
# sweep repopulates it).
SCHEMA_VERSION = 3
TABLES = ("collections", "members", "wanted", "history", "arrivals", "builds")
# Versions that can be upgraded in place (version -> statements bringing it to version + 1). Older
# versions are dropped and rebuilt. In-place matters: a dropped record leaves the arrival handler
# blind until the next sweep repopulates it.
MIGRATIONS = {
    2: (
        "ALTER TABLE members ADD COLUMN source TEXT",
        "ALTER TABLE members ADD COLUMN added_at TEXT",
        "ALTER TABLE history ADD COLUMN source TEXT",
    ),
}
# Readers (the arrival handler) can afford to wait; a Kometa run must not stall on a held lock,
# so its writes use a short timeout and give up for the rest of the run after repeated failures.
BUSY_TIMEOUT_MS = 30000
RUN_WRITE_TIMEOUT_MS = 2000
RUN_FAILURE_LIMIT = 3
HISTORY_KEEP = 1000
# Nothing is ever deleted because a collection was absent from one run: a file can report "Loaded
# Successfully" after dropping a collection (a failed dynamic section, a duplicate name, a bad entry).
# Instead every row carries last_synced. The scheduled sweep is daily, so a configured collection is
# never older than a day; past STALE_AFTER it stops being returned, past EXPIRE_AFTER it is deleted.
STALE_AFTER = timedelta(days=3)
EXPIRE_AFTER = timedelta(days=30)

SCHEMA = """
CREATE TABLE IF NOT EXISTS collections (
    library TEXT NOT NULL,
    name TEXT NOT NULL,
    mapping_name TEXT,
    source_file TEXT,
    source_path TEXT,
    id_type TEXT,
    built INTEGER NOT NULL,
    present INTEGER NOT NULL,
    kind TEXT NOT NULL,
    members_tracked INTEGER NOT NULL,
    missing_checked INTEGER NOT NULL,
    collection_order TEXT,
    order_recorded INTEGER NOT NULL,
    order_recorded_at TEXT,
    last_synced TEXT NOT NULL,
    last_status TEXT NOT NULL DEFAULT 'ok',
    PRIMARY KEY (library, name)
);
CREATE TABLE IF NOT EXISTS members (
    library TEXT NOT NULL,
    name TEXT NOT NULL,
    rating_key INTEGER NOT NULL,
    ext_id INTEGER,
    position INTEGER,
    source TEXT,
    added_at TEXT,
    PRIMARY KEY (library, name, rating_key)
);
CREATE TABLE IF NOT EXISTS wanted (
    library TEXT NOT NULL,
    name TEXT NOT NULL,
    ext_id INTEGER NOT NULL,
    filtered INTEGER,
    held_back INTEGER NOT NULL,
    position INTEGER,
    PRIMARY KEY (library, name, ext_id)
);
CREATE INDEX IF NOT EXISTS wanted_by_id ON wanted (ext_id);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library TEXT NOT NULL,
    name TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    event TEXT NOT NULL,
    rating_key INTEGER,
    ext_id INTEGER,
    source TEXT
);
CREATE INDEX IF NOT EXISTS history_by_collection ON history (library, name, id);
"""
# Tables added after v2. CREATE IF NOT EXISTS, so they double as the v2 -> v3 migration.
SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS arrivals (
    library TEXT NOT NULL,
    rating_key INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    complete INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (library, rating_key)
);
CREATE INDEX IF NOT EXISTS arrivals_unpublished ON arrivals (published_at, created_at);
CREATE TABLE IF NOT EXISTS builds (
    library TEXT NOT NULL,
    mapping_name TEXT NOT NULL,
    source_file TEXT,
    requested_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (library, mapping_name)
);
"""

MEMBER_ADDED = "member_added"
MEMBER_REMOVED = "member_removed"
WANTED_ADDED = "wanted_added"
WANTED_REMOVED = "wanted_removed"
# Who changed a collection: a Kometa collection run, or the arrival handler (item run, S3).
SOURCE_RUN = "run"
SOURCE_TRIGGER = "trigger"


def db_path(config_dir):
    return os.path.join(config_dir, DB_NAME)


def _set_wal(conn, timeout_ms):
    # SQLite does not reliably apply the busy handler while two connections race to switch a
    # brand-new file into WAL, so that one step retries on "locked" until the same deadline.
    # WAL is persistent: once set, later opens return on the first check.
    deadline = time.monotonic() + timeout_ms / 1000
    delay = 0.01
    while True:
        try:
            if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
                return
            mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
            if mode != "wal":
                raise sqlite3.OperationalError(f"journal_mode stayed {mode}")
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


def connect(path, timeout_ms=BUSY_TIMEOUT_MS, log=None, check_same_thread=True):
    """Open the record, creating the schema on first use. A record from an older schema version is
    upgraded in place when MIGRATIONS covers it, otherwise dropped and rebuilt (logged to ``log`` when given). Autocommit; callers open their own transactions."""
    conn = sqlite3.connect(path, timeout=timeout_ms / 1000, isolation_level=None, check_same_thread=check_same_thread)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(timeout_ms)}")
        _set_wal(conn, timeout_ms)
        if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            # executescript() would commit our BEGIN IMMEDIATE, so run the statements one by one.
            conn.execute("BEGIN IMMEDIATE")
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version in MIGRATIONS:
                    while version < SCHEMA_VERSION:
                        for statement in MIGRATIONS[version]:
                            conn.execute(statement)
                        version += 1
                    _create(conn)
                    if log is not None:
                        log.info(f"Live Record: upgraded the record in place to schema v{SCHEMA_VERSION}")
                elif version < SCHEMA_VERSION:
                    if version > 0:
                        for table in TABLES:
                            conn.execute(f"DROP TABLE IF EXISTS {table}")  # nosec B608 - table names are constants
                        if log is not None:
                            log.warning(f"Live Record: schema v{version} is out of date, dropped the record and rebuilt it as v{SCHEMA_VERSION}; the next sweep repopulates it")
                    _create(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    except Exception:
        conn.close()
        raise
    return conn


def _create(conn):
    for statement in (SCHEMA + SCHEMA_V3).split(";"):
        if statement.strip():
            conn.execute(statement)


def _now():
    return _stamp(datetime.now(timezone.utc))


def _stamp(moment):
    # One fixed UTC format so stamps compare correctly as text in SQL.
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _trim_history(conn, library, name):
    conn.execute(
        "DELETE FROM history WHERE library = ? AND name = ? AND id <= (SELECT id FROM history WHERE library = ? AND name = ? ORDER BY id DESC LIMIT 1 OFFSET ?)",
        (library, name, library, name, HISTORY_KEEP),
    )


def _bool_or_none(value):
    return None if value is None else int(bool(value))


def write_collection(conn, snapshot):
    """Replace one collection's record with ``snapshot`` and append the diff to its history.

    snapshot keys: library, name, mapping_name, source_file, source_path, id_type, built, present,
    kind, missing_checked, collection_order, order_recorded,
    members (None = not tracked, else list of (rating_key, ext_id, position)),
    previous_members (list of (rating_key, ext_id) the collection held before this run, or None if
    unknown; used only when there is no earlier record to diff against),
    wanted (list of (ext_id, filtered, held_back, position); filtered is True/False, or None when
    run_missing never evaluated the id),
    observed_at (optional aware datetime: when this run started reading the collection from Plex).

    When this run could not establish a custom order (sort failed or skipped) the last good order
    is carried over by id instead of being erased.

    A snapshot never reverts a newer arrival: a member the arrival handler added after
    ``observed_at`` is kept (and its id is not put back in wanted) even though this run did not see
    it. Without ``observed_at`` the snapshot is taken as current and replaces everything.
    """
    library, name = snapshot["library"], snapshot["name"]
    members = None if snapshot["members"] is None else [list(m) for m in snapshot["members"]]
    wanted = [list(w) for w in snapshot["wanted"]]
    order_recorded = bool(snapshot["order_recorded"])
    now = _now()
    observed_at = snapshot.get("observed_at")
    conn.execute("BEGIN IMMEDIATE")
    try:
        newer = []
        if observed_at is not None:
            newer = conn.execute(
                "SELECT rating_key, ext_id, position, added_at FROM members WHERE library = ? AND name = ? AND source = ? AND added_at > ?",
                (library, name, SOURCE_TRIGGER, _stamp(observed_at)),
            ).fetchall()
        if newer:
            newer_ids = {row["ext_id"] for row in newer if row["ext_id"] is not None}
            wanted = [w for w in wanted if w[0] not in newer_ids]
            if members is not None:
                seen = {m[0] for m in members}
                members.extend([row["rating_key"], row["ext_id"], row["position"]] for row in newer if row["rating_key"] not in seen)
        kept = {row["rating_key"]: row["added_at"] for row in newer}
        prior = conn.execute("SELECT members_tracked, collection_order, order_recorded, order_recorded_at FROM collections WHERE library = ? AND name = ?", (library, name)).fetchone()
        old_members = None
        if prior is not None and prior["members_tracked"]:
            old_members = {row["rating_key"]: row["ext_id"] for row in conn.execute("SELECT rating_key, ext_id FROM members WHERE library = ? AND name = ?", (library, name))}
        elif prior is None and snapshot.get("previous_members") is not None:
            old_members = {rk: ext_id for rk, ext_id in snapshot["previous_members"]}
        old_wanted = None
        if prior is not None:
            old_wanted = {row["ext_id"] for row in conn.execute("SELECT ext_id FROM wanted WHERE library = ? AND name = ?", (library, name))}

        order_recorded_at = now if order_recorded else None
        if not order_recorded and prior is not None and prior["order_recorded"] and snapshot["collection_order"] and prior["collection_order"] == snapshot["collection_order"]:
            carried = {}
            for row in conn.execute(
                "SELECT ext_id, position FROM members WHERE library = ? AND name = ? AND position IS NOT NULL AND ext_id IS NOT NULL UNION ALL SELECT ext_id, position FROM wanted WHERE library = ? AND name = ? AND position IS NOT NULL",
                (library, name, library, name),
            ):
                carried[row["ext_id"]] = row["position"]
            for row in members or []:
                if row[2] is None and row[1] in carried:
                    row[2] = carried[row[1]]
            for row in wanted:
                if row[3] is None and row[0] in carried:
                    row[3] = carried[row[0]]
            order_recorded = True
            order_recorded_at = prior["order_recorded_at"]

        events = []
        if members is not None and old_members is not None:
            new_members = {rk: ext_id for rk, ext_id, _ in members}
            events.extend((MEMBER_ADDED, rk, new_members[rk]) for rk in new_members if rk not in old_members)
            events.extend((MEMBER_REMOVED, rk, old_members[rk]) for rk in old_members if rk not in new_members)
        new_wanted = {w[0] for w in wanted}
        if old_wanted is not None:
            events.extend((WANTED_ADDED, None, ext_id) for ext_id in sorted(new_wanted - old_wanted))
            events.extend((WANTED_REMOVED, None, ext_id) for ext_id in sorted(old_wanted - new_wanted))

        conn.execute(
            "INSERT OR REPLACE INTO collections (library, name, mapping_name, source_file, source_path, id_type, built, present, kind, members_tracked, missing_checked, collection_order, order_recorded, order_recorded_at, last_synced, last_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                library,
                name,
                snapshot.get("mapping_name", name),
                snapshot["source_file"],
                snapshot["source_path"],
                snapshot["id_type"],
                int(bool(snapshot["built"])),
                int(bool(snapshot["present"])),
                snapshot["kind"],
                int(members is not None),
                int(bool(snapshot["missing_checked"])),
                snapshot["collection_order"],
                int(order_recorded),
                order_recorded_at,
                now,
                "ok" if snapshot.get("sync_ok", True) else "failed",
            ),
        )
        conn.execute("DELETE FROM members WHERE library = ? AND name = ?", (library, name))
        if members:
            conn.executemany(
                "INSERT OR REPLACE INTO members (library, name, rating_key, ext_id, position, source, added_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(library, name, rk, ext_id, pos, SOURCE_TRIGGER if rk in kept else SOURCE_RUN, kept.get(rk)) for rk, ext_id, pos in members],
            )
        conn.execute("DELETE FROM wanted WHERE library = ? AND name = ?", (library, name))
        conn.executemany(
            "INSERT OR REPLACE INTO wanted (library, name, ext_id, filtered, held_back, position) VALUES (?, ?, ?, ?, ?, ?)",
            [(library, name, ext_id, _bool_or_none(filtered), int(bool(held)), pos) for ext_id, filtered, held, pos in wanted],
        )
        conn.executemany("INSERT INTO history (library, name, recorded_at, event, rating_key, ext_id, source) VALUES (?, ?, ?, ?, ?, ?, ?)", [(library, name, now, event, rk, ext_id, SOURCE_RUN) for event, rk, ext_id in events])
        _trim_history(conn, library, name)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return events


def mark_deleted(conn, library, mapping_name):
    """A collection Kometa deleted from Plex (delete_not_scheduled): it holds nothing and waits on nothing.
    Returns the record names updated."""
    now = _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        names = [row["name"] for row in conn.execute("SELECT name FROM collections WHERE library = ? AND mapping_name = ?", (library, mapping_name)).fetchall()]
        for name in names:
            events = [(MEMBER_REMOVED, row["rating_key"], row["ext_id"]) for row in conn.execute("SELECT rating_key, ext_id FROM members WHERE library = ? AND name = ? ORDER BY rating_key", (library, name))]
            events += [(WANTED_REMOVED, None, row["ext_id"]) for row in conn.execute("SELECT ext_id FROM wanted WHERE library = ? AND name = ? ORDER BY ext_id", (library, name))]
            conn.execute("UPDATE collections SET present = 0, members_tracked = 1, last_synced = ? WHERE library = ? AND name = ?", (now, library, name))
            conn.execute("DELETE FROM members WHERE library = ? AND name = ?", (library, name))
            conn.execute("DELETE FROM wanted WHERE library = ? AND name = ?", (library, name))
            conn.executemany("INSERT INTO history (library, name, recorded_at, event, rating_key, ext_id) VALUES (?, ?, ?, ?, ?, ?)", [(library, name, now, event, rk, ext_id) for event, rk, ext_id in events])
            _trim_history(conn, library, name)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return names


def touch(conn, library, mapping_name):
    """The run reached this collection but its schedule skipped it today: it is still configured, so
    refresh last_synced on its newest record and change nothing else. Only the newest row per mapping
    name is touched, so a renamed collection's old row still goes stale once the new title syncs.
    Returns the record name touched, or None."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT name, last_status FROM collections WHERE library = ? AND mapping_name = ? ORDER BY last_synced DESC, name LIMIT 1", (library, mapping_name)).fetchone()
        if row is not None and row["last_status"] != "ok":
            row = None  # its last real sync failed: let it go stale rather than vouch for it
        if row is not None:
            conn.execute("UPDATE collections SET last_synced = ? WHERE library = ? AND name = ?", (_now(), library, row["name"]))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return None if row is None else row["name"]


def mark_failed(conn, library, mapping_name):
    """The collection's run failed before its membership settled: flag its newest record so schedule
    skips stop refreshing it. last_synced is left alone. Returns the record name flagged, or None."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT name FROM collections WHERE library = ? AND mapping_name = ? ORDER BY last_synced DESC, name LIMIT 1", (library, mapping_name)).fetchone()
        if row is not None:
            conn.execute("UPDATE collections SET last_status = 'failed' WHERE library = ? AND name = ?", (library, row["name"]))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return None if row is None else row["name"]


def expire(conn, max_age=EXPIRE_AFTER, now=None):
    """Delete records (with their members, wanted ids and history) not synced for ``max_age``.
    Kometa calls this only at the end of a full sweep. Returns the (library, name) pairs deleted."""
    cutoff = _stamp((now or datetime.now(timezone.utc)) - max_age)
    conn.execute("BEGIN IMMEDIATE")
    try:
        expired = [(row["library"], row["name"]) for row in conn.execute("SELECT library, name FROM collections WHERE last_synced < ? ORDER BY library, name", (cutoff,)).fetchall()]
        for library, name in expired:
            for table in ("collections", "members", "wanted", "history"):
                conn.execute(f"DELETE FROM {table} WHERE library = ? AND name = ?", (library, name))  # nosec B608 - table names are constants
        # Published arrivals are kept: they are what stops a show announcing again on its next episode.
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return expired


# ── Read API (used by the arrival handler, S3) ─────────────────────────


def find_waiting(conn, id_type, ext_id, include_unknown=True, stale_after=STALE_AFTER, now=None):
    """Every built collection still waiting on this TMDb ('tmdb') or TVDb ('tvdb') id.

    An id run_missing filtered out is never returned. ``filter_state`` is 'passed' when run_missing
    evaluated the id and its filters accepted it, 'unknown' when it was never evaluated (missing
    processing off, or the TMDb/TVDb lookup failed). Running the collection again re-applies its
    filters, so callers that rebuild the collection can act on 'unknown'; pass
    ``include_unknown=False`` to get only confirmed ids.

    A collection not synced within ``stale_after`` is never returned: it is no longer configured,
    or its run keeps failing, and either way the record is no longer evidence of what it wants.

    Rows: library, name, mapping_name, source_file, source_path, present (the Plex collection
    exists), collection_order, order_recorded, position, held_back, filter_state, last_synced,
    age_seconds (since last_synced).
    """
    moment = now or datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT c.library, c.name, c.mapping_name, c.source_file, c.source_path, c.present, c.collection_order, c.order_recorded, c.last_synced, w.position, w.held_back, "
        "CASE WHEN w.filtered IS NULL THEN 'unknown' ELSE 'passed' END AS filter_state "
        "FROM wanted w JOIN collections c ON c.library = w.library AND c.name = w.name "
        "WHERE w.ext_id = ? AND c.id_type = ? AND c.built = 1 AND c.last_synced >= ? AND (w.filtered = 0 OR (w.filtered IS NULL AND ?)) ORDER BY c.library, c.name",
        (int(ext_id), id_type, _stamp(moment - stale_after), int(bool(include_unknown))),
    )
    found = []
    for row in rows:
        entry = dict(row)
        entry["age_seconds"] = max(0, int((moment - datetime.fromisoformat(entry["last_synced"])).total_seconds()))
        found.append(entry)
    return found


def get_position(conn, library, name, ext_id):
    """Recorded custom-order position of an id (member or wanted) in a collection, or None."""
    row = conn.execute(
        "SELECT position FROM wanted WHERE library = ? AND name = ? AND ext_id = ? AND position IS NOT NULL UNION ALL SELECT position FROM members WHERE library = ? AND name = ? AND ext_id = ? AND position IS NOT NULL LIMIT 1",
        (library, name, int(ext_id), library, name, int(ext_id)),
    ).fetchone()
    return None if row is None else row["position"]


def slot_after(conn, library, name, position):
    """Rating key of the member that should precede an item at ``position`` (None means the start)."""
    row = conn.execute(
        "SELECT rating_key FROM members WHERE library = ? AND name = ? AND position IS NOT NULL AND position < ? ORDER BY position DESC, rating_key DESC LIMIT 1",
        (library, name, position),
    ).fetchone()
    return None if row is None else row["rating_key"]


def positions(conn, library, name):
    """Recorded custom-order positions of a collection, members and wanted ids alike: {ext_id: position}."""
    rows = conn.execute(
        "SELECT ext_id, position FROM members WHERE library = ? AND name = ? AND ext_id IS NOT NULL AND position IS NOT NULL " "UNION ALL SELECT ext_id, position FROM wanted WHERE library = ? AND name = ? AND position IS NOT NULL",
        (library, name, library, name),
    )
    return {row["ext_id"]: row["position"] for row in rows}


def get_members(conn, library, name):
    """Members in recorded order (positioned members first, by position)."""
    rows = conn.execute("SELECT rating_key, ext_id, position FROM members WHERE library = ? AND name = ? ORDER BY position IS NULL, position, rating_key", (library, name))
    return [dict(row) for row in rows]


def get_collection(conn, library, name):
    row = conn.execute("SELECT * FROM collections WHERE library = ? AND name = ?", (library, name)).fetchone()
    return None if row is None else dict(row)


def get_history(conn, library, name, limit=200):
    rows = conn.execute("SELECT recorded_at, event, rating_key, ext_id, source FROM history WHERE library = ? AND name = ? ORDER BY id DESC LIMIT ?", (library, name, limit))
    return [dict(row) for row in rows]


# ── Arrival API (the item run writes, the import trigger drains; S3) ───


def record_arrival(conn, library, name, rating_key, ext_id, position=None, now=None):
    """The arrival handler put ``rating_key`` into collection ``name``: record it as a member, drop
    its id from wanted and log a member_added event with source=trigger. Idempotent: an item already
    recorded as a member changes nothing. Returns True when the record changed."""
    stamp = _stamp(now or datetime.now(timezone.utc))
    conn.execute("BEGIN IMMEDIATE")
    try:
        exists = conn.execute("SELECT 1 FROM members WHERE library = ? AND name = ? AND rating_key = ?", (library, name, int(rating_key))).fetchone() is not None
        dropped = conn.execute("DELETE FROM wanted WHERE library = ? AND name = ? AND ext_id = ?", (library, name, int(ext_id))).rowcount
        if not exists:
            conn.execute(
                "INSERT INTO members (library, name, rating_key, ext_id, position, source, added_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (library, name, int(rating_key), int(ext_id), position, SOURCE_TRIGGER, stamp),
            )
            conn.execute(
                "INSERT INTO history (library, name, recorded_at, event, rating_key, ext_id, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (library, name, stamp, MEMBER_ADDED, int(rating_key), int(ext_id), SOURCE_TRIGGER),
            )
            _trim_history(conn, library, name)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return not exists or bool(dropped)


def _merge_attempts(old, new):
    """A retry no longer sees what an earlier, failed attempt finished (those collections stopped
    waiting, or it finds the item already a member). Keep the earlier attempt's collections and
    its "added": the event reports what the trigger did across all attempts."""
    if "collections" not in new:
        return new
    before = {entry["name"]: entry for entry in old.get("collections", [])}
    merged = []
    for entry in new.get("collections", []):
        earlier = before.pop(entry["name"], None)
        if earlier is not None and earlier.get("added"):
            entry["added"] = True
        merged.append(entry)
    new["collections"] = list(before.values()) + merged
    return new


def queue_arrival(conn, library, rating_key, payload, now=None, complete=True):
    """Put one kometa.item.arrived event in the outbox. A re-run of an item that is still unpublished
    replaces its payload (keeping what the earlier attempt added); one that was already published is
    left alone (no second announcement). ``complete=False`` (a collection failed, the trigger runs the
    item again) holds the row back from publishing until a later run completes it or
    release_arrival gives up waiting. Returns True when queued."""
    stamp = _stamp(now or datetime.now(timezone.utc))
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT payload, published_at FROM arrivals WHERE library = ? AND rating_key = ?", (library, int(rating_key))).fetchone()
        if row is not None and row["published_at"] is not None:
            conn.execute("COMMIT")
            return False
        if row is not None:
            payload = _merge_attempts(json.loads(row["payload"]), payload)
        conn.execute(
            "INSERT OR REPLACE INTO arrivals (library, rating_key, payload, created_at, published_at, complete) VALUES (?, ?, ?, ?, NULL, ?)",
            (library, int(rating_key), json.dumps(payload, sort_keys=True), stamp, 1 if complete else 0),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True


def unpublished_arrivals(conn, limit=100):
    """Outbox rows ready to publish and not yet acknowledged by JetStream, oldest first: library,
    rating_key, payload (dict). Rows held back by a failed run are not ready."""
    rows = conn.execute("SELECT library, rating_key, payload FROM arrivals WHERE published_at IS NULL AND complete = 1 ORDER BY created_at, library, rating_key LIMIT ?", (limit,))
    return [{"library": row["library"], "rating_key": row["rating_key"], "payload": json.loads(row["payload"])} for row in rows]


def release_arrival(conn, rating_key):
    """The trigger stopped retrying an item: publish what its last run managed rather than nothing.
    Plex ratingKeys are unique across a server's libraries. Returns True when a held row was released."""
    cursor = conn.execute("UPDATE arrivals SET complete = 1 WHERE rating_key = ? AND published_at IS NULL AND complete = 0", (int(rating_key),))
    return cursor.rowcount > 0


def mark_published(conn, library, rating_key, now=None):
    conn.execute("UPDATE arrivals SET published_at = ? WHERE library = ? AND rating_key = ?", (_stamp(now or datetime.now(timezone.utc)), library, int(rating_key)))


def queue_build(conn, library, mapping_name, source_file, now=None):
    """A collection waiting on an arrival does not exist in Plex yet: ask the trigger for a scoped
    Kometa run of it (the run creates it with its full metadata)."""
    conn.execute(
        "INSERT INTO builds (library, mapping_name, source_file, requested_at, attempts) VALUES (?, ?, ?, ?, 0) " "ON CONFLICT (library, mapping_name) DO UPDATE SET source_file = excluded.source_file, requested_at = excluded.requested_at",
        (library, mapping_name, source_file, _stamp(now or datetime.now(timezone.utc))),
    )


def pending_builds(conn):
    rows = conn.execute("SELECT library, mapping_name, source_file, attempts FROM builds ORDER BY requested_at, library, mapping_name")
    return [dict(row) for row in rows]


def finish_build(conn, library, mapping_name):
    conn.execute("DELETE FROM builds WHERE library = ? AND mapping_name = ?", (library, mapping_name))


def fail_build(conn, library, mapping_name, max_attempts):
    """Charge a failed scoped run an attempt; past ``max_attempts`` the request is dropped (the
    scheduled sweep still builds the collection). Returns True when dropped."""
    conn.execute("UPDATE builds SET attempts = attempts + 1 WHERE library = ? AND mapping_name = ?", (library, mapping_name))
    row = conn.execute("SELECT attempts FROM builds WHERE library = ? AND mapping_name = ?", (library, mapping_name)).fetchone()
    if row is not None and row["attempts"] >= max_attempts:
        finish_build(conn, library, mapping_name)
        return True
    return False


# ── Snapshot from a finished CollectionBuilder ─────────────────────────


def _is_custom_order(custom_sort):
    return isinstance(custom_sort, str) and custom_sort.startswith("custom")


def _list_order(builder, library):
    """Ordered TMDb/TVDb ids as the builders returned them: the order a custom collection is sorted in."""
    is_movie = library.is_movie
    key_map = library.movie_rating_key_map if is_movie else library.show_rating_key_map
    order = []
    seen = set()

    def push(ext_id):
        if ext_id is not None and ext_id not in seen:
            seen.add(ext_id)
            order.append(ext_id)

    for input_id, id_type in builder.gathered_ids:
        try:
            if (id_type == "tmdb" and is_movie) or (id_type == "tvdb" and not is_movie):
                for one in input_id if isinstance(input_id, list) else [input_id]:
                    push(int(one))
            elif id_type == "imdb":
                for rk in library.imdb_map.get(input_id, []):
                    push(key_map.get(rk))
            elif id_type == "ratingKey":
                push(key_map.get(int(input_id)))
        except (TypeError, ValueError):
            continue
    if builder.custom_sort == "custom.desc":
        order.reverse()
    return {ext_id: index for index, ext_id in enumerate(order)}


def build_snapshot(builder, library, metadata, add_ran, sync_ran, mapping_name=None):
    """Describe what ``builder`` left in Plex, reusing what the run already knows (no extra Plex calls
    except one membership read for an append-mode collection that was not loaded this run)."""
    is_movie = library.is_movie
    key_map = library.movie_rating_key_map if is_movie else library.show_rating_key_map
    present = bool(builder.build_collection and builder.obj is not None and not builder.deleted)
    kind = "smart" if builder.smart_url or builder.smart_label_collection else ("blank" if builder.blank_collection else "list")

    previous_members = None
    rating_keys = None
    if builder.build_collection and kind == "list":
        if not present:
            rating_keys = []
        elif builder.items:
            # load_collection_items() read the finished collection from Plex this run.
            rating_keys = [item.ratingKey for item in builder.items]
        elif builder.sync:
            # remove_item_map holds the collection as it was before the run; entries still set were removed.
            original = set(builder.remove_item_map)
            rating_keys = set(original)
            if sync_ran:
                rating_keys -= {rk for rk, item in builder.remove_item_map.items() if item is not None}
            if add_ran:
                rating_keys |= {item.ratingKey for item in builder.found_items}
            rating_keys = sorted(rating_keys)
        else:
            rating_keys = [item.ratingKey for item in library.get_collection_items(builder.obj, builder.smart_label_collection)]
        if builder.sync and builder.exists:
            previous_members = [(rk, key_map.get(rk)) for rk in builder.remove_item_map]
        elif not builder.exists:
            previous_members = []

    custom = _is_custom_order(builder.custom_sort)
    order_recorded = bool(custom and present and builder.sort_completed)
    positions = _list_order(builder, library) if order_recorded else {}

    members = None
    if rating_keys is not None:
        members = []
        for rk in rating_keys:
            ext_id = key_map.get(rk)
            members.append((rk, ext_id, positions.get(ext_id) if ext_id is not None else None))

    wanted = []
    if builder.build_collection and kind == "list":
        raw = builder.missing_movies if is_movie else builder.missing_shows
        filtered = set(builder.missing_filtered_movies if is_movie else builder.missing_filtered_shows)
        passed = set(builder.missing_passed_movies if is_movie else builder.missing_passed_shows)
        held = {entry["id"] for entry in (builder.held_back_radarr if is_movie else builder.held_back_sonarr)}
        seen = set()
        for ext_id in raw:
            if ext_id in seen:
                continue
            seen.add(ext_id)
            state = True if ext_id in filtered else (False if ext_id in passed else None)
            wanted.append((ext_id, state, ext_id in held, positions.get(ext_id)))

    return {
        "library": library.name,
        "name": builder.name,
        "mapping_name": mapping_name if mapping_name is not None else builder.name,
        "source_file": metadata.get_file_name(),
        "source_path": metadata.path,
        "id_type": "tmdb" if is_movie else ("tvdb" if library.is_show else None),
        "built": builder.build_collection,
        "present": present,
        "kind": kind,
        "missing_checked": builder.missing_checked,
        "collection_order": builder.custom_sort if isinstance(builder.custom_sort, str) else None,
        "order_recorded": order_recorded,
        "members": members,
        "previous_members": previous_members,
        "wanted": wanted,
    }


# ── Run integration (kometa.py calls these; they never raise) ──────────


class RunRecorder:
    """Writes the record during one Kometa run. Every failure is logged and swallowed so the run
    carries on; after RUN_FAILURE_LIMIT consecutive failures it stops trying for the rest of the run
    so a stuck lock or broken file costs seconds, not minutes per collection."""

    def __init__(self, config_dir, log, timeout_ms=RUN_WRITE_TIMEOUT_MS, failure_limit=RUN_FAILURE_LIMIT):
        self.path = db_path(config_dir)
        self.log = log
        self.timeout_ms = timeout_ms
        self.failure_limit = failure_limit
        self.failures = 0

    def _run(self, label, action):
        if self.failures >= self.failure_limit:
            return None
        try:
            conn = connect(self.path, timeout_ms=self.timeout_ms, log=self.log)
            try:
                result = action(conn)
            finally:
                conn.close()
        except Exception as e:
            self.failures += 1
            self.log.stacktrace()
            self.log.error(f"Live Record Error: {label}: {e}")
            if self.failures >= self.failure_limit:
                self.log.error(f"Live Record Error: {self.failures} failures in a row, not recording the rest of this run")
            return None
        self.failures = 0
        return result

    def record(self, library, metadata, builder, mapping_name, add_ran, sync_ran, sync_ok=True, observed_at=None):
        """Record one collection. ``sync_ok`` is False when a later step of its run failed; ``observed_at``
        is when its run started reading Plex (naive = local time). Returns True when written."""
        try:
            snapshot = build_snapshot(builder, library, metadata, add_ran, sync_ran, mapping_name=mapping_name)
            snapshot["sync_ok"] = sync_ok
            if observed_at is not None:
                snapshot["observed_at"] = observed_at if observed_at.tzinfo else observed_at.astimezone()
        except Exception as e:
            self.log.stacktrace()
            self.log.error(f"Live Record Error: {mapping_name}: {e}")
            return False
        events = self._run(snapshot["name"], lambda conn: write_collection(conn, snapshot))
        if events is None:
            return False
        members = "untracked" if snapshot["members"] is None else len(snapshot["members"])
        self.log.info(f"Live Record: {snapshot['name']} | members: {members} | wanted: {len(snapshot['wanted'])} | changes: {len(events)}")
        return True

    def deleted(self, library_name, mapping_name):
        names = self._run(mapping_name, lambda conn: mark_deleted(conn, library_name, mapping_name))
        for name in names or []:
            self.log.info(f"Live Record: {name} deleted from Plex, recorded as absent")
        return names is not None

    def failed(self, library_name, mapping_name):
        return self._run(mapping_name, lambda conn: (mark_failed(conn, library_name, mapping_name),)) is not None

    def skipped(self, library_name, mapping_name):
        # Wrapped so "no record yet" (touch returns None) is not mistaken for a failure.
        return self._run(mapping_name, lambda conn: (touch(conn, library_name, mapping_name),)) is not None

    def expire(self):
        expired = self._run("expiring old records", lambda conn: expire(conn))
        for library, name in expired or []:
            self.log.info(f"Live Record: expired {name} ({library}), not synced for {EXPIRE_AFTER.days} days")
        return expired is not None

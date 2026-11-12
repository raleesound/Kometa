"""Collection membership record for live Kometa (epic #5634, slice S2).

After each collection runs, Kometa writes what the collection now holds and what it still
wants into a SQLite file next to config.yml. The arrival handler (S3) reads it to add a
newly imported film to every collection waiting on it, in its recorded position, without
waiting for the next scheduled sweep.

Two processes write this file: the scheduled/scoped Kometa run and the import trigger.
WAL mode plus a busy timeout lets them interleave; every write is one short transaction.
Only the stdlib sqlite3 module is used.
"""

import os
import sqlite3
import time
from datetime import datetime, timezone

DB_NAME = "live-collections.db"
SCHEMA_VERSION = 1
# Readers (the arrival handler) can afford to wait; a Kometa run must not stall on a held lock,
# so its writes use a short timeout and give up for the rest of the run after repeated failures.
BUSY_TIMEOUT_MS = 30000
RUN_WRITE_TIMEOUT_MS = 2000
RUN_FAILURE_LIMIT = 3
HISTORY_KEEP = 1000

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
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (library, name)
);
CREATE TABLE IF NOT EXISTS members (
    library TEXT NOT NULL,
    name TEXT NOT NULL,
    rating_key INTEGER NOT NULL,
    ext_id INTEGER,
    position INTEGER,
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
    ext_id INTEGER
);
CREATE INDEX IF NOT EXISTS history_by_collection ON history (library, name, id);
"""

MEMBER_ADDED = "member_added"
MEMBER_REMOVED = "member_removed"
WANTED_ADDED = "wanted_added"
WANTED_REMOVED = "wanted_removed"


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


def connect(path, timeout_ms=BUSY_TIMEOUT_MS):
    """Open the record, creating the schema on first use. Autocommit; callers open their own transactions."""
    conn = sqlite3.connect(path, timeout=timeout_ms / 1000, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(timeout_ms)}")
        _set_wal(conn, timeout_ms)
        if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            # executescript() would commit our BEGIN IMMEDIATE, so run the statements one by one.
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                    for statement in SCHEMA.split(";"):
                        if statement.strip():
                            conn.execute(statement)
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    except Exception:
        conn.close()
        raise
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    run_missing never evaluated the id).

    When this run could not establish a custom order (sort failed or skipped) the last good order
    is carried over by id instead of being erased.
    """
    library, name = snapshot["library"], snapshot["name"]
    members = None if snapshot["members"] is None else [list(m) for m in snapshot["members"]]
    wanted = [list(w) for w in snapshot["wanted"]]
    order_recorded = bool(snapshot["order_recorded"])
    now = _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
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
            "INSERT OR REPLACE INTO collections (library, name, mapping_name, source_file, source_path, id_type, built, present, kind, members_tracked, missing_checked, collection_order, order_recorded, order_recorded_at, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            ),
        )
        conn.execute("DELETE FROM members WHERE library = ? AND name = ?", (library, name))
        if members:
            conn.executemany("INSERT OR REPLACE INTO members (library, name, rating_key, ext_id, position) VALUES (?, ?, ?, ?, ?)", [(library, name, rk, ext_id, pos) for rk, ext_id, pos in members])
        conn.execute("DELETE FROM wanted WHERE library = ? AND name = ?", (library, name))
        conn.executemany(
            "INSERT OR REPLACE INTO wanted (library, name, ext_id, filtered, held_back, position) VALUES (?, ?, ?, ?, ?, ?)",
            [(library, name, ext_id, _bool_or_none(filtered), int(bool(held)), pos) for ext_id, filtered, held, pos in wanted],
        )
        conn.executemany("INSERT INTO history (library, name, recorded_at, event, rating_key, ext_id) VALUES (?, ?, ?, ?, ?, ?)", [(library, name, now, event, rk, ext_id) for event, rk, ext_id in events])
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
            conn.execute("UPDATE collections SET present = 0, members_tracked = 1, recorded_at = ? WHERE library = ? AND name = ?", (now, library, name))
            conn.execute("DELETE FROM members WHERE library = ? AND name = ?", (library, name))
            conn.execute("DELETE FROM wanted WHERE library = ? AND name = ?", (library, name))
            conn.executemany("INSERT INTO history (library, name, recorded_at, event, rating_key, ext_id) VALUES (?, ?, ?, ?, ?, ?)", [(library, name, now, event, rk, ext_id) for event, rk, ext_id in events])
            _trim_history(conn, library, name)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return names


def prune_library(conn, library, loaded_files):
    """Drop records only for collections positively removed from config.

    ``loaded_files`` maps each collection file that loaded cleanly this run (its path) to the
    collection mapping names it defines. A record is dropped when its source file is in that map
    and no longer defines it. Records whose file failed to load, was skipped, or is gone are kept:
    a failed load is not evidence that the collection was removed. Returns the dropped names.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        stale = []
        for row in conn.execute("SELECT name, mapping_name, source_path FROM collections WHERE library = ?", (library,)).fetchall():
            names = loaded_files.get(row["source_path"])
            if names is not None and (row["mapping_name"] or row["name"]) not in names:
                stale.append(row["name"])
        for name in stale:
            for table in ("collections", "members", "wanted", "history"):
                conn.execute(f"DELETE FROM {table} WHERE library = ? AND name = ?", (library, name))  # nosec B608 - table names are constants
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return stale


# ── Read API (used by the arrival handler, S3) ─────────────────────────


def find_waiting(conn, id_type, ext_id, include_unknown=True):
    """Every built collection still waiting on this TMDb ('tmdb') or TVDb ('tvdb') id.

    An id run_missing filtered out is never returned. ``filter_state`` is 'passed' when run_missing
    evaluated the id and its filters accepted it, 'unknown' when it was never evaluated (missing
    processing off, or the TMDb/TVDb lookup failed). Running the collection again re-applies its
    filters, so callers that rebuild the collection can act on 'unknown'; pass
    ``include_unknown=False`` to get only confirmed ids.

    Rows: library, name, mapping_name, source_file, source_path, present (the Plex collection
    exists), collection_order, order_recorded, position, held_back, filter_state.
    """
    rows = conn.execute(
        "SELECT c.library, c.name, c.mapping_name, c.source_file, c.source_path, c.present, c.collection_order, c.order_recorded, w.position, w.held_back, "
        "CASE WHEN w.filtered IS NULL THEN 'unknown' ELSE 'passed' END AS filter_state "
        "FROM wanted w JOIN collections c ON c.library = w.library AND c.name = w.name "
        "WHERE w.ext_id = ? AND c.id_type = ? AND c.built = 1 AND (w.filtered = 0 OR (w.filtered IS NULL AND ?)) ORDER BY c.library, c.name",
        (int(ext_id), id_type, int(bool(include_unknown))),
    )
    return [dict(row) for row in rows]


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


def get_members(conn, library, name):
    """Members in recorded order (positioned members first, by position)."""
    rows = conn.execute("SELECT rating_key, ext_id, position FROM members WHERE library = ? AND name = ? ORDER BY position IS NULL, position, rating_key", (library, name))
    return [dict(row) for row in rows]


def get_collection(conn, library, name):
    row = conn.execute("SELECT * FROM collections WHERE library = ? AND name = ?", (library, name)).fetchone()
    return None if row is None else dict(row)


def get_history(conn, library, name, limit=200):
    rows = conn.execute("SELECT recorded_at, event, rating_key, ext_id FROM history WHERE library = ? AND name = ? ORDER BY id DESC LIMIT ?", (library, name, limit))
    return [dict(row) for row in rows]


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
            conn = connect(self.path, timeout_ms=self.timeout_ms)
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

    def record(self, library, metadata, builder, mapping_name, add_ran, sync_ran):
        """Record one collection. Returns True when written."""
        try:
            snapshot = build_snapshot(builder, library, metadata, add_ran, sync_ran, mapping_name=mapping_name)
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

    def prune(self, library_name, loaded_files):
        dropped = self._run(f"pruning {library_name}", lambda conn: prune_library(conn, library_name, loaded_files))
        for name in dropped or []:
            self.log.info(f"Live Record: dropped {name}, no longer configured in {library_name}")
        return dropped is not None

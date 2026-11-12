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
from datetime import datetime, timezone

DB_NAME = "live-collections.db"
SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 30000

SCHEMA = """
CREATE TABLE IF NOT EXISTS collections (
    library TEXT NOT NULL,
    name TEXT NOT NULL,
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
    filtered INTEGER NOT NULL,
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


def connect(path):
    """Open the record, creating the schema on first use. Autocommit; callers open their own transactions."""
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        # executescript() would commit our BEGIN IMMEDIATE, so run the statements one by one.
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_collection(conn, snapshot):
    """Replace one collection's record with ``snapshot`` and append the diff to its history.

    snapshot keys: library, name, source_file, source_path, id_type, built, present, kind,
    missing_checked, collection_order, order_recorded,
    members (None = not tracked, else list of (rating_key, ext_id, position)),
    previous_members (rating keys the collection held before this run, or None if unknown;
    used only when there is no earlier record to diff against),
    wanted (list of (ext_id, filtered, held_back, position)).
    """
    library, name = snapshot["library"], snapshot["name"]
    members = snapshot["members"]
    now = _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        prior = conn.execute("SELECT members_tracked FROM collections WHERE library = ? AND name = ?", (library, name)).fetchone()
        old_members = None
        if prior is not None and prior["members_tracked"]:
            old_members = {row["rating_key"]: row["ext_id"] for row in conn.execute("SELECT rating_key, ext_id FROM members WHERE library = ? AND name = ?", (library, name))}
        elif prior is None and snapshot.get("previous_members") is not None:
            old_members = {rk: None for rk in snapshot["previous_members"]}
        old_wanted = None
        if prior is not None:
            old_wanted = {row["ext_id"] for row in conn.execute("SELECT ext_id FROM wanted WHERE library = ? AND name = ?", (library, name))}

        events = []
        if members is not None and old_members is not None:
            new_members = {rk: ext_id for rk, ext_id, _ in members}
            events.extend((MEMBER_ADDED, rk, new_members[rk]) for rk in new_members if rk not in old_members)
            events.extend((MEMBER_REMOVED, rk, old_members[rk]) for rk in old_members if rk not in new_members)
        new_wanted = {ext_id for ext_id, _, _, _ in snapshot["wanted"]}
        if old_wanted is not None:
            events.extend((WANTED_ADDED, None, ext_id) for ext_id in sorted(new_wanted - old_wanted))
            events.extend((WANTED_REMOVED, None, ext_id) for ext_id in sorted(old_wanted - new_wanted))

        conn.execute(
            "INSERT OR REPLACE INTO collections (library, name, source_file, source_path, id_type, built, present, kind, members_tracked, missing_checked, collection_order, order_recorded, recorded_at) " "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                library,
                name,
                snapshot["source_file"],
                snapshot["source_path"],
                snapshot["id_type"],
                int(bool(snapshot["built"])),
                int(bool(snapshot["present"])),
                snapshot["kind"],
                int(members is not None),
                int(bool(snapshot["missing_checked"])),
                snapshot["collection_order"],
                int(bool(snapshot["order_recorded"])),
                now,
            ),
        )
        conn.execute("DELETE FROM members WHERE library = ? AND name = ?", (library, name))
        if members:
            conn.executemany("INSERT OR REPLACE INTO members (library, name, rating_key, ext_id, position) VALUES (?, ?, ?, ?, ?)", [(library, name, rk, ext_id, pos) for rk, ext_id, pos in members])
        conn.execute("DELETE FROM wanted WHERE library = ? AND name = ?", (library, name))
        conn.executemany(
            "INSERT OR REPLACE INTO wanted (library, name, ext_id, filtered, held_back, position) VALUES (?, ?, ?, ?, ?, ?)",
            [(library, name, ext_id, int(bool(filtered)), int(bool(held)), pos) for ext_id, filtered, held, pos in snapshot["wanted"]],
        )
        conn.executemany("INSERT INTO history (library, name, recorded_at, event, rating_key, ext_id) VALUES (?, ?, ?, ?, ?, ?)", [(library, name, now, event, rk, ext_id) for event, rk, ext_id in events])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return events


def prune_library(conn, library, keep_names):
    """Drop records for collections no longer configured in ``library`` (full runs only). Returns the dropped names."""
    keep = set(keep_names)
    conn.execute("BEGIN IMMEDIATE")
    try:
        stale = [row["name"] for row in conn.execute("SELECT name FROM collections WHERE library = ?", (library,)) if row["name"] not in keep]
        for name in stale:
            for table in ("collections", "members", "wanted"):
                conn.execute(f"DELETE FROM {table} WHERE library = ? AND name = ?", (library, name))  # nosec B608 - table names are constants
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return stale


# ── Read API (used by the arrival handler, S3) ─────────────────────────


def find_waiting(conn, id_type, ext_id):
    """Every built collection whose post-filter wanted set contains this TMDb ('tmdb') or TVDb ('tvdb') id.

    Rows: library, name, source_file, source_path, present (the Plex collection exists),
    collection_order, order_recorded, position, held_back.
    """
    rows = conn.execute(
        "SELECT c.library, c.name, c.source_file, c.source_path, c.present, c.collection_order, c.order_recorded, w.position, w.held_back "
        "FROM wanted w JOIN collections c ON c.library = w.library AND c.name = w.name "
        "WHERE w.ext_id = ? AND c.id_type = ? AND c.built = 1 AND w.filtered = 0 ORDER BY c.library, c.name",
        (int(ext_id), id_type),
    )
    return [dict(row) for row in rows]


def get_position(conn, library, name, ext_id):
    """Recorded custom-order position of an id (member or wanted) in a collection, or None."""
    row = conn.execute(
        "SELECT position FROM wanted WHERE library = ? AND name = ? AND ext_id = ? AND position IS NOT NULL " "UNION ALL SELECT position FROM members WHERE library = ? AND name = ? AND ext_id = ? AND position IS NOT NULL LIMIT 1",
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


def build_snapshot(builder, library, metadata, add_ran, sync_ran):
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
            previous_members = list(builder.remove_item_map)
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
        held = {entry["id"] for entry in (builder.held_back_radarr if is_movie else builder.held_back_sonarr)}
        seen = set()
        for ext_id in raw:
            if ext_id in seen:
                continue
            seen.add(ext_id)
            wanted.append((ext_id, ext_id in filtered, ext_id in held, positions.get(ext_id)))

    return {
        "library": library.name,
        "name": builder.name,
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

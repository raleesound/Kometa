"""Arrival step of a per-item run (--run-items, #5634 S3).

The import trigger starts a per-item run once Plex has a new item. Before the item's operations run,
this puts the item into every collection the live record says is waiting for it, slots it into its
recorded position in a custom-ordered collection, records the change (source=trigger) and queues one
kometa.item.arrived event in the record's outbox for the trigger to publish.

It adds ONLY what the record says a collection wants: a scheduled sync run removes members its
builders do not find, so anything else would be undone (and announced wrongly) a few hours later.
Everything here is idempotent with the scheduled sweep: an item that is already a member is not
added again, an item already in its slot is not moved, and the record ignores repeats.

The run holds the Kometa run lock, so a scheduled sweep never interleaves with these writes.
"""

from datetime import datetime, timezone

from modules import live_record, util
from modules.util import Failed

logger = util.logger

PAYLOAD_VERSION = 1
# plexapi Collection.collectionSort: 0 release date, 1 alphabetical, 2 custom (plex.collection_order_keys).
CUSTOM_SORT = 2


def _ext_ids(library, rating_key):
    """(tmdb_id, tvdb_id, imdb_id) from the maps the run built with map_guids()."""
    if library.is_movie:
        return library.movie_rating_key_map.get(rating_key), None, library.imdb_rating_key_map.get(rating_key)
    return None, library.show_rating_key_map.get(rating_key), library.imdb_rating_key_map.get(rating_key)


def _read_collection(library, name):
    """A fresh Plex collection and its items in Plex's order. plexapi caches a collection's items on
    the object, so every read (before and after an edit) fetches the collection again."""
    collection = library.get_collection(name, debug=False)
    return collection, list(library.get_collection_items(collection, False))


def _stamp(moment):
    """ISO 8601 UTC with a Z suffix, whole seconds: the contract's datetime format."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _predecessor(order, rating_key, key_map, recorded, position):
    """The item that should precede ``rating_key``: of the items actually in the collection (Plex's
    order, not the record's), the one with the largest recorded position below ``position``. None
    means the start. Items with no recorded position are ignored."""
    best = None
    best_position = None
    for item in order:
        rk = int(item.ratingKey)
        if rk == rating_key:
            continue
        ext_id = key_map.get(rk)
        found = recorded.get(ext_id) if ext_id is not None else None
        if found is not None and found < position and (best_position is None or found > best_position):
            best, best_position = item, found
    return best


def _in_slot(order, rating_key, after):
    keys = [int(item.ratingKey) for item in order]
    if rating_key not in keys:
        return False
    index = keys.index(rating_key)
    if after is None:
        return index == 0
    return index > 0 and keys[index - 1] == int(after.ratingKey)


def _slot(conn, library, name, item, rating_key, position):
    """Move the item to its recorded position in a custom-ordered collection, then check Plex's actual
    order. Returns True when it is in its slot (moved or already there)."""
    key_map = library.movie_rating_key_map if library.is_movie else library.show_rating_key_map
    recorded = live_record.positions(conn, library.name, name)
    collection, order = _read_collection(library, name)
    after = _predecessor(order, rating_key, key_map, recorded, position)
    if _in_slot(order, rating_key, after):
        return True
    library.moveItem(collection, item, after)
    _, order = _read_collection(library, name)
    if not _in_slot(order, rating_key, after):
        raise Failed(f"Arrival Error: {util.item_title(item)} did not land after {util.item_title(after) if after else 'the start'} in {name}")
    logger.info(f"Arrival: moved {util.item_title(item)} {'after ' + util.item_title(after) if after else 'to the start'} of {name}")
    return True


def _add_to(conn, library, item, rating_key, ext_id, waiting, now):
    """Put the item into one waiting collection. Returns its payload entry (the kometa-contract
    arrivedCollectionSchema), or None when the collection is not in Plex and a run of it was queued
    instead: the event lists only collections the item belongs to now."""
    name = waiting["name"]
    custom = live_record._is_custom_order(waiting["collection_order"]) and waiting["position"] is not None
    # The record counts from 0; the event's position is 1-based and only set for a custom order.
    entry = {"name": name, "mapping_name": waiting["mapping_name"], "source_file": waiting["source_file"], "added": False, "position": waiting["position"] + 1 if custom else None, "slotted": False}
    collection = None
    order = []
    if waiting["present"]:
        try:
            collection, order = _read_collection(library, name)
        except Failed:
            collection = None
    if collection is None:
        # Not in Plex (never built, below its minimum, or deleted since the record was written).
        # Adding the tag would make a bare collection without its metadata, so ask for a scoped run.
        live_record.queue_build(conn, library.original_mapping_name, waiting["mapping_name"], waiting["source_file"], now=now)
        logger.info(f"Arrival: {name} is waiting for {util.item_title(item)} but is not in Plex; queued a run of it")
        return None

    if rating_key not in {int(member.ratingKey) for member in order}:
        library.alter_collection([item], name)
        _, order = _read_collection(library, name)
        if rating_key not in {int(member.ratingKey) for member in order}:
            raise Failed(f"Arrival Error: {util.item_title(item)} is still not in {name} after adding it")
        entry["added"] = True
        logger.info(f"Arrival: added {util.item_title(item)} to {name}")

    if custom:
        if int(collection.collectionSort) == CUSTOM_SORT:
            entry["slotted"] = _slot(conn, library, name, item, rating_key, waiting["position"])
        else:
            logger.warning(f"Arrival: {name} is recorded as custom-ordered but Plex sorts it by {collection.collectionSort}; not moving {util.item_title(item)}")

    live_record.record_arrival(conn, library.name, name, rating_key, ext_id, position=waiting["position"], now=now)
    return entry


def handle_item(conn, library, rating_key, now=None):
    """Add one arrived item to every collection waiting for it and queue its arrival event.
    Returns True when every waiting collection was handled; False means run it again."""
    moment = now or datetime.now(timezone.utc)
    tmdb_id, tvdb_id, imdb_id = _ext_ids(library, rating_key)
    ext_id = tmdb_id if library.is_movie else tvdb_id
    if ext_id is None and imdb_id is None:
        logger.info(f"Arrival: {rating_key} has no TMDb, TVDb or IMDb id in {library.name}; not announcing it")
        return True
    item = library.fetch_item(rating_key)
    id_type = "tmdb" if library.is_movie else "tvdb"
    ok = True
    collections = []
    waiting_for = live_record.find_waiting(conn, id_type, ext_id, include_unknown=True, now=moment) if ext_id is not None else []
    for waiting in waiting_for:
        if waiting["library"] != library.name:
            continue
        try:
            entry = _add_to(conn, library, item, rating_key, ext_id, waiting, moment)
            if entry is not None:
                collections.append(entry)
        except Exception as e:
            ok = False
            logger.stacktrace()
            logger.error(f"Arrival Error: {util.item_title(item)} into {waiting['name']}: {e}")
    if not collections and ok:
        logger.info(f"Arrival: no collection is waiting for {util.item_title(item)}")
    payload = {
        "v": PAYLOAD_VERSION,
        "library": library.name,
        "rating_key": str(rating_key),
        "media_type": "movie" if library.is_movie else "show",
        "tmdb_id": tmdb_id,
        "tvdb_id": tvdb_id,
        "imdb_id": imdb_id,
        "title": item.title or str(rating_key),
        "year": getattr(item, "year", None),
        "added_at": _stamp(getattr(item, "addedAt", None) or moment),
        "arrived_at": _stamp(moment),
        "collections": collections,
    }
    live_record.queue_arrival(conn, library.name, rating_key, payload, now=moment)
    return ok


def run(config, library, now=None):
    """The arrival step for every --run-items key in this library. Never raises; returns False when any
    item needs another run (the trigger retries it)."""
    path = live_record.db_path(config.default_dir)
    try:
        conn = live_record.connect(path, log=logger)
    except Exception as e:
        logger.stacktrace()
        logger.error(f"Arrival Error: cannot open the live record {path}: {e}")
        return False
    ok = True
    try:
        key_map = library.movie_rating_key_map if library.is_movie else library.show_rating_key_map
        for rating_key in config.run_items:
            if rating_key not in key_map and rating_key not in library.cached_items:
                continue
            try:
                ok = handle_item(conn, library, int(rating_key), now=now) and ok
            except Exception as e:
                ok = False
                logger.stacktrace()
                logger.error(f"Arrival Error: item {rating_key}: {e}")
    finally:
        conn.close()
    return ok

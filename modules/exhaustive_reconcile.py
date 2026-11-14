"""Which ``exhaustive-*`` Radarr tags does the config still own, and strip the ones it does not (#5634 S9c).

``acquire: exhaustive`` tags every wanted film ``exhaustive-<slug>`` and the radarr-web-bridge searches every film carrying such a tag.
Nothing in a collection's own run notices that the collection is gone, so removing the key, deleting the chart's file or renaming it
would leave its films tagged (and hunted) forever. This module reads the collection files config.yml lists, works out which tags
the configured ``acquire: exhaustive`` collections own, and removes every other ``exhaustive-<slug>`` tag from Radarr.

It is built never to guess. ``owned_tags`` answers ``None`` (and why) whenever the config cannot be read completely and
literally: a file that will not parse or is missing, a remote (url/git/repo) collection file, a template that is not defined in its own
file, an ``acquire`` value or collection name that is a template variable. Nothing is untagged then. Only labels of the exact form
``exhaustive-<slug>`` (lowercase a-z, 0-9, single hyphens) are touched: the bare ``exhaustive`` tag, ``exhaustive_x`` and every other
tag are never read as ours. Only the stdlib, ruamel.yaml (Kometa's own loader) and arrapi are used, so the import trigger can run it.
"""

import glob
import logging
import os
import re
from types import SimpleNamespace

import ruamel.yaml

from modules import add_budget, live_record

OWNED_LABEL = re.compile(r"^exhaustive-[a-z0-9]+(?:-[a-z0-9]+)*$")
REMOTE_KEYS = ("url", "git", "repo")
FILE_LISTS = ("collection_files", "metadata_files")
_seen = {}
MAX_STRIPPED = 2


def _read_yaml(path):
    # The loader Kometa itself reads collection files with (YAML 1.2), so 1e3 / 0o17 / Yes mean what the builder sees.
    with open(path, encoding="utf-8") as fh:
        return ruamel.yaml.YAML(typ="safe").load(fh)


def _resolve(path, root):
    return path if os.path.isabs(path) else os.path.join(root, path)


def _local_files(config, root):
    """([paths], reason): every local YAML file config.yml lists as a collection or metadata file. reason is set when one is not local."""
    paths = []
    libraries = (config or {}).get("libraries")
    if not isinstance(libraries, dict) or not libraries:
        return paths, "config.yml has no libraries"
    for library, attrs in libraries.items():
        if isinstance(attrs, dict) and attrs.get("metadata_path"):
            return paths, f"{library} uses the deprecated metadata_path"
        for key in FILE_LISTS:
            for entry in (attrs.get(key) if isinstance(attrs, dict) else None) or []:
                if not isinstance(entry, dict):
                    return paths, f"{library} {key} has an entry that is not a mapping"
                if any(k in entry for k in REMOTE_KEYS):
                    return paths, f"{library} {key} lists a remote file"
                if isinstance(entry.get("file"), str):
                    paths.append((str(library), _resolve(entry["file"], root)))
                if isinstance(entry.get("folder"), str):
                    folder = _resolve(entry["folder"], root)
                    escaped = glob.escape(folder)
                    found = sorted(glob.glob(os.path.join(escaped, "*.yml")) + glob.glob(os.path.join(escaped, "*.yaml")))
                    if not found:
                        return paths, f"{library} {key} folder {os.path.basename(folder)} holds no yaml files right now"
                    paths.extend((str(library), p) for p in found)
    return paths, None


UNRESOLVABLE_NAME_KEYS = ("name_mapping", "translations", "translation_key", "translation_prefix", "key_name", "language", "collection_name", "playlist_name")


def _lower(source):
    """A block as Kometa reads it: attribute keys match case-insensitively. Raises ValueError when a key is a template variable."""
    out = {}
    for key, value in source.items():
        if "<<" in str(key):
            raise ValueError("it has an attribute whose key is a template variable")
        out[str(key).strip().lower()] = value
    return out


def _sources(block, templates):
    """[block, *templates it names], each key-lowercased; raises ValueError when a named template is not defined in its own file."""
    sources = [_lower(block)]
    template = sources[0].get("template")
    for item in template if isinstance(template, list) else [template]:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str):
            if not isinstance(templates.get(name), dict):
                raise ValueError(f"it names template {name} which is not defined in its own file")
            sources.append(_lower(templates[name]))
    return sources


def _acquire_values(sources):
    """Every literal ``acquire`` value a collection (or a template it names) sets, as the builder reads it; raises ValueError when one cannot be read literally."""
    values = []
    for source in sources:
        if "acquire" in source:
            value = source["acquire"]
            if not isinstance(value, str) or "<<" in value:
                raise ValueError("its acquire value is not a literal")
            values.append(value.strip().lower())
    return values


def _effective_name(key, sources):
    """The name Kometa will run the collection under (the builder tags exhaustive_tag(builder.name)); raises ValueError when it cannot be read literally."""
    if any(k in source for source in sources for k in UNRESOLVABLE_NAME_KEYS) or any("name" in source for source in sources[1:]):
        raise ValueError("its name may be changed by a template, alias or translation")
    name = sources[0].get("name", key)
    # A number would be str()-ed by the builder in ways this scan does not model (010, 1e3); only a string is read back verbatim.
    if not isinstance(name, str) or "<<" in name:
        raise ValueError("its name is not a literal")
    return str(name)


def exhaustive_collections(config_path, root=None):
    """([(library, mapping_name)], reason): the collections the config defines with ``acquire: exhaustive``. reason is None when the whole config was read."""
    root = root or os.path.dirname(os.path.dirname(config_path))
    try:
        config = _read_yaml(config_path)
        if not isinstance(config, dict):
            return None, "config.yml is not a mapping document"
        files, remote = _local_files(config, root)
        if remote:
            return None, remote
        if not files:
            return None, "config.yml lists no collection files"
        found = []
        for library, path in files:
            try:
                data = _read_yaml(path)
            except Exception as e:
                return None, f"{os.path.basename(path)} cannot be read now ({type(e).__name__})"
            if not isinstance(data, dict):
                return None, f"{os.path.basename(path)} is not a mapping document"
            if data.get("dynamic_collections"):
                return None, f"{os.path.basename(path)} has dynamic_collections, whose names this scan cannot resolve"
            collections = data.get("collections")
            if collections is None:
                continue
            if not isinstance(collections, dict):
                return None, f"{os.path.basename(path)}: collections is not a mapping"
            templates = data["templates"] if isinstance(data.get("templates"), dict) else {}
            for name, block in collections.items():
                if not isinstance(block, dict):
                    return None, f"{os.path.basename(path)}: collection {name} is not a mapping"
                try:
                    sources = _sources(block, templates)
                    values = _acquire_values(sources)
                    if "exhaustive" in values:
                        found.append((library, _effective_name(name, sources)))
                except ValueError as e:
                    return None, f"{os.path.basename(path)}: collection {name}: {e}"
    except Exception as e:
        return None, f"config.yml cannot be read ({type(e).__name__})"
    return found, None


def owned_tags(config_path, root=None):
    """({tag labels the config owns}, reason): reason is set (and the set is None) whenever the answer cannot be trusted."""
    found, reason = exhaustive_collections(config_path, root)
    if found is None:
        return None, reason
    return {add_budget.exhaustive_tag(name) for _library, name in found}, None


def clashes(config_path, library, name, root=None):
    """The other (library, collection) pairs that map to the same tag as this one; empty when the config cannot be read (the in-run check still applies)."""
    tag = add_budget.exhaustive_tag(name)
    found, _reason = exhaustive_collections(config_path, root)
    others = {(lib, n) for lib, n in (found or []) if add_budget.exhaustive_tag(n) == tag and (lib, n) != (library, name)}
    earlier = _seen.setdefault(tag, (library, name))
    if earlier != (library, name):
        others.add(earlier)
    return sorted(others)


def forget_run():
    _seen.clear()


def radarr_instances(config):
    """[(url, token)] of the global Radarr and every library's own, in config order, without repeats."""
    out = []
    sections = [(config or {}).get("radarr")] + [attrs.get("radarr") for attrs in ((config or {}).get("libraries") or {}).values() if isinstance(attrs, dict)]
    for section in sections:
        if isinstance(section, dict) and isinstance(section.get("url"), str) and isinstance(section.get("token"), str) and "<<" not in section["url"] + section["token"]:
            pair = (section["url"].rstrip("/"), section["token"])
            if pair not in out:
                out.append(pair)
    return out


TIMEOUT = (5, 60)  # (connect, read) seconds for every Radarr call made here and by sync_tag; arrapi sets none, so a half-open Radarr would hang forever


def bound(api):
    """Give every request ``api`` makes a timeout. A timeout surfaces as arrapi's ConnectionFailure, which callers log and skip."""
    session = api.session
    if getattr(session, "_exhaustive_bound", False):
        return api
    request = session.request

    def with_timeout(*args, **kwargs):
        kwargs.setdefault("timeout", TIMEOUT)
        return request(*args, **kwargs)

    session.request = with_timeout
    session._exhaustive_bound = True
    return api


def bounded_session():
    import requests

    session = requests.Session()
    bound(SimpleNamespace(session=session))
    return session


def strip_orphans(api, owned, recorded, log):
    """Remove the ``exhaustive-<slug>`` tags in this Radarr that Kometa recorded applying and ``owned`` no longer holds: off all their films, then the tag.

    A lookalike tag the record has never seen (a hand-made one, the bridge's) is left alone with a warning. At most MAX_STRIPPED tags, and never more than half
    of the recorded tags that exist in this Radarr (at least one), go in one pass; the rest wait for a later pass. Returns {label: films untagged}."""
    lookalikes = {tag.id: tag.label.lower() for tag in api.all_tags() if OWNED_LABEL.match(tag.label.lower())}
    ours = {i: label for i, label in lookalikes.items() if label in recorded}
    strangers = sorted(label for label in lookalikes.values() if label not in recorded and label not in owned)
    if strangers:
        log.warning(f"Radarr Exhaustive: leaving {', '.join(strangers)} alone: no Kometa run recorded applying {'it' if len(strangers) == 1 else 'them'}")
    orphans = {i: label for i, label in ours.items() if label not in owned}
    if not orphans:
        return {}
    cap = min(MAX_STRIPPED, max(1, len(ours) // 2))
    batch = dict(sorted(orphans.items(), key=lambda item: item[1])[:cap])
    if len(orphans) > len(batch):
        log.warning(f"Radarr Exhaustive: {len(orphans) - len(batch)} more orphaned tag(s) ({', '.join(sorted(set(orphans.values()) - set(batch.values())))}) wait for a later pass; at most {cap} are stripped at a time")
    by_tag = {label: [] for label in batch.values()}
    for movie in api.all_movies():
        for tag_id in batch.keys() & set(movie.tagsIds):
            by_tag[batch[tag_id]].append(movie.tmdbId)
    result = {}
    for tag_id, label in sorted(batch.items()):
        ids = sorted(by_tag[label])
        if ids:
            api.edit_multiple_movies(ids, tags=[label], apply_tags="remove", per_request=100)
        try:
            api.delete_tag(tag_id)
        except Exception:
            # arrapi fails to read Radarr's empty reply to a delete; what matters is whether the tag is still there.
            if any(tag.id == tag_id for tag in api.all_tags()):
                raise
        result[label] = len(ids)
        log.info(f"Radarr Exhaustive: {label} is not owned by any collection in the config; untagged {len(ids)} film(s) and deleted the tag")
    return result


def _default_api(url, token):
    from arrapi import RadarrAPI

    return RadarrAPI(url, token, session=bounded_session())


def reconcile(config_path, root=None, log=None, make_api=None, record_path=None):
    """Strip orphan ``exhaustive-*`` tags Kometa applied from every Radarr the config names. Returns {instance number: {label: films}} or None when it was skipped.

    Never raises: it is called from a collection watcher and from the end of a full run. Every Radarr call has a timeout; a Radarr that does not answer is a logged skip."""
    log = log or logging.getLogger("exhaustive-reconcile")
    try:
        owned, reason = owned_tags(config_path, root)
        if owned is None:
            log.warning(f"Radarr Exhaustive: not looking for orphaned tags: {reason}")
            return None
        instances = radarr_instances(_read_yaml(config_path))
        make_api = make_api or _default_api
        try:
            conn = live_record.connect(record_path or live_record.db_path(os.path.dirname(config_path)), timeout_ms=2000)
        except Exception as e:
            log.warning(f"Radarr Exhaustive: not looking for orphaned tags: the live record cannot be opened ({type(e).__name__})")
            return None
        try:
            recorded = live_record.recorded_exhaustive_tags(conn)
            out = {}
            failed = False
            for number, (url, token) in enumerate(instances, 1):
                try:
                    out[number] = strip_orphans(make_api(url, token), owned, recorded, log)
                except Exception as e:
                    failed = True
                    log.error(f"Radarr Exhaustive: could not look for orphaned tags in Radarr {number} ({type(e).__name__})")
            if not failed:
                live_record.forget_exhaustive_tags(conn, {label for stripped in out.values() for label in stripped})
        finally:
            conn.close()
        return out
    except Exception as e:
        log.error(f"Radarr Exhaustive: orphaned-tag check failed ({type(e).__name__})")
        return None

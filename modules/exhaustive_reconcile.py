"""Which ``exhaustive-*`` Radarr tags does the config still own, and strip the ones it does not (#5634 S9c).

``acquire: exhaustive`` tags every wanted film ``exhaustive-<slug>`` and the radarr-web-bridge searches every film carrying such a tag.
Nothing in a collection's own run notices that the collection is gone, so removing the key, deleting the chart's file or renaming it
would leave its films tagged (and hunted) forever. This module reads the collection files config.yml lists, works out which tags
the configured ``acquire: exhaustive`` collections own, and removes every other ``exhaustive-<slug>`` tag from Radarr.

It is built never to guess. ``owned_tags`` answers ``None`` (and why) whenever the config cannot be read completely and
literally: a file that will not parse or is missing, a remote (url/git/repo) or inline-data collection file, a dynamic_collections block that could carry the key, a template that is not defined in its own
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
                if any(str(k).strip().lower() in ("data", "pmm") for k in entry):
                    return paths, f"{library} {key} has an entry with inline data or pmm that this scan does not read"
                if _mentions_acquire(entry):
                    return paths, f"{library} {key} has an entry that mentions acquire"
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


def _builtins_for(data):
    """The built-in templates a file may name without defining them: only when its ``external_templates`` is absent or exactly Kometa's shipped
    ``default: templates``. Any file/url/git/repo entry can supply a template this scan never reads, so nothing then counts as built in."""
    external = data.get("external_templates")
    if external is None or external == {"default": "templates"}:
        return _builtin_templates()
    return {}


def _sources(block, templates, builtin):
    """[block, *templates it names], each key-lowercased; raises ValueError when a named template is neither defined in its own file nor a trusted built-in."""
    sources = [_lower(block)]
    template = sources[0].get("template")
    for item in template if isinstance(template, list) else [template]:
        name = item.get("name") if isinstance(item, dict) else item
        if item is not None and not isinstance(name, str):
            raise ValueError("it names a template this scan cannot read")
        if isinstance(name, str):
            if isinstance(templates.get(name), dict):
                sources.append(_lower(templates[name]))
            elif name in builtin and not _mentions_acquire(builtin[name], variable_keys=False):
                # one of Kometa's own shipped templates: it never carries the fork-only key; if it can rename the collection, say so for _effective_name
                sources.append({"name_mapping": True} if _can_rename(builtin[name]) else {})
            else:
                raise ValueError(f"it names template {name} which is neither defined in a scanned file nor a Kometa built-in")
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


def _could_spell_acquire(key):
    """A key built from a template variable whose literal head leaves room for the variable to make it ``acquire`` (``<<k>>``, ``ac<<x>>``); ``name_<<key>>`` cannot."""
    return isinstance(key, str) and "<<" in key and "acquire".startswith(key.split("<<")[0].strip().lower())


def _mentions_acquire(node, variable_keys=True):
    """True when ``acquire`` appears anywhere in a parsed YAML node, as (part of) a key or a string value. With ``variable_keys`` a key whose literal head
    could be completed by a variable into ``acquire`` counts too; Kometa's shipped templates use bare variable keys inside filter arguments and are read without it."""
    if isinstance(node, dict):
        return any(_mentions_acquire(k, variable_keys) or (variable_keys and _could_spell_acquire(k)) or _mentions_acquire(v, variable_keys) for k, v in node.items())
    if isinstance(node, (list, tuple)):
        return any(_mentions_acquire(v, variable_keys) for v in node)
    return isinstance(node, str) and "acquire" in node.lower()


def _can_rename(node):
    """True when a template sets ``name`` or any attribute that changes the name Kometa gives a collection, at any depth."""
    if isinstance(node, dict):
        return any(str(k).strip().lower() in UNRESOLVABLE_NAME_KEYS + ("name",) or _can_rename(v) for k, v in node.items())
    if isinstance(node, (list, tuple)):
        return any(_can_rename(v) for v in node)
    return False


_BUILTINS = []


def _builtin_templates():
    """{name: definition} of the templates Kometa ships in its own defaults/templates.yml; {} when it cannot be read (then nothing counts as built in).
    Only a successful read is remembered."""
    if _BUILTINS:
        return _BUILTINS[0]
    try:
        data = _read_yaml(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "defaults", "templates.yml"))
        found = data.get("templates") if isinstance(data, dict) else None
        built = {k: v for k, v in found.items() if isinstance(k, str)} if isinstance(found, dict) else {}
    except Exception:
        return {}
    if built:
        _BUILTINS.append(built)
    return built


def _dynamic_blocker(data, defined, builtin):
    """Why a file's dynamic_collections might carry ``acquire`` (so the scan cannot say which names it owns), or None when it provably cannot.

    Safe only when (a) no block mentions ``acquire`` anywhere, and (b) every template a block names is defined in the file without ``acquire``, or is one of
    Kometa's own built-in templates (``builtin``: empty unless the file's external_templates is absent or the shipped default), which do not carry it.
    An unknown template name keeps the scan aborting.
    """
    blocks = data.get("dynamic_collections")
    if not isinstance(blocks, dict):
        return "its dynamic_collections is not a mapping"
    for key, block in blocks.items():
        if not isinstance(block, dict):
            return f"dynamic collection {key} is not a mapping"
        if _mentions_acquire(block) or _mentions_acquire(key):
            return f"dynamic collection {key} mentions acquire"
        try:
            lowered = _lower(block)
        except ValueError:
            return f"dynamic collection {key} has an attribute whose key is a template variable"
        names = []
        for field in ("template", "other_template"):
            value = lowered.get(field)
            for item in value if isinstance(value, list) else [value]:
                name = item.get("name") if isinstance(item, dict) else item
                if item is not None:
                    names.append(name)
        for name in names:
            if not isinstance(name, str) or "<<" in name:
                return f"dynamic collection {key} names a template this scan cannot read"
            if isinstance(defined.get(name), dict):
                if _mentions_acquire(defined[name]):
                    return f"dynamic collection {key} uses template {name}, which mentions acquire"
            elif name not in builtin or _mentions_acquire(builtin[name], variable_keys=False):
                return f"dynamic collection {key} names template {name}, which is neither defined in a scanned file nor a Kometa built-in"
    return None


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
                blocker = _dynamic_blocker(data, data["templates"] if isinstance(data.get("templates"), dict) else {}, _builtins_for(data))
                if blocker:
                    return None, f"{os.path.basename(path)} has dynamic_collections whose names this scan cannot resolve: {blocker}"
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
                    sources = _sources(block, templates, _builtins_for(data))
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
    session = getattr(api, "_raw", api).session  # arrapi's RadarrAPI holds its requests.Session on the raw client
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

"""Notice a new or edited collection definition in the config and say which collections to run (#5634 S8).

The import trigger watches the collection files config.yml lists (inotify, no timer), waits for a burst of writes to
go quiet, reads every file, and hashes each collection's definition. The caller diffs the hashes against the live
record and queues a scoped Kometa run of what is new or changed.

A collection's definition is its block under ``collections:`` plus the templates it names and the
``template_variables`` of the config.yml entry that lists its file: everything that decides what Kometa builds.
Files the generator rewrites (``text_file`` lists) are not part of it; only their path is.
A file that cannot be read or parsed right now is *held*: it is reported as such and judged on the next write.
Only the stdlib and PyYAML are used.
"""

import ctypes
import ctypes.util
import glob
import hashlib
import json
import os
import select
import struct
import time

import yaml

IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_Q_OVERFLOW = 0x00004000
WATCH_MASK = IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE
EVENT_HEADER = struct.Struct("iIII")
YAML_SUFFIXES = (".yml", ".yaml")
# Collection-file entries that are Kometa's own (not ours to watch).
LOCAL_KEYS = ("file", "folder")


def _read_yaml(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def resolve(path, root):
    """Kometa runs from the image root, so ``config/movies/x.yml`` means ``<root>/config/movies/x.yml``."""
    return path if os.path.isabs(path) else os.path.join(root, path)


def listed_files(config, root):
    """{library key: [(path, template_variables)]} for the local collection files config.yml lists.

    ``folder`` entries expand to their *.yml files; ``default``/``git``/``url``/``repo`` entries are Kometa's and skipped."""
    out = {}
    for library, attrs in ((config or {}).get("libraries") or {}).items():
        entries = []
        for entry in (attrs.get("collection_files") if isinstance(attrs, dict) else None) or []:
            if not isinstance(entry, dict):
                continue
            variables = entry.get("template_variables") if isinstance(entry.get("template_variables"), dict) else {}
            if isinstance(entry.get("file"), str):
                entries.append((resolve(entry["file"], root), variables))
            if isinstance(entry.get("folder"), str):
                folder = resolve(entry["folder"], root)
                entries.extend((path, variables) for path in sorted(glob.glob(os.path.join(folder, "*.yml")) + glob.glob(os.path.join(folder, "*.yaml"))))
        out[str(library)] = entries
    return out


def _digest(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _template_names(block):
    template = block.get("template") if isinstance(block, dict) else None
    items = template if isinstance(template, list) else [template]
    names = []
    for item in items:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str):
            names.append(name)
    return names


def definitions(path, variables):
    """{mapping_name: hash} for one collection file. Raises on a file that cannot be read or is not a usable document."""
    data = _read_yaml(path)
    if not isinstance(data, dict):
        raise ValueError("not a mapping document (empty or truncated)")
    collections = data.get("collections")
    if collections is None:
        return {}
    if not isinstance(collections, dict):
        raise ValueError("collections is not a mapping")
    templates = data["templates"] if isinstance(data.get("templates"), dict) else {}
    return {str(name): _digest(block, {t: templates.get(t) for t in _template_names(block)}, variables) for name, block in collections.items()}


def scan(config_path, root):
    """Read config.yml and every collection file it lists.

    Returns (found, held, watch_dirs): found maps (library, mapping_name) -> (path, hash); held maps each path that could
    not be read now to the error class name; watch_dirs are the directories to watch. A config.yml that cannot be read raises, so
    the caller keeps the last answer."""
    config = _read_yaml(config_path)
    if not isinstance(config, dict):
        raise ValueError("config.yml is not a mapping document")
    found, held, dirs = {}, {}, {os.path.dirname(config_path)}
    for library, entries in listed_files(config, root).items():
        for path, variables in entries:
            dirs.add(os.path.dirname(path))
            try:
                hashes = definitions(path, variables)
            except (OSError, ValueError, yaml.YAMLError) as e:
                held[path] = type(e).__name__
                continue
            for name, digest in hashes.items():
                found[(library, name)] = (path, digest)
    return found, held, dirs


def libc_inotify():
    lib = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    return lib.inotify_init1, lib.inotify_add_watch


class Watcher:
    """inotify on directories (a rename-into-place replaces the file's inode, so a file watch would go blind)."""

    def __init__(self, wanted, init=None, add=None):
        if init is None or add is None:
            init, add = libc_inotify()
        self.wanted = wanted  # filenames that matter, e.g. a predicate on the basename
        self._add = add
        self.fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.dirs = set()

    def watch(self, dirs):
        for directory in set(dirs) - self.dirs:
            if self._add(self.fd, directory.encode(), WATCH_MASK) >= 0:
                self.dirs.add(directory)

    def drain(self):
        """Read what is queued. Returns True when a relevant file changed (or the kernel queue overflowed)."""
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return False
        relevant, pos = False, 0
        while pos + EVENT_HEADER.size <= len(data):
            _wd, mask, _cookie, length = EVENT_HEADER.unpack_from(data, pos)
            name = data[pos + EVENT_HEADER.size : pos + EVENT_HEADER.size + length].split(b"\0", 1)[0].decode(errors="replace")
            pos += EVENT_HEADER.size + length
            if mask & IN_Q_OVERFLOW or self.wanted(name):
                relevant = True
        return relevant

    def wait(self, timeout=None):
        """Block until the fd is readable or ``timeout`` passes (None = forever). True when readable."""
        return bool(select.select([self.fd], [], [], timeout)[0])

    def settle(self, quiet):
        """After a relevant event: return once no relevant event has arrived for ``quiet`` seconds."""
        deadline = time.monotonic() + quiet
        while (remaining := deadline - time.monotonic()) > 0:
            if self.wait(remaining) and self.drain():
                deadline = time.monotonic() + quiet

    def close(self):
        os.close(self.fd)


def relevant_name(name):
    return name.endswith(YAML_SUFFIXES)

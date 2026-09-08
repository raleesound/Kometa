"""Assert this fork's own changes survived a merge from upstream.

Every night this fork merges Kometa-Team/Kometa. A merge can be textually clean
and still remove our behaviour — upstream refactors the surrounding code, or a
conflict is resolved in upstream's favour, and a feature quietly stops existing.
Nothing else in the suite would notice: upstream's tests pass perfectly well
without our additions.

That failure is silent and expensive. It is the shape where a feature is built,
correct, and simply not running any more, which nobody discovers until they go
looking for why the thing they rely on stopped happening.

So each entry below pins one thing this fork owns. These are deliberately
shallow marker checks rather than behavioural tests — the behaviour is covered
by the normal suite; what this file catches is *deletion*.

When intentionally removing or renaming one of our features, delete or update
its entry here in the same commit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# (path, marker, why it matters)
MUST_BE_PRESENT = [
    (
        "kometa.py",
        '"run-items"',
        "--run-items scopes operations to specific rating keys; the import trigger depends on it",
    ),
    (
        "kometa.py",
        "unrecognised = [",
        "unknown flags must fail rather than silently start a full run",
    ),
    (
        "kometa.py",
        "run_lock_path",
        "one Kometa run at a time per config dir; item runs step aside for a sweep",
    ),
    (
        "modules/logs.py",
        "ITEMS_LOG",
        "item-scoped runs must not rotate a sweep's meta.log",
    ),
    (
        "modules/plex.py",
        "def get_items_by_rating_key",
        "fetches only the requested items and rejects keys from another library",
    ),
    (
        "modules/plex.py",
        "def batch_lock_field",
        "lock-only batch edit; editField would resend the value and clobber the upload",
    ),
    (
        "modules/plex.py",
        "def flush_image_locks",
        "coalesces the per-item image lock PUTs that once deadlocked Plex",
    ),
    (
        "modules/plex.py",
        "IMAGE_LOCK_FIELDS",
        "maps image types onto the Plex field whose .locked flag is set",
    ),
    (
        "modules/library.py",
        "defer_image_locks",
        "the flag image_update() reads to queue a lock instead of sending it",
    ),
    (
        "modules/library.py",
        "if not self.config.run_items:",
        "a per-item run must not parse every collection file (63s of startup)",
    ),
    (
        "modules/config.py",
        "self.run_items",
        "parses --run-items into rating keys for the operations walk",
    ),
    (
        "modules/operations.py",
        "def _skip_show_level_image",
        "season/episode images honour ignore_locked; without it they never converge",
    ),
    (
        "modules/operations.py",
        "def _sub_field_locked",
        "reads the lock state of a season or episode rather than its show",
    ),
]

# Things upstream must not bring back.
MUST_BE_ABSENT = [
    (
        "modules/builder.py",
        "collection_order: custom can only be used with a single builder",
        "the multi-builder gate we removed to allow ordered multi-list collections",
    ),
]


def _read(relative_path: str) -> str:
    path = REPO_ROOT / relative_path
    assert path.exists(), f"{relative_path} is missing from the fork entirely"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("relative_path,marker,reason", MUST_BE_PRESENT, ids=[f"{p}:{m}" for p, m, _ in MUST_BE_PRESENT])
def test_fork_feature_survived(relative_path: str, marker: str, reason: str):
    assert marker in _read(relative_path), (
        f"{relative_path} no longer contains {marker!r}.\n"
        f"This fork owns that code: {reason}.\n"
        f"An upstream merge has most likely dropped it. Restore it, or if the removal was "
        f"deliberate, remove this entry from MUST_BE_PRESENT in the same commit."
    )


@pytest.mark.parametrize("relative_path,marker,reason", MUST_BE_ABSENT, ids=[f"{p}:absent" for p, _, _ in MUST_BE_ABSENT])
def test_removed_upstream_behaviour_stays_removed(relative_path: str, marker: str, reason: str):
    assert marker not in _read(relative_path), f"{relative_path} contains {marker!r} again.\n" f"This fork deliberately removed it: {reason}.\n" f"An upstream merge has reintroduced it."


def test_no_conflict_markers_anywhere():
    """A merge resolved by hand or by an agent must leave nothing behind.

    Conflict markers in Python are a syntax error and would fail loudly, but in
    YAML, Markdown or a JSON schema they parse or are ignored, and ship.
    """
    # "<<<<<<<" split up so this file never matches its own check.
    markers = ("<" * 7, ">" * 7)
    offenders = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or ".git/" in str(path):
            continue
        if path.suffix.lower() not in {".py", ".yml", ".yaml", ".json", ".md", ".txt", ".cfg", ".toml"}:
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(f"\n{m}" in f"\n{text}" for m in markers):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"unresolved merge conflict markers in: {', '.join(sorted(offenders))}"

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


# The only workflows this fork runs, and the in-cluster ARC pools they may use.
# Upstream's own workflows stay in the tree but are disabled; upstream-sync.yml
# disables any new one after each merge. Nothing here may run on a GitHub-hosted
# runner.
FORK_WORKFLOWS = {
    "fork-ci.yml": {"kometa-ci"},
    "upstream-sync.yml": {"kometa-ci", "kometa-sync"},
}


def _jobs(workflow: str) -> dict:
    from ruamel.yaml import YAML

    data = YAML(typ="safe").load(_read(f".github/workflows/{workflow}"))
    return data.get("jobs") or {}


@pytest.mark.parametrize("workflow,pools", FORK_WORKFLOWS.items(), ids=list(FORK_WORKFLOWS))
def test_fork_workflows_run_only_on_fork_pools(workflow: str, pools: set):
    jobs = _jobs(workflow)
    assert jobs, f".github/workflows/{workflow} has no jobs"
    offenders = {name: job.get("runs-on") for name, job in jobs.items() if job.get("runs-on") not in pools}
    assert not offenders, f"{workflow} has jobs on runners other than {sorted(pools)}: {offenders}.\n" f"This fork runs nothing on GitHub-hosted runners; use the in-cluster ARC pool."


@pytest.mark.parametrize("workflow", FORK_WORKFLOWS)
def test_fork_workflow_jobs_are_guarded_to_this_repository(workflow: str):
    """A copy of the fork elsewhere must not run these on whatever runners it has."""
    unguarded = [name for name, job in _jobs(workflow).items() if "github.repository == 'raleesound/Kometa'" not in str(job.get("if", ""))]
    assert not unguarded, f"{workflow} jobs without the same-repo guard: {unguarded}"


def test_upstream_sync_splits_untrusted_gate_from_trusted_land():
    """The gate runs repo code on kometa-ci with no secrets; the job holding the
    write token runs on kometa-sync and never executes repo code."""
    jobs = _jobs("upstream-sync.yml")
    assert jobs["gate"]["runs-on"] == "kometa-ci"
    assert jobs["land"]["runs-on"] == "kometa-sync"
    assert jobs["land"]["needs"] == "gate"
    gate = str(jobs["gate"])
    assert "secrets." not in gate, "the gate job runs repository code and must not see secrets"
    land = str(jobs["land"])
    assert "./.github/actions" not in land and "uv " not in land and "pytest" not in land, "the land job must not execute repository code"
    assert "--no-ff" in land and "[skip ci]" in land


def test_upstream_sync_disables_workflows_it_does_not_own():
    """The sync is what keeps upstream's workflows off. If it stops listing ours, it
    would disable fork-ci; if the step goes, new upstream workflows would run."""
    sync = _read(".github/workflows/upstream-sync.yml")
    assert "Disable workflows this fork does not run" in sync
    for workflow in FORK_WORKFLOWS:
        assert workflow in sync, f"upstream-sync.yml must exempt {workflow} when disabling workflows"

"""Guard the host deploy tree in deploy/.

deploy/ is the only copy of the scripts that build and run this fork on the
media host (deploy.sh, the import trigger, the curated collection YAML). The
host runs a git checkout of it, so a broken commit here is a broken deploy the
next time someone runs deploy.sh. These checks are cheap and static: nothing
here talks to Docker, Plex or the host.
"""

from __future__ import annotations

import py_compile
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY = REPO_ROOT / "deploy"
SCRIPTS = sorted(DEPLOY.glob("*.sh"))


def _env_example_keys() -> set[str]:
    keys = set()
    for line in (DEPLOY / ".env.example").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*([A-Z_][A-Z0-9_]*)=", line)
        if match:
            keys.add(match.group(1))
    return keys


def test_scripts_exist():
    names = {p.name for p in SCRIPTS}
    assert {"deploy.sh", "down.sh", "logs.sh", "ps.sh"} <= names


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs a POSIX bash")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_parse(script):
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)  # nosec B603 B607
    assert result.returncode == 0, result.stderr


def test_env_example_covers_every_variable_deploy_reads_from_env():
    # deploy.sh runs under `set -u` after `source ./.env`, so any ${VAR} it
    # neither assigns itself nor defaults with ${VAR:-...} must come from .env.
    # A fresh host built from .env.example would otherwise die mid-deploy.
    lines = (DEPLOY / "deploy.sh").read_text(encoding="utf-8").splitlines()
    text = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    used = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)\}", text))
    assigned = set(re.findall(r"^\s*([A-Z_][A-Z0-9_]*)=", text, flags=re.MULTILINE))
    missing = sorted(used - assigned - _env_example_keys())
    assert not missing, f".env.example is missing keys deploy.sh requires: {missing}"


def test_branch_name_is_not_hardcoded():
    text = (DEPLOY / "deploy.sh").read_text(encoding="utf-8")
    assert "--build-arg BRANCH_NAME=nightly" not in text
    assert 'BRANCH_NAME="${BRANCH_NAME:-nightly}"' in text


def test_kometa_ref_is_a_full_sha():
    ref = (DEPLOY / "KOMETA_REF").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"[0-9a-f]{40}", ref), "KOMETA_REF must pin a full commit SHA"


def test_import_trigger_compiles(tmp_path):
    py_compile.compile(str(DEPLOY / "trigger" / "import_trigger.py"), cfile=str(tmp_path / "t.pyc"), doraise=True)


def test_collections_are_synced_dirs_only():
    # deploy.sh only rsyncs these four dirs; a collection file anywhere else
    # would sit in git and never reach the live config.
    allowed = {"movies", "tv", "playlists", "overlays"}
    present = {p.name for p in (DEPLOY / "collections").iterdir() if p.is_dir()}
    assert present <= allowed, f"unsynced collection dirs: {sorted(present - allowed)}"


def test_no_inline_secrets():
    pattern = re.compile(r"(token|apikey|api_key|password|secret)['\" ]*[:=]['\" ]*[A-Za-z0-9_\-]{12,}", re.IGNORECASE)
    offenders = []
    for path in DEPLOY.rglob("*"):
        if path.is_file() and path.suffix in {".yml", ".yaml", ".txt", ".sh", ".py", ".md", ".example"}:
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{n}")
    assert not offenders, f"possible secret values committed: {offenders}"


def test_deploy_tree_is_kept_out_of_the_image():
    lines = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "deploy" in lines

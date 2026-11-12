#!/usr/bin/env bash
#
# Deploy our Kometa fork to this host. Runs ON 10.3.16.4 from a git checkout
# of this deploy/ tree (raleesound/Kometa, deploy branch) — see README.md.
#
# What this does, in order:
#   1. Fetch our fork (raleesound/Kometa) at the pinned ref in KOMETA_REF.
#   2. Build the base image (deps, from the fork's own requirements.txt) and
#      the app image (our source) using upstream's OWN two Dockerfiles,
#      unmodified — so the merge bot never has to reconcile a custom one.
#   3. Sync curated collection YAML from collections/ into the live config dir.
#   4. Recreate the container with the same runtime shape it has today.
#
# It does NOT touch:
#   - config.yml            — host-owned, Kometa writes to it (Trakt OAuth
#                             refresh, default backfill). See ../README.md.
#   - config.cache, logs/, anidb_cache/, UUID, *_report.yml — runtime state.
#   - movies/assets/        — 285MB of poster binaries, host-local by design.
#
set -euo pipefail

cd "$(dirname "$0")"
# shellcheck source-path=SCRIPTDIR source=.env.example
source ./.env

# TZ is load-bearing: without it the container runs UTC and KOMETA_TIMES is
# interpreted in UTC, which is how a run landed inside Plex's butler window
# (ButlerStartHour=3/EndHour=7) and deadlocked Plex on 2026-09-03.
if [[ -z "${TZ:-}" ]]; then
  echo "FATAL: TZ is not set in .env - refusing to recreate the container in UTC" >&2
  exit 1
fi

SRC_DIR="${SRC_DIR:-/home/rob/kometa-src}"
FORK_URL="${FORK_URL:-https://github.com/raleesound/Kometa.git}"
REF="$(tr -d '[:space:]' < KOMETA_REF)"
IMAGE="${IMAGE:-kometa-fork}"
# BRANCH_NAME is baked into the image as an env var that Kometa only uses to
# label its version/update check (kometa.py env_branch). It does NOT choose
# what gets built — KOMETA_REF does. Defaults to the channel the fork tracks.
BRANCH_NAME="${BRANCH_NAME:-nightly}"

if [[ -z "$REF" ]]; then
  echo "FATAL: KOMETA_REF is empty" >&2
  exit 1
fi

# Every Kometa run (scheduled sweep or the trigger's item-scoped run) holds an
# exclusive flock on this file while it runs. Recreating a container mid-run
# kills that run, so refuse unless FORCE=1. Checked before the (slow) build and
# again right before the recreate, since a scheduled run can start in between.
RUN_LOCK="${CONFIG_DIR}/.kometa.run.lock"
require_idle() {
  if [[ -e "$RUN_LOCK" ]] && ! flock -n -s "$RUN_LOCK" true; then
    if [[ "${FORCE:-0}" == "1" ]]; then
      echo "    FORCE=1 -- Kometa is mid-run; recreating anyway"
    else
      echo "FATAL: Kometa is mid-run (${RUN_LOCK} is locked). Recreating now would kill it." >&2
      echo "       Wait for the run to finish, or re-run with FORCE=1." >&2
      exit 1
    fi
  fi
}
require_idle

# The import trigger publishes kometa.item.arrived to NATS as the nkey user
# kometa-trigger. Its seed lives in an env file on this host (NATS_NKEY_SEED=...),
# never in .env and never on a command line. Checked before the slow build.
NATS_ENV_FILE="${NATS_ENV_FILE:-$(pwd)/.env.nats}"
NATS_URL="${NATS_URL:-nats://10.43.155.192:4222}"
if [[ ! -f "$NATS_ENV_FILE" ]]; then
  echo "FATAL: ${NATS_ENV_FILE} is missing (NATS_NKEY_SEED for the kometa-trigger nkey user)" >&2
  exit 1
fi
if [[ "$(stat -c %a "$NATS_ENV_FILE")" != "600" ]]; then
  echo "FATAL: ${NATS_ENV_FILE} must be mode 0600 (it holds an nkey seed)" >&2
  exit 1
fi
if ! grep -q '^NATS_NKEY_SEED=.' "$NATS_ENV_FILE"; then
  echo "FATAL: ${NATS_ENV_FILE} has no NATS_NKEY_SEED" >&2
  exit 1
fi

echo "==> Fetching fork at ${REF}"
if [[ ! -d "$SRC_DIR/.git" ]]; then
  git clone "$FORK_URL" "$SRC_DIR"
fi
git -C "$SRC_DIR" fetch --tags origin
git -C "$SRC_DIR" checkout --detach "$REF"
BUILT_SHA="$(git -C "$SRC_DIR" rev-parse HEAD)"
echo "==> Building ${BUILT_SHA}"

# Upstream's Dockerfile is a thin `COPY . /` over a base image carrying the
# deps. We build BOTH from the fork so the image always matches the fork's own
# requirements.txt, rather than trusting upstream's published :base to still
# agree with us once our diff is non-empty.
#
# Careful: that Dockerfile is `FROM kometateam/kometa:${BASE_TAG}` — the
# REPOSITORY is hardcoded, so BASE_TAG only picks a tag under upstream's name.
# Passing BASE_TAG=base silently builds against upstream's PUBLISHED base and
# ignores whatever we just built. Harmless while our diff is empty and the deps
# match; the moment requirements.txt diverges the image ships the wrong deps and
# still looks fine. So we tag our locally-built base under the name the
# Dockerfile expects, with a tag upstream does not publish. Docker resolves the
# local tag, and we avoid patching the Dockerfile (keeping the merge bot's job
# trivial).
docker build -f "$SRC_DIR/Dockerfile.base" -t "${IMAGE}:base" "$SRC_DIR"
docker tag "${IMAGE}:base" kometateam/kometa:forkbase
docker build -f "$SRC_DIR/Dockerfile" \
  --build-arg BASE_TAG=forkbase \
  --build-arg BRANCH_NAME="${BRANCH_NAME}" \
  -t "${IMAGE}:${BUILT_SHA}" -t "${IMAGE}:local" "$SRC_DIR"

# Assert we actually built on OUR base, not upstream's published one.
if ! docker history --no-trunc "${IMAGE}:local" --format '{{.CreatedBy}}' \
     | grep -q .; then
  echo "FATAL: could not inspect image history" >&2
  exit 1
fi
BASE_ID=$(docker image inspect "${IMAGE}:base" --format '{{.Id}}')
APP_BASE=$(docker image inspect "${IMAGE}:local" --format '{{index .RootFS.Layers 0}}')
BASE_FIRST=$(docker image inspect "${IMAGE}:base" --format '{{index .RootFS.Layers 0}}')
if [[ "$APP_BASE" != "$BASE_FIRST" ]]; then
  echo "FATAL: app image was not built on ${IMAGE}:base (${BASE_ID})" >&2
  echo "       check the FROM/BASE_TAG wiring in $SRC_DIR/Dockerfile" >&2
  exit 1
fi

# Guard: upstream's .dockerignore excludes `venv` but NOT `.venv`, and the
# Dockerfile is `COPY . /`. A stray .venv in the source tree gets baked into
# the image root. Fail loudly rather than shipping a 300MB surprise.
if docker run --rm --entrypoint sh "${IMAGE}:local" -c '[ -d /.venv ]'; then
  echo "FATAL: /.venv leaked into the image — check .dockerignore" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Drift guard. `collections/` here is the source of truth and the rsync below
# is `-a --delete`, so ANY edit made directly to the live config dir is
# silently reverted with no log line. On 2026-09-08 that ate five files,
# including "Rob's Monthly List" (flipped back to August's Letterboxd list) and
# the whole Halloweeners collection.
#
# So: after every successful sync we record a checksum of each file we wrote.
# Before the next sync we re-check them. A file that no longer matches was
# edited live, behind this script's back -- refuse rather than clobber it.
# Reconcile by copying the change into collections/ (and commit it), or set
# ALLOW_CONFIG_CLOBBER=1 to deliberately discard the live edits.
MANIFEST="${CONFIG_DIR}/.kometa-deploy-manifest.sha256"
if [[ -f "$MANIFEST" ]]; then
  echo "==> Checking live config for edits made outside collections/"
  DRIFT=""
  while read -r sum file; do
    [[ -n "$sum" ]] || continue
    if [[ ! -f "$file" ]]; then
      DRIFT+="  DELETED LIVE: $file"$'\n'
    elif [[ "$(sha256sum "$file" | awk '{print $1}')" != "$sum" ]]; then
      DRIFT+="  EDITED LIVE:  $file"$'\n'
    fi
  done < "$MANIFEST"
  if [[ -n "$DRIFT" ]] && [[ "${ALLOW_CONFIG_CLOBBER:-0}" != "1" ]]; then
    echo "FATAL: live Kometa config has edits that are NOT in collections/." >&2
    echo "       Syncing now would silently revert them:" >&2
    printf '%s' "$DRIFT" >&2
    echo "       Copy them into collections/ first, or re-run with" >&2
    echo "       ALLOW_CONFIG_CLOBBER=1 to discard them on purpose." >&2
    exit 1
  fi
  [[ -n "$DRIFT" ]] && echo "    ALLOW_CONFIG_CLOBBER=1 -- discarding the live edits above"
fi

echo "==> Syncing curated collection YAML -> ${CONFIG_DIR}"
# --delete so a collection file removed from git is removed here too. Scoped to
# the curated dirs ONLY. Backups predating git are preserved until the import is
# proven; drop --exclude once you trust the history.
for d in movies tv playlists overlays; do
  [[ -d "collections/$d" ]] || continue
  mkdir -p "${CONFIG_DIR}/${d}"
  rsync -a --delete \
    --include='*/' --include='*.yml' --include='*.yaml' --include='*.txt' --exclude='*' \
    --filter='protect *.bak*' \
    "collections/${d}/" "${CONFIG_DIR}/${d}/"
done

# Record what we just wrote, so the drift guard above can spot a live edit on
# the next run. Same filter as the rsync, so the two can never disagree.
: > "$MANIFEST"
for d in movies tv playlists overlays; do
  [[ -d "${CONFIG_DIR}/${d}" ]] || continue
  find "${CONFIG_DIR}/${d}" -type f \( -name '*.yml' -o -name '*.yaml' -o -name '*.txt' \) \
    ! -name '*.bak*' -print0 | sort -z | xargs -0 -r sha256sum >> "$MANIFEST"
done
echo "    recorded $(wc -l < "$MANIFEST") files in $MANIFEST"

require_idle
echo "==> Recreating container"
docker rm -f kometa >/dev/null 2>&1 || true
docker run -d \
  --name kometa \
  --restart unless-stopped \
  --network "${NETWORK}" \
  -v "${DATA_DIR}:/data:rw" \
  -v "${CONFIG_DIR}:/config:rw" \
  -e KOMETA_TIMES="${KOMETA_TIMES}" \
  -e TZ="${TZ}" \
  "${IMAGE}:local"

echo "==> Recreating import trigger"
# Same image as the scheduler, so the trigger is always version-locked to the fork it
# shells out to. It listens on Plex's websocket (outbound only, no published port).
# It refuses to start if that build has no --run-items (Kometa ignores unknown
# flags and would run a full sweep per import instead). It publishes the arrival
# outbox to NATS (#5634), so it gets the nkey seed by env file.
docker rm -f kometa-import-trigger >/dev/null 2>&1 || true
docker run -d \
  --name kometa-import-trigger \
  --restart unless-stopped \
  --network "${NETWORK}" \
  -v "${CONFIG_DIR}:/config:rw" \
  -v "$(pwd)/trigger:/trigger:ro" \
  -e TZ="${TZ}" \
  -e DEBOUNCE_SECONDS="${TRIGGER_DEBOUNCE_SECONDS}" \
  -e NATS_URL="${NATS_URL}" \
  --env-file "${NATS_ENV_FILE}" \
  --entrypoint python3 \
  "${IMAGE}:local" /trigger/import_trigger.py

echo "==> Deployed ${BUILT_SHA}"
docker ps --filter name=kometa --format '{{.Names}}\t{{.Image}}\t{{.Status}}'

---
title: Kometa (forked) — host deploy tree
summary: How the media host (10.3.16.4) builds and runs this fork, from a git checkout of deploy/.
type: deployment
---

# Kometa (forked) — host deploy tree

Kometa manages Plex collections, overlays, and library operations. We run a
**fork** (`raleesound/Kometa`) because Kometa has no plugin or hook system and
its config schema is `additionalProperties: false` at every level — any
feature we want is necessarily an in-tree change.

This `deploy/` directory is everything that turns the fork into running
containers on the media host: the build/recreate script, the import trigger,
and the curated collection YAML. It used to exist only as loose files on the
host; it now lives here, on the fork's deploy branch, and the host runs a git
checkout of it.

## Where things live

| Thing | Owner | Why |
|---|---|---|
| `deploy/collections/{movies,tv,playlists,overlays}/` | **git** | Curation. No secrets, no write-back. This is what actually gets edited. |
| `deploy/KOMETA_REF` | **git** | The fork commit the host builds. Change it in a commit. |
| `deploy/trigger/import_trigger.py` | **git** | Per-item import trigger, bind-mounted read-only into its container. |
| `deploy/.env` | **host** | Paths, network, schedule, TZ. Gitignored; `.env.example` lists every key `deploy.sh` needs. |
| `config.yml` (in `CONFIG_DIR`) | **host** | Contains live secrets AND Kometa writes to it (Trakt OAuth refresh, default backfill). See below. |
| `movies/assets/*.png` (in `CONFIG_DIR`) | **host** | ~285MB of poster binaries. |
| `config.cache`, `logs/`, `anidb_cache/`, `UUID`, `*_report.yml` | **host** | Runtime state. Never synced, never in git. |

## The host checkout

On the host the deploy branch is a sparse checkout of this repo containing
only `deploy/`:

```
/home/rob/kometa-deploy-git/          git clone, sparse-checkout: /deploy/
/home/rob/kometa-deploy  ->  /home/rob/kometa-deploy-git/deploy   (symlink)
```

Everything that refers to `/home/rob/kometa-deploy/…` keeps working through the
symlink. Host-only files (`.env`, `*.bak*`, `deploy-*.log`) sit in the same
directory and are gitignored.

To change anything — a collection, the trigger, the pinned ref — commit it to
the deploy branch (PR), then on the host:

```
cd /home/rob/kometa-deploy-git && git pull --ff-only
```

`git status` there should be clean. A dirty tree means someone edited on the
host; commit that change or discard it before deploying.

This checkout is deliberately **not** `SRC_DIR` (`/home/rob/kometa-src`):
`deploy.sh` detaches `SRC_DIR` to `KOMETA_REF`, and doing that to the checkout
holding the running script would rewrite `deploy.sh` under bash's feet.

## collections/ is rsynced over the live config

`deploy.sh` copies `collections/{movies,tv,playlists,overlays}/` into
`CONFIG_DIR` with `rsync -a --delete` (only `*.yml`, `*.yaml`, `*.txt`;
`*.bak*` protected). So **the git copy wins**:

- Edit collections here, in git. A YAML edit made only in the live config dir
  is reverted by the next deploy.
- If you must hot-fix the live file (to get it into tonight's run), make the
  same edit here too — **both paths** — or the next deploy will refuse.
- The refusal is the drift guard: after each sync `deploy.sh` records a
  checksum of every file it wrote in `CONFIG_DIR/.kometa-deploy-manifest.sha256`,
  and before the next sync it fails if any of them changed live. Reconcile
  the change into `collections/`, or run with `ALLOW_CONFIG_CLOBBER=1` to
  discard it on purpose.

## Deploying

```
/home/rob/kometa-deploy/deploy.sh   # fetch fork at KOMETA_REF, build, sync YAML, recreate
/home/rob/kometa-deploy/ps.sh       # container status
/home/rob/kometa-deploy/logs.sh     # tail kometa logs
/home/rob/kometa-deploy/down.sh     # stop kometa
```

`deploy.sh` fetches the fork at `KOMETA_REF`, builds the base and app images
with upstream's own `Dockerfile.base` + `Dockerfile` (unmodified, so the
upstream merge bot never reconciles a custom build file), syncs the curated
YAML, and recreates both containers.

`BRANCH_NAME` (optional in `.env`, default `nightly`) is only the label Kometa
uses for its version/update check. It does not choose what gets built —
`KOMETA_REF` does.

### Before recreating anything: is Kometa mid-run?

A recreate kills a run in progress. Every Kometa run (scheduled sweeps and
the trigger's item-scoped runs alike) holds an exclusive `flock` on
`$CONFIG_DIR/.kometa.run.lock` while it runs (`kometa.py` `process()`). The
file stays behind after the first run, so check whether it is *locked*, not
whether it exists. From the deploy dir on the host:

```
source .env && flock -n -s "$CONFIG_DIR/.kometa.run.lock" true && echo idle || echo RUNNING
```

Only recreate when it says `idle`. "idle" means no run is in progress; the
scheduler container itself stays up and sleeps until the next `KOMETA_TIMES`
slot (host TZ). `deploy.sh` runs this check itself, before the build and again
right before it recreates anything, and refuses while a run holds the lock.
`FORCE=1` overrides it (kills the run in progress).

### Containers are recreated by hand, never by compose

Both containers run `kometa-fork:local` (the image `deploy.sh` just built):

- `kometa` — the scheduler (`KOMETA_TIMES`, long-lived, sleeps between runs)
- `kometa-import-trigger` — the same image, entrypoint `python3 /trigger/import_trigger.py`

They are created by the `docker run` blocks at the end of `deploy.sh`, **not**
by docker compose. The compose block for Kometa on the host is intentionally
commented out; bringing it back would start a second Kometa (or upstream's
image) against the same config. To recreate one container without a rebuild,
run that container's `docker rm -f` + `docker run` block from `deploy.sh` by
hand, with `.env` sourced — after the lock check above.

## Rolling back

Commit the previous SHA to `KOMETA_REF` and redeploy. Every build is also
tagged `kometa-fork:<sha>` locally on the host, so a rollback needs no rebuild.

## Container shape

- network `media-net`, restart `unless-stopped`, `TZ` from `.env` (required:
  KOMETA_TIMES is interpreted in the container's zone; a UTC container once
  ran inside Plex's butler window and deadlocked Plex on 2026-09-03)
- `kometa`: `DATA_DIR` → `/data`, `CONFIG_DIR` → `/config`
- `kometa-import-trigger`: `CONFIG_DIR` → `/config`, `./trigger` → `/trigger:ro`,
  `--env-file .env.nats` (the NATS nkey seed), `NATS_URL`, no published port (it
  connects out to Plex's websocket and to NATS)

## The import trigger

`trigger/import_trigger.py` listens on Plex's local websocket
(`/:/websockets/notifications`, URL and token from `config.yml`) and runs
`kometa.py --operations-only --run-items <keys>` for new imports, so a new film
or episode gets its operations within minutes instead of at the next sweep.

- A library timeline entry in state 5 ("processed") counts as an import only if
  Plex added the item within `NEW_ITEM_WINDOW_SECONDS` (default 6h) and it is not
  already in the seen-set. Refreshes, edits and the 2097-pinned films are ignored.
- Episodes queue their show. Keys are debounced (`TRIGGER_DEBOUNCE_SECONDS`) into
  one run per library; `local://` (unmatched) items are retried on a backoff.
- Queue, in-flight batch, seen-set and catch-up horizon persist in
  `CONFIG_DIR/.kometa-trigger-queue.json`. Every (re)connect, once the socket is
  open, sweeps the libraries for items added since the last completed sweep
  (capped at `MAX_CATCH_UP_SECONDS`, default 7 days), so a restart, a dropped
  socket or a Plex outage loses nothing. A failed Kometa run, or a crash
  mid-run, puts its batch back in the queue (bounded by `MAX_ATTEMPTS`).
- An item run that finds a sweep holding the run lock exits 75; the trigger waits
  for the lock and runs it again, without charging it an attempt. A scheduled
  sweep that finds an item run holding it waits for it.
- Plex's `library.new` webhook never fired on this server; the old `:9596`
  receiver and its plex.tv webhook registration were removed.

### Arrivals (#5634)

The item run starts with an arrival step (`modules/arrivals.py`): every collection
the live record (`CONFIG_DIR/live-collections.db`, written by each scheduled sweep)
says is waiting for the new item's TMDb id (films) or TVDb id (shows) gets it
through the Plex API, and a custom-ordered collection gets it moved into its
recorded position, checked against Plex's actual order. Only what the record says a
collection wants is ever added, so the next sweep agrees with it. A waiting
collection that is not in Plex yet is queued as a scoped
`--collections-only --run-collections <name>` run, which the trigger runs after
the batch.

Each new item also leaves one `kometa.item.arrived` event (payload `v: 1`, with the
collections it went into, possibly none) in the record's outbox. The trigger
publishes the outbox to JetStream as the nkey user `kometa-trigger`, with
`Nats-Msg-Id: arrived:<library>:<ratingKey>`, and removes a row from the outbox
only once JetStream acknowledged it. A NATS outage, or no stream yet (plex-bot
owns the `KOMETA` stream), only delays an event; the trigger retries on a backoff
and on every reconnect.

The seed lives in `.env.nats` next to `.env` (mode 0600, one line
`NATS_NKEY_SEED=SU...`). `deploy.sh` refuses to run without it; the trigger drops
it from its environment before starting any Kometa run.

## Why config.yml is host-owned

Kometa rewrites its own config file as routine behaviour: Trakt OAuth refresh
persists new tokens (`modules/trakt.py`), MyAnimeList OAuth does the same
(`modules/mal.py`), and `check_for_attribute()` backfills defaults for missing
optional keys on nearly every run (`modules/config.py`). All three are gated by
`--read-only-config`, which defaults to off. Leaving `config.yml` host-owned
lets tokens persist and keeps live secrets out of git.

If a git-tracked file ever needs a secret, Kometa supports `<<secretname>>`
placeholders resolved from `KOMETA_*` env vars or `/config/.env`. Two traps:
**no underscores in the secret name**, and an unresolved placeholder becomes
`None` rather than erroring.

## Gotchas

- **`.dockerignore` excludes `venv` but not `.venv`**, and the Dockerfile is
  `COPY . /`. `deploy.sh` asserts no `/.venv` landed in the image. (`deploy/`
  itself is in `.dockerignore`.)
- **CI tests on Python 3.12; the image runs 3.13.** A merge can be green in CI
  and still fall over in the container.
- **Run lint with CI's exact args** (`--max-line-length=256`). Lint globs are
  `modules/ tests/ *.py`; `tests/test_deploy_tree.py` covers this directory.
- **Pyright ratchets rather than requiring zero.** An upstream merge can fail
  the gate with an empty diff of ours; regenerate `.pyright-baseline.json`.

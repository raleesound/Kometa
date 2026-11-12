"""Apply Kometa library operations to an item as soon as Plex imports it.

Receives Plex's `library.new` webhook and runs ``kometa.py --run-items`` for the
new rating keys, so a newly imported item gets its poster, background, content
rating and labels immediately instead of waiting for the next scheduled sweep.

Runs from the Kometa image itself, so it is always the same build as the
scheduler and needs no Docker socket to reach it — it just runs kometa.py as a
subprocess against the same /config.

Three things make it more than a shell-out:

* **Debounce.** Kometa costs ~17s of startup per invocation regardless of how
  many items it is given, so a batch import should be one run, not twenty.
  Keys collected within DEBOUNCE_SECONDS of each other go out together.
* **Match verification.** `library.new` can fire before Plex has finished agent
  matching, so an item's GUID may still be `local://`. Operations sourced from
  TMDb would find nothing and lock in that nothing. Unmatched keys are retried
  on a backoff instead.
* **Durable queue.** Pending keys are written to disk, so a restart mid-debounce
  does not drop the imports it had accepted.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin
from xml.etree import ElementTree

import requests
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kometa-import-trigger")

CONFIG_PATH = os.environ.get("KOMETA_CONFIG", "/config/config.yml")
KOMETA_PY = os.environ.get("KOMETA_PY", "/kometa.py")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9596"))
QUEUE_PATH = os.environ.get("QUEUE_PATH", "/config/.kometa-trigger-queue.json")
DEBOUNCE_SECONDS = int(os.environ.get("DEBOUNCE_SECONDS", "90"))
# A Plex agent match usually lands within a minute; give up after roughly half an
# hour and leave the item to the scheduled sweep.
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "12"))
RETRY_SECONDS = int(os.environ.get("RETRY_SECONDS", "150"))
RUN_TIMEOUT_SECONDS = int(os.environ.get("RUN_TIMEOUT_SECONDS", "1800"))

# Plex writes a placeholder GUID until its agent has matched the item.
UNMATCHED_GUID = re.compile(r"^local://")


class Pending:
    """Rating keys waiting to be sent to Kometa, grouped by library."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.due_at = 0.0
        # {library: {rating_key: attempts}}
        self.items: dict[str, dict[str, int]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        self.items = {lib: {str(k): int(v) for k, v in keys.items()} for lib, keys in data.get("items", {}).items()}
        if self.items:
            # Anything recovered from disk was already accepted; send it promptly
            # rather than waiting for another webhook to start the clock.
            self.due_at = time.time() + DEBOUNCE_SECONDS
            log.info("recovered %d pending items from %s", sum(len(v) for v in self.items.values()), self.path)

    def _save_locked(self) -> None:
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"items": self.items}, fh)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("could not persist queue: %s", e)

    def add(self, library: str, rating_key: str) -> None:
        with self.lock:
            self.items.setdefault(library, {}).setdefault(str(rating_key), 0)
            self.due_at = time.time() + DEBOUNCE_SECONDS
            self._save_locked()

    def take_due(self) -> dict[str, dict[str, int]]:
        with self.lock:
            if not self.items or time.time() < self.due_at:
                return {}
            taken = self.items
            self.items = {}
            self._save_locked()
            return taken

    def requeue(self, library: str, rating_key: str, attempts: int) -> None:
        with self.lock:
            self.items.setdefault(library, {})[str(rating_key)] = attempts
            self.due_at = max(self.due_at, time.time() + RETRY_SECONDS)
            self._save_locked()

    def counts(self) -> dict[str, int]:
        with self.lock:
            return {lib: len(keys) for lib, keys in self.items.items()}


def load_plex() -> tuple[str, str]:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    plex = config["plex"]
    return plex["url"].rstrip("/"), plex["token"]


def plex_item(rating_key: str) -> ElementTree.Element | None:
    """Return the Plex metadata element for a rating key, or None if it is gone."""
    url, token = load_plex()
    try:
        response = requests.get(
            urljoin(f"{url}/", f"library/metadata/{rating_key}"),
            headers={"X-Plex-Token": token},
            timeout=30,
        )
    except requests.RequestException as e:
        log.warning("Plex lookup for %s failed: %s", rating_key, e)
        return None
    if response.status_code != 200:
        log.info("Plex returned %s for rating key %s", response.status_code, rating_key)
        return None
    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError as e:
        log.warning("could not parse Plex response for %s: %s", rating_key, e)
        return None
    return next(iter(root), None)


def resolve_target(element: ElementTree.Element) -> str:
    """Map a season or episode onto its show.

    Library operations are defined per top-level item; the sweep walks shows and
    descends into their seasons and episodes itself. Handing it a bare episode
    key would apply show-level operations to an episode.
    """
    item_type = element.get("type")
    if item_type == "episode":
        return element.get("grandparentRatingKey") or element.get("ratingKey") or ""
    if item_type == "season":
        return element.get("parentRatingKey") or element.get("ratingKey") or ""
    return element.get("ratingKey") or ""


def is_matched(element: ElementTree.Element) -> bool:
    guid = element.get("guid") or ""
    return bool(guid) and not UNMATCHED_GUID.match(guid)


def run_kometa(library: str, rating_keys: list[str]) -> bool:
    command = [
        "python3",
        KOMETA_PY,
        "-c",
        CONFIG_PATH,
        "--operations-only",
        "--run-libraries",
        library,
        "--run-items",
        "|".join(rating_keys),
        "--ignore-schedules",
        "--read-only-config",
    ]
    log.info("running operations for %d item(s) in %s: %s", len(rating_keys), library, ",".join(rating_keys))
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        log.error("Kometa run for %s timed out after %ss", library, RUN_TIMEOUT_SECONDS)
        return False
    if result.returncode != 0:
        log.error("Kometa exited %s for %s: %s", result.returncode, library, result.stderr[-2000:])
        return False
    log.info("finished operations for %s", library)
    return True


def worker(pending: Pending) -> None:
    while True:
        time.sleep(5)
        due = pending.take_due()
        if not due:
            continue
        for library, keys in due.items():
            ready: list[str] = []
            for rating_key, attempts in keys.items():
                element = plex_item(rating_key)
                if element is None:
                    # Item vanished between the webhook and now (a failed import, a
                    # replaced file). Nothing to apply.
                    log.info("dropping %s: no longer in Plex", rating_key)
                    continue
                if not is_matched(element):
                    if attempts + 1 >= MAX_ATTEMPTS:
                        log.warning("giving up on %s: still unmatched after %d attempts", rating_key, attempts + 1)
                        continue
                    log.info("%s not matched yet, retrying (attempt %d)", rating_key, attempts + 1)
                    pending.requeue(library, rating_key, attempts + 1)
                    continue
                target = resolve_target(element)
                if target and target not in ready:
                    ready.append(target)
            if ready:
                run_kometa(library, ready)


def parse_multipart_payload(body: bytes, content_type: str) -> dict | None:
    """Pull Plex's `payload` field out of a multipart/form-data body.

    Plex posts its webhook as multipart. The stdlib lost `cgi` in 3.13 and this
    only ever needs one named text field, so parse it directly.
    """
    match = re.search(r'boundary="?([^";]+)"?', content_type)
    if not match:
        return None
    boundary = b"--" + match.group(1).encode()
    for part in body.split(boundary):
        head, _, part_body = part.partition(b"\r\n\r\n")
        if b'name="payload"' not in head:
            continue
        try:
            return json.loads(part_body.rstrip(b"\r\n--").decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            log.warning("could not decode webhook payload: %s", e)
            return None
    return None


def make_handler(pending: Pending):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self, code: int, body: dict) -> None:
            encoded = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path.rstrip("/") == "/health":
                self._reply(200, {"status": "ok", "pending": pending.counts()})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path.rstrip("/") != "/plex":
                self._reply(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            payload = parse_multipart_payload(body, self.headers.get("Content-Type") or "")
            # Always 200: Plex retries nothing, and a non-2xx just fills its log.
            if not payload:
                self._reply(200, {"ok": True, "ignored": "unparsable"})
                return
            event = payload.get("event")
            if event != "library.new":
                self._reply(200, {"ok": True, "ignored": event})
                return
            meta = payload.get("Metadata") or {}
            library = meta.get("librarySectionTitle")
            rating_key = meta.get("ratingKey")
            if not library or not rating_key:
                self._reply(200, {"ok": True, "ignored": "incomplete metadata"})
                return
            pending.add(library, str(rating_key))
            log.info("queued %s (%s) from %s", rating_key, meta.get("title"), library)
            self._reply(200, {"ok": True, "queued": rating_key})

        def log_message(self, fmt: str, *args) -> None:
            log.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


def assert_run_items_supported() -> None:
    """Refuse to start against a Kometa build that predates --run-items.

    Kometa does not reject unrecognised flags; it ignores them and starts a normal
    run. So on a version skew this service would silently kick off a full library
    sweep for every imported file instead of a one-item run.
    """
    try:
        with open(KOMETA_PY, encoding="utf-8") as fh:
            source = fh.read()
    except OSError as e:
        raise SystemExit(f"cannot read {KOMETA_PY}: {e}")
    if '"run-items"' not in source:
        raise SystemExit(
            f"{KOMETA_PY} has no --run-items flag. Kometa ignores unknown flags and would run a "
            f"FULL library sweep per import. Rebuild the image from a fork revision that has it."
        )


def main() -> None:
    assert_run_items_supported()
    pending = Pending(QUEUE_PATH)
    threading.Thread(target=worker, args=(pending,), daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), make_handler(pending))
    log.info("listening on :%d, debounce %ds, kometa at %s", LISTEN_PORT, DEBOUNCE_SECONDS, KOMETA_PY)
    server.serve_forever()


if __name__ == "__main__":
    main()

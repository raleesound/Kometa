"""Tell the Letterboxd bridge which third-party Letterboxd lists Kometa collections watch (epic #5634, S5).

A collection with ``live_watch: true`` is built from Letterboxd list URLs. At the end of a full sweep
Kometa PUTs the set of those URLs (read from the live record) to the bridge's ``/watched-lists``. The
bridge reads each list on its own cadence and publishes ``letterboxd.watchedlist.updated`` when one
really changes; the trigger consuming that event rebuilds the collection (S6).

The PUT replaces the whole set, so a collection that is deleted or drops ``live_watch`` stops being
watched at the next sweep. Configuration comes from the environment, never config.yml:
``LB_BRIDGE_URL`` (e.g. http://letterboxd-bridge/api) and ``LB_BRIDGE_TOKEN`` (the bridge's bearer
token). Nothing here raises, and nothing here talks to letterboxd.com.
"""

import os

import requests

URL_ENV = "LB_BRIDGE_URL"
TOKEN_ENV = "LB_BRIDGE_TOKEN"  # nosec B105 - the name of an environment variable, not a secret
TIMEOUT = (5, 30)
ENDPOINT = "/watched-lists"


def push(urls, log, session=None):
    """PUT ``urls`` as the bridge's watched set. Returns True when the bridge accepted it."""
    base = (os.environ.get(URL_ENV) or "").strip().rstrip("/")
    token = (os.environ.get(TOKEN_ENV) or "").strip()
    if not base or not token:
        if urls:
            log.warning(f"Live Watch: {len(urls)} list(s) to watch but {URL_ENV}/{TOKEN_ENV} are not set, not telling the bridge")
        return False
    body = [{"url": url} for url in urls]
    try:
        response = (session or requests).put(base + ENDPOINT, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
        response.raise_for_status()
    except Exception as e:
        # The message can echo the request URL but never the token (it only travels in a header).
        log.error(f"Live Watch Error: could not update the bridge's watched lists: {e}")
        return False
    log.info(f"Live Watch: bridge now watching {len(urls)} Letterboxd list(s)")
    return True

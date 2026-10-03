"""Per-collection drip for Radarr/Sonarr ``add_missing``.

The ledger is a tag in Radarr/Sonarr: every item a dripped (or exempt) collection adds is tagged with the ledger tag. The allowance is counted fresh from
the arr each run using the ``added`` date of every tagged item, so nothing is stored here:

* First week (no tagged item yet, or the oldest one was added less than 7 days ago): up to ``initial`` items in total.
* After that: up to ``per_week`` items per rolling 7 days.

Every tagged item counts towards the rate, including ones no longer on the list (they were still downloads). Nothing is ever removed or untagged here;
tagged items that are no longer on the list are only reported as orphans.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

LEDGER_PREFIX = "kl-"
WINDOW = timedelta(days=7)
_TAG_LABEL = re.compile(r"^[a-z0-9-]+$")


def utcnow():
    """Run time as a naive UTC datetime, matching the ``added`` dates arrapi returns. Patched in tests."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive_utc(value):
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def default_ledger_tag(collection_name):
    """Stable ledger tag derived from the collection name (Radarr/Sonarr labels allow only a-z, 0-9 and -)."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(collection_name).lower()).strip("-")
    return f"{LEDGER_PREFIX}{slug or 'collection'}"


def valid_ledger_tag(tag):
    return bool(_TAG_LABEL.match(tag))


def with_ledger_tag(tags, ledger_tag):
    """Existing add tags plus the ledger tag, without duplicates."""
    out = list(tags or [])
    if ledger_tag not in out:
        out.append(ledger_tag)
    return out


@dataclass
class DripPlan:
    ledger_tag: str
    first_week: bool
    limit: int  # initial in the first week, per_week after it
    spent: int = 0  # tagged items added within the last 7 days
    remaining: int = 0
    ids_to_pass: list = field(default_factory=list)  # candidates handed to Radarr/Sonarr (everything except held back), in list order
    held_back: list = field(default_factory=list)  # addable candidates over the allowance, in list order
    orphans: list = field(default_factory=list)  # (id, title) tagged in the arr but no longer on the list


def plan_adds(candidates, initial, per_week, ledger_tag, tagged, list_ids, unaddable, now):
    """Decide which missing items may be added this run.

    candidates: missing ids in list order.
    tagged:     {id: (title, added)} for every arr item carrying the ledger tag; added is a datetime or None (None counts as old).
    list_ids:   ids currently on the collection's list (found in Plex or missing); only used to report orphans.
    unaddable:  ids the arr would skip anyway (already in the arr or cached as added); they do not consume allowance and are passed through untouched.
    now:        run time, naive UTC.
    """
    window_start = now - WINDOW
    dates = [_naive_utc(added) for _, added in tagged.values()]
    # An undated item is treated as old, so it ends the first week and does not count towards the rolling window.
    first_week = not dates or all(added is not None and added > window_start for added in dates)
    spent = sum(1 for added in dates if added is not None and added > window_start)
    limit = initial if first_week else per_week
    remaining = max(limit - spent, 0)
    orphans = [(_id, title) for _id, (title, _) in tagged.items() if _id not in list_ids]
    held_back = []
    ids_to_pass = []
    left = remaining
    for _id in candidates:
        if _id in unaddable:
            ids_to_pass.append(_id)
        elif left > 0:
            ids_to_pass.append(_id)
            left -= 1
        else:
            held_back.append(_id)
    return DripPlan(ledger_tag, first_week, limit, spent, remaining, ids_to_pass, held_back, orphans)

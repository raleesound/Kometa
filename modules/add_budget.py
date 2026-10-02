"""Lifetime per-collection budget for Radarr/Sonarr ``add_missing``.

The ledger is a tag in Radarr/Sonarr: every item a budgeted collection adds is tagged with the ledger tag, and the number of tagged items that are still
on the collection's list is what has been spent. Nothing is ever removed or untagged here; items that are tagged but no longer on the list are only
excluded from the count and reported as orphans.
"""

import re
from dataclasses import dataclass, field

LEDGER_PREFIX = "kl-"
_TAG_LABEL = re.compile(r"^[a-z0-9-]+$")


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
class BudgetPlan:
    budget: int
    ledger_tag: str
    spent: int = 0
    remaining: int = 0
    ids_to_pass: list = field(default_factory=list)  # candidates handed to Radarr/Sonarr (everything except held back), in list order
    held_back: list = field(default_factory=list)  # eligible candidates over budget, in list order
    orphans: list = field(default_factory=list)  # (id, title) tagged in the arr but no longer on the list


def plan_adds(candidates, budget, ledger_tag, tagged, list_ids, unaddable):
    """Decide which missing items may be added.

    candidates: missing ids in list order.
    tagged:     {id: title} for every arr item carrying the ledger tag.
    list_ids:   ids currently on the collection's list (found in Plex or missing).
    unaddable:  ids the arr would skip anyway (already in the arr or cached as added); they do not consume budget and are passed through untouched.
    """
    spent = sum(1 for _id in tagged if _id in list_ids)
    orphans = [(_id, title) for _id, title in tagged.items() if _id not in list_ids]
    remaining = max(budget - spent, 0)
    held_back = []
    ids_to_pass = []
    for _id in candidates:
        if _id in unaddable:
            ids_to_pass.append(_id)
        elif remaining > 0:
            ids_to_pass.append(_id)
            remaining -= 1
        else:
            held_back.append(_id)
    return BudgetPlan(budget, ledger_tag, spent, max(budget - spent, 0), ids_to_pass, held_back, orphans)

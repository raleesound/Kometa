---
hide:
  - tags
  - toc
tags:
  - radarr_add_missing
  - radarr_add_missing_budget
  - radarr_add_missing_ledger_tag
  - radarr_add_existing
  - radarr_upgrade_existing
  - radarr_monitor_existing
  - radarr_ignore_cache
  - radarr_folder
  - radarr_monitor
  - radarr_availability
  - radarr_quality
  - radarr_tag
  - radarr_search
  - item_radarr_tag
  - item_radarr_tag.remove
  - item_radarr_tag.sync
  - sonarr_add_missing
  - sonarr_add_missing_budget
  - sonarr_add_missing_ledger_tag
  - sonarr_add_existing
  - sonarr_upgrade_existing
  - sonarr_monitor_existing
  - sonarr_ignore_cache
  - sonarr_folder
  - sonarr_monitor
  - sonarr_quality
  - sonarr_language
  - sonarr_series
  - sonarr_season
  - sonarr_tag
  - sonarr_search
  - sonarr_cutoff_search
  - item_sonarr_tag
  - item_sonarr_tag.remove
  - item_sonarr_tag.sync
---
# Radarr/Sonarr Definition Settings

## Radarr Definition Settings

All the following attributes can override the global/library [Radarr](../config/radarr.md) attributes which are the default unless otherwise specified.

| Attribute                 | Description & Values                                                                                                                                                                                                                                              |
|:--------------------------|:------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `radarr_add_missing`      | **Description:** Override Radarr `add_missing` attribute<br>**Values:** `true` or `false`                                                                                                                                                                         |
| `radarr_add_missing_budget`| **Description:** Lifetime cap on how many items this collection may add to Radarr through `radarr_add_missing`. See [Add Missing Budget](#add-missing-budget)<br>**Values:** Integer 0 or greater                                                                 |
| `radarr_add_missing_ledger_tag`| **Description:** Radarr tag that records this collection's adds for the budget. Only valid with `radarr_add_missing_budget`<br>**Values:** Tag using only letters, numbers and `-`<br>**Default:** `kl-<collection name>`                                         |
| `radarr_add_existing`     | **Description:** Override Radarr `add_existing` attribute<br>**Values:** `true` or `false`                                                                                                                                                                        |
| `radarr_upgrade_existing` | **Description:** Override Radarr `upgrade_existing` attribute<br>**Values:** `true` or `false`                                                                                                                                                                    |
| `radarr_monitor_existing` | **Description:** Override Radarr `monitor_existing` attribute<br>**Values:** `true` or `false`                                                                                                                                                                    |
| `radarr_ignore_cache`     | **Description:** Override Radarr `ignore_cache` attribute<br>**Values:** `true` or `false`                                                                                                                                                                        |
| `radarr_folder`           | **Description:** Override Radarr `root_folder_path` attribute<br>**Values:** Folder Path                                                                                                                                                                          |
| `radarr_monitor`          | **Description:** Override Radarr `monitor` attribute<br>**Values:** `true` or `false`                                                                                                                                                                             |
| `radarr_availability`     | **Description:** Override Radarr `availability` attribute<br>**Values:** `announced`, `cinemas`, `released`, `db`                                                                                                                                                 |
| `radarr_quality`          | **Description:** Override Radarr `quality_profile` attribute<br>**Values:** Radarr Quality Profile                                                                                                                                                                |
| `radarr_tag`              | **Description:** Override Radarr `tag` attribute<br>**Values:** List :material-information-outline:{ data-tooltip data-tooltip-id="tippy-yaml-lists" } or comma-separated string of tags                                                                          |
| `radarr_search`           | **Description:** Override Radarr `search` attribute<br>**Values:** `true` or `false`                                                                                                                                                                              |
| `item_radarr_tag`         | **Description:** Used to append a tag in Radarr for every movie found by the builders that's in Radarr<br>**Values:** List :material-information-outline:{ data-tooltip data-tooltip-id="tippy-yaml-lists" } or comma-separated string of tags                    |
| `item_radarr_tag.remove`  | **Description:** Used to remove existing tags in Radarr for every movie found by the builders that's in Radarr<br>**Values:** List :material-information-outline:{ data-tooltip data-tooltip-id="tippy-yaml-lists" } or comma-separated string of tags            |
| `item_radarr_tag.sync`    | **Description:** Matches the tags in Radarr for every movie found by the builders that's in Radarr with the provided tags<br>**Values:** List :material-information-outline:{ data-tooltip data-tooltip-id="tippy-yaml-lists" } or comma-separated string of tags |

## Sonarr Definition Settings

All the following attributes can override the global/library [Sonarr](../config/sonarr.md) attributes which are the default unless otherwise specified.

| Attribute                 | Description & Values                                                                                                                                                                                                                                               |
|:--------------------------|:-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `sonarr_add_missing`      | **Description:** Override Sonarr `add_missing` attribute<br>**Values:** `true` or `false`                                                                                                                                                                          |
| `sonarr_add_missing_budget`| **Description:** Lifetime cap on how many items this collection may add to Sonarr through `sonarr_add_missing`. See [Add Missing Budget](#add-missing-budget)<br>**Values:** Integer 0 or greater                                                                  |
| `sonarr_add_missing_ledger_tag`| **Description:** Sonarr tag that records this collection's adds for the budget. Only valid with `sonarr_add_missing_budget`<br>**Values:** Tag using only letters, numbers and `-`<br>**Default:** `kl-<collection name>`                                          |
| `sonarr_add_existing`     | **Description:** Override Sonarr `add_existing` attribute<br>**Values:** `true` or `false`                                                                                                                                                                         |
| `sonarr_upgrade_existing` | **Description:** Override Sonarr `upgrade_existing` attribute<br>**Values:** `true` or `false`                                                                                                                                                                     |
| `sonarr_monitor_existing` | **Description:** Override Sonarr `monitor_existing` attribute<br>**Values:** `true` or `false`                                                                                                                                                                     |
| `sonarr_ignore_cache`     | **Description:** Override Sonarr `ignore_cache` attribute<br>**Values:** `true` or `false`                                                                                                                                                                         |
| `sonarr_folder`           | **Description:** Override Sonarr `root_folder_path` attribute<br>**Values:** Folder Path                                                                                                                                                                           |
| `sonarr_monitor`          | **Description:** Override Sonarr `monitor` attribute<br>**Values:** `all`, `future`, `missing`, `existing`, `pilot`, `first`, `latest`, `none`                                                                                                                     |
| `sonarr_quality`          | **Description:** Override Sonarr `quality_profile` attribute<br>**Values:** Sonarr Quality Profile                                                                                                                                                                 |
| `sonarr_language`         | **Description:** Override Sonarr `language_profile` attribute<br>**Values:** Sonarr Language Profile                                                                                                                                                               |
| `sonarr_series`           | **Description:** Override Sonarr `series_type` attribute<br>**Values:** `standard`, `daily`, `anime`                                                                                                                                                               |
| `sonarr_season`           | **Description:** Override Sonarr `season_folder` attribute<br>**Values:** `true` or `false`                                                                                                                                                                        |
| `sonarr_tag`              | **Description:** Override Sonarr `tag` attribute<br>**Values:** List :material-information-outline:{ data-tooltip data-tooltip-id="tippy-yaml-lists" } or comma-separated string of tags                                                                           |
| `sonarr_search`           | **Description:** Override Sonarr `search` attribute<br>**Values:** `true` or `false`                                                                                                                                                                               |
| `sonarr_cutoff_search`    | **Description:** Override Sonarr `cutoff_search` attribute<br>**Values:** `true` or `false`                                                                                                                                                                        |
| `item_sonarr_tag`         | **Description:** Used to append a tag in Sonarr for every series found by the builders that's in Sonarr<br>**Values:** List :material-information-outline:{ data-tooltip data-tooltip-id="tippy-yaml-lists" } or comma-separated string of tags                    |
| `item_sonarr_tag.remove`  | **Description:** Used to remove existing tags in Sonarr for every series found by the builders that's in Sonarr<br>**Values:** List :material-information-outline:{ data-tooltip data-tooltip-id="tippy-yaml-lists" } or comma-separated string of tags            |
| `item_sonarr_tag.sync`    | **Description:** Matches the tags in Sonarr for every series found by the builders that's in Sonarr with the provided tags<br>**Values:** List :material-information-outline:{ data-tooltip data-tooltip-id="tippy-yaml-lists" } or comma-separated string of tags |

## Adding to Arr
You can add items to Radarr/Sonarr in two different ways.
  1. Items found by Kometa that are missing from your collections/playlists.
  2. Items found by Kometa that already exist in Plex but are not in Radarr/Sonarr.

### Arr Add Missing

When `radarr_add_missing`/`sonarr_add_missing` are true the items missing from the collection/playlist will be added to Radarr/Sonarr.

### Arr Add Existing

When `radarr_add_existing`/`sonarr_add_existing` are true the items that exist in the collection/playlist will be added to Radarr/Sonarr. 

If your Radarr/Sonarr has different file system mappings from your Plex use `radarr_path`/`sonarr_path` along with `plex_path` from your 
[Radarr](../config/radarr.md)/[Sonarr](../config/sonarr.md) global config settings.

### Add Missing Budget

`radarr_add_missing_budget`/`sonarr_add_missing_budget` cap how many items a collection may **ever** add to Radarr/Sonarr, so a list that grows (for example a member-submitted one) cannot flood the library. Without it `add_missing` behaves exactly as before.

* The ledger is a tag in Radarr/Sonarr. Everything a budgeted collection adds is tagged with the ledger tag (in addition to any `radarr_tag`/`sonarr_tag`). It defaults to `kl-` plus the collection name in lowercase with runs of other characters turned into `-`, so renaming a collection starts a new ledger unless you pin `radarr_add_missing_ledger_tag`/`sonarr_add_missing_ledger_tag`.
* Each run counts the items carrying the ledger tag that are still on the list, and adds at most `budget - count` of the missing items, in list order. Items already in Radarr/Sonarr, or already in Kometa's add cache, do not use budget.
* Items that carry the ledger tag but are no longer on the list are **orphans**: they do not count against the budget. Kometa never deletes or untags anything in Radarr/Sonarr; orphans are only logged and written to the missing report.
* Items held back by the budget are logged, written to the missing report under `Held Back by Add Missing Budget`, and included in the collection changes webhook as `radarr_held_back`/`sonarr_held_back`.
* To bring items that were added before the budget existed under it, give them the ledger tag in Radarr/Sonarr.

```yaml
collections:
  Member Picks:
    letterboxd_list: https://letterboxd.com/someone/list/picks/
    radarr_add_missing: true
    radarr_add_missing_budget: 25
```

### Radarr Add Settings

When adding a movie in Radarr you get the screen below to set these options use `radarr_folder`, `radarr_monitor`,`radarr_availability`, `radarr_quality`, `radarr_tag`, and `radarr_search`.

![Radarr Details](../assets/images/files/radarr-settings.png)

### Sonarr Add Settings

When adding a show in Sonarr you get the screen below to set these options use `sonarr_folder`, `sonarr_monitor`, `sonarr_quality`, `sonarr_language`, `sonarr_series`, 
`sonarr_season`, `sonarr_tag`, `sonarr_search`, and `sonarr_cutoff_search`.

![Sonarr Details](../assets/images/files/sonarr-settings.png)

## Arr Edit Settings

When editing the details of items that exist in the collection/playlist and in Radarr/Sonarr use `item_radarr_tag` and `item_sonarr_tag`

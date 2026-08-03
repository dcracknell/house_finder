# AI README

Written for AI coding assistants and developers working on this repository. It
explains the shape of the codebase, the invariants that matter, and the
decisions that were made deliberately so they are not "fixed" by accident.

## Project in one sentence

A UK property search pipeline: it fetches listings from Rightmove, normalises
them into one schema, filters and scores them against a buyer/renter profile,
enriches them with free public-record data, stores everything in SQLite, and
regenerates an Excel workbook and an interactive map.

It is adapted from `dcracknell/find_a_job_automator`, which does the same thing
for jobs. Where a module here has a counterpart there, the invariants are the
same.

## Entry points

- `house_finder/cli.py` — all commands. Installed as `house-finder`.
- `house_finder/__init__.py` — `PROJECT_ROOT`, config loaders, `${ENV}` expansion.
- `pyproject.toml` — registers `house-finder = house_finder.cli:main`.

```bash
house-finder --help
house-finder run --dry-run
house-finder health
pytest && ruff check house_finder tests
```

## Runtime data and secrets

- `.env` is gitignored and holds API keys. `.env.example` is the template.
- `data/` is gitignored: `houses.db`, `houses.xlsx`, `dashboard.html`, caches,
  backups, `quota.jsonl`.

## Core data model

`PropertyRecord` in `house_finder/adapters/base.py`. Key invariant:

```text
property_id = sha1(source + "|" + canonical_url)
```

Deliberately keyed on the URL only. Address text and price both change while a
listing is live (agents re-word addresses, prices get reduced); the URL does
not. Changing this needs a migration.

`status` and `notes` are **not** on the dataclass — they exist only in SQLite
and Excel, because they belong to the user and the pipeline must never author
them.

## Pipeline flow (`cli.run`)

1. Load `.env`, `settings.yaml`, `profile.json`, `sources.yaml`.
2. Open and migrate SQLite (`storage/db.py`).
3. Import the user's Excel edits (`output/workbook_import.py`) — **before**
   anything else, so edits made since the last run are not lost.
4. For each enabled mode (`buy`, `rent`):
   - resolve search areas (`search/areas.py`)
   - fetch from each enabled adapter
   - normalise (`pipeline/normalise.py`)
   - filter (`pipeline/filter.py`)
   - enrich (`enrichment/`)
   - rank (`pipeline/rank.py`)
5. Sync to SQLite (`pipeline/dedup.py`), mark stale listings withdrawn.
6. Regenerate the workbook and map, back up the database, send the digest.

## Adapters

`house_finder/adapters/`, all inheriting `Adapter` (`fetch`/`normalise`/`healthcheck`).

- **`rightmove.py`** — the only real listings source. Rightmove's search pages
  are server-rendered Next.js and embed the whole result set as JSON in
  `<script id="__NEXT_DATA__">`, so one plain GET per page returns structured
  data with no browser and no HTML parsing. Cards carry coordinates, tenure,
  floor area and key features, so **no per-listing detail fetch is needed**.
  Criteria are pushed into the query string (`minPrice`, `minBedrooms`,
  `propertyTypes`, `dontShow`, ...) so nothing is downloaded only to be
  discarded locally.
- **`agent_page.py`** — generic schema.org JSON-LD reader for individual estate
  agents listed under `custom_pages:`. Coverage varies; returns nothing rather
  than guessing when a site publishes no structured data.
- **`zoopla.py`, `onthemarket.py`, `auctions.py`** — deliberate
  `NotImplementedError` placeholders. Each docstring records what was checked
  and why it was rejected. **Do not implement these without reading them** —
  Zoopla's public API is retired, OnTheMarket blocks automation, and AuctionIQ
  is a client-side SPA whose only data path is a private backend API.

### Rightmove specifics worth not re-learning

- The typeahead endpoint (`los.rightmove.co.uk/typeahead`) **content-negotiates**
  and returns XML unless you send `Accept: application/json`.
- `locationIdentifier` is `{TYPE}^{ID}`, e.g. `REGION^1195`.
- Radius must be one of `ALLOWED_RADII` in `search/areas.py`; other values are
  snapped down so a configured 4 miles never becomes 40.
- 24 results per page, `index` steps by 24, paging caps out at 1000.
- Rent prices come with `frequency`; weekly is normalised to monthly so one
  column means one thing.

## Filtering

`pipeline/filter.py`. Principles:

- Anything genuinely unknown is **kept**. A missing bedroom count is not
  evidence of a bad match, and keeping one extra listing costs a fraction of a
  penny.
- `property_type == "other"` means "we could not classify it", so it is kept
  even when the user restricted types.
- Unknown lease length on a leasehold is kept — most listings omit it, and
  dropping them would discard most flats sight-unseen.
- **The radius check only applies to sources that cannot filter by location
  themselves** (`SERVER_SIDE_LOCATION_SOURCES` in `adapters/base.py`). Rightmove
  applies the radius server-side, and re-checking it against a single geocoded
  centre point wrongly discarded ~45% of valid results — a portal "region" is
  not a circle.

## Ranking

`pipeline/rank.py`, two passes:

1. **Free keyword pre-score** — word-boundary matches of `must_haves` (×3) and
   `nice_to_haves` (×1). Also the fallback score when no API key is set, in
   which case confidence is forced to 0.3 and the reason says so.
2. **Claude** for everything above `pre_score_threshold`.

Invariants:

- Every LLM call goes through `util/quota.py:api_call_wrapper()`, which logs
  token cost and enforces `quota_soft_cap_gbp` (warn at 1x, stop at 2x).
- Scores are matched to properties by the **echoed `"i"` index**, never by
  position in the response array.
- A listing whose `content_hash` and `ranker_version` both match the stored row
  is never re-sent. This is the single biggest cost saving.
- Default model is **Haiku 4.5**, not Sonnet — deliberate, documented in
  `settings.yaml`.
- The Batches API path is tried first when there are 2+ batches (50% discount)
  and falls back to sync calls on any failure.

## Enrichment

`house_finder/enrichment/`. All best-effort: a source being down never fails a
run, it just leaves those fields empty.

| Source | Cost | Key | Notes |
|---|---|---|---|
| `land_registry` | free | none | Sold comparables. **Full postcode only** — outcode queries return nothing. |
| `crime` | free | none | Fixed ~1 mile radius; not configurable. |
| `flood` | free | none | Warnings **currently in force**, not flood-zone risk. |
| `epc` | free | `EPC_API_EMAIL`+`EPC_API_KEY` | Requires ≥2 address tokens to match, or it would attach a neighbour's certificate. |
| `broadband` | free tier | `OFCOM_API_KEY` | |
| `propertydata` | **paid** | `PROPERTYDATA_API_KEY` | Analytics only, no listings search. Credit-capped. Only fills gaps the free sources left. |

Results are cached in the `enrichment_cache` table keyed by **location**, not by
property — neighbours share the same answer. Cache lifetimes are in `_CACHE_DAYS`;
flood is 1 day because it is live data.

`pipeline/normalise.py:backfill_postcodes()` bulk reverse-geocodes coordinates
to full postcodes via postcodes.io (one request per 90 properties). Without it
Land Registry and EPC only work on the minority of listings that happen to
publish a full postcode — it took Land Registry coverage from ~20% to most
listings.

## Persistence

SQLite is the source of truth; Excel is a regenerated view.

Critical invariants in `pipeline/dedup.py`:

- `sync_record()` must never overwrite `status` or `notes` on an existing row.
- Ranking fields only update when `record.freshly_ranked` is set.
- Enrichment fields are never cleared by a run that skipped enrichment.
- An empty description never overwrites a stored one.
- `first_seen` is preserved.
- Only the pipeline's own housekeeping statuses (`new`, `sold_stc`, `withdrawn`)
  may be changed automatically. A user decision like `offer_made` is final.

## Excel round-trip

`output/workbook_export.py` writes a **hidden `Ref` column** holding
`property_id`, and `workbook_import.py` matches edits on it.

**Do not "simplify" this by matching on the Link column.** openpyxl cannot read
hyperlink targets in `read_only=True` mode, so doing that silently imports
nothing and loses every user edit. There is a regression test
(`test_import_does_not_depend_on_hyperlinks`).

## Dashboard

`output/dashboard.py` + `templates/dashboard.html.j2`. Serialises properties to
one inline JSON blob and renders a Leaflet map with OpenStreetMap tiles. Static
and self-contained. Filtering happens client-side against the embedded data.
Properties without coordinates are listed in a panel rather than dropped.

Starting a search from the map, in `areaSearch()`. Two invariants:

- **The generated file never holds a credential.** It is committed to the
  `house-search-data` branch and opened from public URLs, so anything that needs
  authentication is done by `ui.py` on the user's machine (`gh`, or `GH_TOKEN`
  from `.env`), never by the page. That is the whole reason `/api/dispatch`
  exists rather than the page calling the GitHub API itself.
- **Reachability is probed, not inferred.** `ON_THIS_MACHINE` only says where the
  page came from, which is not the same question as whether `house-finder ui` is
  running. A hosted copy pings `/api/run-status` with `mode: 'no-cors'` - the
  reply is opaque, and "something answered" is all it needs - then hands the
  three answers to `127.0.0.1:8765/map` as a query string with `run=1`, which
  presets the panel and starts the search once. The issue form is the fallback
  when nothing answers. Do not widen `_LOCAL_ORIGIN` in `ui.py` to make the
  hosted page call the API directly: that origin allowlist is what stops any
  website from making the machine search.

## Deliberately not built

Recorded so they are not mistaken for oversights:

- **LLM query generation / rotation.** Job search is keyword-driven with an
  infinite phrasing space; house search is filter-driven against a short, fixed
  list of places. `search/areas.py` just iterates them.
- **Domain packs.** No meaningful house-search equivalent without rental-yield
  and planning data that is not wired up.
- **CV parsing.** Replaced by a deterministic issue-form parser — every field is
  already structured, so there is nothing for an LLM to extract.
- **Source auto-discovery.** Worked for jobs because ATS providers form a small
  slug-guessable ecosystem. UK estate agents do not.

## Tests

`pytest` — 110 tests covering adapter normalisation against a real captured
Rightmove response, filters, dedup invariants, ranking (including a mocked
Claude and Batches path), price parsing, the workbook round-trip, dashboard
generation, and the issue-form parser. CI runs ruff + pytest.

Tests must not depend on the contents of `config/profile.json` — pass
`base_profile={}` to `build_profile_from_issue`.

## Safe change checklist

Before editing: read the nearby module; check `git status`.

When editing:
- Keep `PropertyRecord` compatible or write a migration.
- Never bypass `api_call_wrapper()` for model calls.
- Never let a refresh overwrite `status`, `notes`, or a good score.
- Prefer config in `config/*.yaml` over hard-coded values.

Before finishing: `pytest && ruff check house_finder tests`.

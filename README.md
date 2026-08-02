# House Finder

Finds UK properties matching criteria you set, scores each one against what you
actually want, adds public-record data the listing never tells you, and puts the
results on an interactive map plus a spreadsheet you can track viewings in.

Adapted from [find_a_job_automator](https://github.com/dcracknell/find_a_job_automator)
— same architecture, different problem.

**Live site:** https://dcracknell.github.io/house_finder/

**Website:** once you enable GitHub Pages (see [below](#the-website)), this repo
publishes a landing page at `https://<your-username>.github.io/<repo-name>/`
with setup instructions and direct links to your latest map and spreadsheet.

---

## What it does

1. **Searches** Rightmove for both properties to buy and to rent, using your
   criteria as real search filters (price, bedrooms, type, area, radius,
   floor area, tenure, and whether a garden or parking is required).
2. **Filters out** anything that breaks your rules — over budget, too few
   bedrooms, retirement homes, shared ownership, short leases, auction lots,
   the wrong outcode, a blocked agent, or listings containing words you have
   banned.
3. **Scores what is left, 0-10**, against your must-haves, nice-to-haves and a
   plain-English description of what you are after, with a one-line reason.
4. **Adds data the listing does not show you**, from free public records:
   - what similar properties nearby actually **sold for** (HM Land Registry),
     so you can see if the asking price is optimistic
   - **reported crime** in the surrounding area (police.uk)
   - **flood warnings** currently in force nearby (Environment Agency)
   - full **EPC** figures, and **broadband speed**, if you add those free keys
5. **Remembers everything** in SQLite, so a property you rejected stays gone and
   your notes are never lost.
6. **Gives you two views**: `houses.xlsx` to track viewings and offers, and
   `dashboard.html`, an interactive map you can filter and click through.

Steps 2 and 4 are two halves of the same job. The first filter runs on what the
listing itself says; the second runs after the public-record lookups, so limits
on EPC, crime, broadband, flood warnings or price against local sales are
applied before any scoring is paid for.

---

## Quick start

```bash
cd house_finder
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

pip install -e .

# Set what you are looking for (opens a browser form)
house-finder ui

# Try it without saving anything
house-finder run --dry-run

# Do it for real
house-finder run
```

Results land in `data/houses.xlsx` and `data/dashboard.html`.

### Optional: better scoring

Without an API key the pipeline still runs, but scores are keyword-only and
marked low-confidence. To get real scoring, copy `.env.example` to `.env` and add:

```
ANTHROPIC_API_KEY=sk-ant-...
```

A run costs a few pence. See [Cost](#cost) below.

---

## Setting your criteria

Three ways, easiest first:

1. **`house-finder ui`** — a browser form covering everything. Recommended.
2. **The GitHub issue form** — if you run this on GitHub, open a "House search
   setup" issue and fill it in. No local computer needed.
3. **Edit `config/profile.json`** directly.

Once a search has run, the map itself has a **Change what I am looking for**
link in its header. It opens the same editor, so you can adjust your criteria
from the page you are already looking at rather than remembering a command.

The things worth knowing:

| Setting | What it does |
|---|---|
| `search_areas` | Postcode or town, plus a radius. Add as many as you like. |
| `price` / `price_pcm` | Budget. Applied as a real search filter, not after the fact. |
| `must_haves` | Weigh heavily in the score. Plain words: `garden`, `garage`. |
| `nice_to_haves` | A bonus, not a requirement. |
| `exclusions` | Hard rules. Anything matching is discarded before scoring. |
| `bedrooms_max` / `bathrooms_max` | Upper limits, if a place can be too big. |
| `min_size_sqft` / `max_size_sqft` | Floor area. Only applied to listings that state one. |
| `tenure_types` | `freehold`, `leasehold`, `share_of_freehold`. Blank allows any. |
| `must_have_features` | `garden`, `parking`, `chain_free`. These **rule a property out**. |
| `keyword_includes` | A listing must mention at least one of these to survive. |
| `outcode_includes` | Only these outcodes, e.g. `S10`. Blank allows the whole search area. |
| `min_image_count` | Skip listings with fewer photos than this. Off unless you set it. |
| `max_price_per_sqft` | Only applied to listings that state a floor area. |
| `min_epc_rating` | `A` to `G`. Judged on the EPC score, or the letter if that is all there is. |
| `max_crime_incidents` | Cap on reported incidents nearby. |
| `min_broadband_mbps` | Minimum available speed. |
| `max_price_vs_local_pct` | How far above nearby sold prices you will go, as a percentage. |
| `preferences_freetext` | **The important one.** Describe in your own words what you want — this is what the AI reads. |

`must_haves` and `must_have_features` are not the same thing. `must_haves` is
a wish-list the scorer weighs heavily; `must_have_features` is a hard filter
that discards anything without it. Garden and parking are also sent to
Rightmove as search filters, so those two are applied before anything is even
downloaded.

A few of those are checked **after** the search rather than during it, because
the data behind them does not exist until the free public-record lookups have
run: `min_epc_rating`, `max_crime_incidents`, `min_broadband_mbps`,
`max_price_vs_local_pct` and `no_flood_risk`. They need the matching source
switched on in `config/sources.yaml`, and **a property with no data for a given
check is always kept** — enrichment is capped per run, so most listings reach
that point with nothing attached and a blank is never treated as a bad answer.

Three of the new settings live inside `exclusions` alongside `keyword_excludes`,
because they rule things out rather than describe what you want:
`outcode_excludes`, `agent_excludes` (ignore an agent by name) and
`no_flood_risk`.

`preferences_freetext` is where the real value is. A checkbox cannot express
"not on a main road, room to extend, happy to redo a kitchen but not a roof".

---

## The map

`data/dashboard.html` is a single self-contained file. Open it in any browser,
or sync it to your phone.

- Every property is a pin, coloured by score and shaped by sale vs rent.
- Click a pin for price, rooms, why it scored what it did, and a link.
- Filter by sale/rent, price, bedrooms, score and status without reloading.
- Anything that could not be placed on the map is listed underneath rather than
  quietly dropped.
- "Change what I am looking for" in the header opens the preferences editor, so
  you can adjust your criteria from the page you are already looking at. On the
  machine running `house-finder ui` that is the local editor; anywhere else -
  GitHub Pages, htmlpreview, your phone - it opens the setup issue form instead.
- "Search another area" is a one-off look somewhere that is not in your profile.
  Type a postcode, pick a radius, buy or rent, and whether to score it with the
  AI, and it searches that place against everything you have already set -
  budget, bedrooms, must-haves, exclusions and all. Your saved criteria are
  untouched, so the next scheduled run goes back to your usual areas. On the
  machine running `house-finder ui` it starts there and then. Anywhere else it
  fills in the "One-off area search" issue form for you; press Create and GitHub
  runs it, and the map picks up whatever it found.

---

## The spreadsheet

`data/houses.xlsx` has a **For Sale** sheet, a **To Rent** sheet, and a short
how-to-use sheet.

Two columns are yours: **Status** and **Notes**. Set Status from the dropdown
(`interested`, `viewing_booked`, `viewed`, `offer_made`, ...) and write whatever
you like in Notes. **The pipeline never overwrites either.** Everything else in
the workbook is rebuilt each run.

Marking something `rejected` also keeps it from coming back for 90 days.

---

## Commands

```
house-finder run                  # the full pipeline
house-finder run --dry-run        # fetch and score, save nothing
house-finder run --mode rent      # only one mode
house-finder run --no-rank        # no AI scoring, no spend
house-finder run --area "S10" --radius 5   # a one-off look somewhere else
house-finder run --area "S10" --no-rank    # the same, without spending anything
house-finder ui                   # edit your criteria in a browser
house-finder map-view --open      # rebuild and open the map
house-finder export               # rebuild spreadsheet and map from the database
house-finder list --status interested
house-finder search "bungalow garden"
house-finder health               # check every source still works
house-finder costs                # what it has spent
house-finder stats                # what is being tracked
```

---

## Cost

Designed to be cheap, on purpose:

- **Scoring uses Claude Haiku 4.5** by default, not a larger model. Judging a
  house against explicit criteria is a much easier task than it looks, and Haiku
  is roughly 2.5x cheaper. Switch to Sonnet in `config/settings.yaml` if scores
  feel wrong.
- **Nothing is scored twice.** A listing whose description has not changed since
  it was last scored is never re-sent.
- **Batched at a 50% discount** via the Message Batches API.
- **A daily cap** (`quota_soft_cap_gbp`, default £1.50) warns and then hard-stops.
- **Every enrichment source that is on by default is free** — Land Registry,
  police.uk and the Environment Agency need no key and cost nothing.
- **PropertyData is off unless you add a key.** It is paid, it sells analytics
  rather than listings, and everything it offers overlaps the free sources.

Check spend with `house-finder costs`.

---

## Running it automatically on GitHub

Push this to a GitHub repo and it runs itself — no computer left switched on.
Keep the repo **private**; it records which properties you are looking at.

### 1. Create the repo and push

```bash
cd house_finder
git init                                   # already done if you cloned
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/house_finder.git
git push -u origin main
```

Create the empty repo at [github.com/new](https://github.com/new) first — do not
let GitHub add a README, or the first push will be rejected.

### 2. Add your API key

**Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Needed? | Where from |
|---|---|---|
| `ANTHROPIC_API_KEY` | For real scoring | [console.anthropic.com](https://console.anthropic.com/) |
| `EPC_API_EMAIL`, `EPC_API_KEY` | Optional | [epc.opendatacommunities.org](https://epc.opendatacommunities.org/) — free |
| `OFCOM_API_KEY` | Optional | [api.ofcom.org.uk](https://api.ofcom.org.uk/) — free |
| `SMTP_*` | Only for email digests | Gmail app password works |
| `PROPERTYDATA_API_KEY` | Optional, **paid** | [propertydata.co.uk](https://propertydata.co.uk/api) |

Everything runs without any of these — scores just fall back to keyword-only.

### 3. Set your criteria

**Issues → New issue → House search setup.** Fill in the form and submit it.
That writes `config/profile.json` and kicks off the first search.

### 4. Enable Actions and Pages

- **Actions tab** → enable workflows if prompted.
- **Settings → Pages → Source: GitHub Actions** to publish the website.

### What the workflows do

| Workflow | When | What |
|---|---|---|
| `daily_run.yml` | 07:00 and 16:00 UTC | Searches, scores, saves results to the `house-search-data` branch, uploads artifacts |
| `configure_search.yml` | You open a setup issue | Turns the form into your criteria and starts a search |
| `area_search.yml` | You open a one-off area search issue | Searches that one place with your existing criteria, without changing them |
| `pages.yml` | You push to `docs/` | Publishes the website |
| `test.yml` | Every push | Runs the tests and linter |

**Run workflow** on `daily_run.yml` also takes a one-off `area`, a `radius` and a
`scoring` choice, so you can look at somewhere new, with or without AI scoring,
without touching your saved criteria or waiting for the schedule.

---

## The website

`docs/index.html` is published to GitHub Pages at
`https://<your-username>.github.io/<repo-name>/`. It explains the setup and
links straight to your latest results.

To turn it on: **Settings → Pages → Source: GitHub Actions**. The `pages.yml`
workflow does the rest.

> **GitHub Pages does not work on a private repo on the free plan.** You have
> to pick one:
>
> - **Keep the repo private** (recommended) — no website, but everything else
>   works. Get your results from the Actions artifacts, from the
>   `house-search-data` branch, or just run it locally.
> - **Make the repo public** — the website works, but so does public access to
>   the branch holding your map and spreadsheet. Anyone could see which
>   properties you are tracking and what you wrote in your notes.
> - **Upgrade to GitHub Pro** — private repo and Pages together.
>
> The website is a convenience. The pipeline does not need it.

The links on the page work out your username and repo name from the URL at
runtime, so a fork needs no editing. They point at:

- the interactive map, rendered from the `house-search-data` branch
- `houses.xlsx`, downloaded from the same branch
- your Actions tab, to check the last run succeeded

Those links 404 until the pipeline has run and saved results at least once.

**A public Pages site makes your results public too.** The page itself gives
nothing away, but the map and spreadsheet it links to are only private if the
repository is. Keep the repo private unless you are happy for anyone to see
which properties you are tracking.

---

## Legal and reliability note

Rightmove publishes no public API, so this reads its search results pages
directly. Two things follow from that, and you should know both:

1. **It is against Rightmove's terms of service.** At the volume here — twice a
   day, a handful of areas, a polite delay between requests — the realistic
   worst case is that your IP gets blocked, not legal action. But it is your
   call, and `portals.rightmove.enabled: false` in `config/sources.yaml` turns
   it off.
2. **It will break eventually.** Rightmove will change their page structure at
   some point and the adapter will stop returning results. `house-finder health`
   exists to tell you clearly when that has happened rather than silently
   returning nothing. Fixing it means updating `house_finder/adapters/rightmove.py`.

There is no automatic fallback if Rightmove breaks — Zoopla retired its public
API and OnTheMarket blocks automated access, both documented in
`house_finder/adapters/`. The free government enrichment APIs carry none of
this risk.

**None of this is property advice.** Scores are a machine's opinion against
criteria you wrote. Sold-price comparisons are rough. A zero flood-warning count
does **not** mean a property is not in a flood zone. View in person, get a
survey, and use a solicitor.

---

## How it works

```
config/profile.json          your criteria
  ↓
adapters/rightmove.py        search each area, both channels
  ↓
pipeline/normalise.py        clean up, recover postcodes from coordinates
  ↓
pipeline/filter.py           discard anything breaking your hard rules
  ↓
enrichment/                  sold prices, crime, flood, EPC, broadband
  ↓
pipeline/filter.py           second pass, now that public-record data exists
  ↓
pipeline/rank.py             free keyword pass, then Claude for the judgement
  ↓
pipeline/dedup.py            save to SQLite, never touching your edits
  ↓
output/                      houses.xlsx + dashboard.html + email
```

Full architecture notes for developers and AI assistants: [AI_README.md](AI_README.md).

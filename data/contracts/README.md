# ALPR Contract Status — curated data

Hand-curated data powering `docs/contracts.html`: a nationwide map of agencies
that have canceled, paused, are reviewing, or are considering ending their
ALPR contracts. Vendor-agnostic — tracks Flock, Motorola Vigilant, Axon Fusus,
Rekor, etc.

**Agency identity lives in `assets/agency_registry.json`** (central registry
for all tools in this repo). Contract-map events reference agencies by their
registry `agency_id` (UUID). An agency that isn't yet in the registry must be
added there first — see the workflow below.

## Files in this directory

- `events.json` — flat list of timeline events (signed / canceled / etc.).
- `articles.json` — article metadata. Referenced by events via `article_ids`.

## Schema

### events.json

```json
{
  "agency_id": "8e97dede-dc5e-518a-bd9c-ca1acce1757a",
  "type": "canceled",
  "date": "2026-01-13",
  "vendor": "flock",
  "cameras_affected": null,
  "article_ids": ["santacruzlocal-2026-01-13"],
  "reasons": ["federal-access", "unauthorized-access"],
  "notes": "6-1 council vote. First CA city to terminate."
}
```

- `agency_id`: **registry UUID** from `assets/agency_registry.json`. Build
  fails loudly if the id isn't present in the registry.
- `type`: one of `signed`, `considering`, `reviewing`, `paused`, `canceled`,
  `reinstated`.
- `date`: `YYYY-MM-DD` preferred. `YYYY-MM` and `YYYY` also accepted. Null
  for events with no pinnable date.
- `vendor`: which ALPR vendor this event is about. An agency using both
  Flock and Vigilant will have vendor-tagged events for each.
- `cameras_affected`: optional integer — how many cameras the event touches
  (e.g. "canceled their 500 cameras"). Editorial; pulled from the article.
- `article_ids`: zero or more article ids from `articles.json`. Build fails
  if any id is missing.
- `reasons`: zero or more reason codes. Rendered as colored chips in the UI.
  Build fails on unknown codes. Known codes:
  - `federal-access` — ICE/CBP/DHS access or unreviewed federal sharing
  - `unauthorized-access` — out-of-state or third-party agencies ran
    searches without approval (e.g. Mountain View's 250+ unauthorized
    agencies)
  - `vendor-misconduct` — Flock's own conduct (e.g. Cambridge's unauthorized
    installs, Oshkosh's heatmap misrepresentation)
  - `broad-disclosure` — contract language allows disclosure to any gov
    entity or third party (e.g. Hillsborough)
  - `sanctuary-conflict` — conflicts with the jurisdiction's sanctuary
    ordinance (e.g. Oak Park)
  - `data-breach` — actual breach or unauthorized exposure
  - `privacy-general` — catchall for general privacy concerns

### articles.json

```json
{
  "id": "sfchron-2025-06-15-sfpd-drops-flock",
  "url": "https://www.sfchronicle.com/...",
  "title": "San Francisco police will stop using Flock cameras",
  "outlet": "San Francisco Chronicle",
  "author": "Jane Doe",
  "published_date": "2025-06-15",
  "summary": null,
  "tags": []
}
```

- `id`: stable slug. Convention: `<outlet-slug>-<YYYY-MM-DD>-<shortkey>`.
- `url`, `title`, `outlet`, `published_date` required.
- `author`, `summary`, `tags` optional.

## Adding a new case

1. **Agency missing from registry?** Add it. Two paths:
   - Edit `scripts/seed_contract_registry.py` and add a row to `SEEDS`,
     then run `uv run python scripts/seed_contract_registry.py`. This
     auto-geocodes via the Census gazetteer. Idempotent — re-running with
     the same slug is a no-op.
   - Or edit `assets/agency_registry.json` directly (follow the shape of
     existing entries). Use `uv run python scripts/geocode_agencies.py
     --apply` to backfill `geo`.
2. **Add the article** to `articles.json` with a stable `id`.
3. **Add one or more events** to `events.json` — one per reported state
   change — each referencing the agency's registry `agency_id` and the
   article `id`.
4. Run `uv run python scripts/build_contract_map.py`. It validates
   references and regenerates `docs/data/contract_map_data.json`.

## Derived status

The build script computes the "current" status per agency/vendor from the
most recent event. The map marker reflects the most serious status across
all vendors for that agency (ordering: `canceled > paused > reviewing >
considering > reinstated > signed`).

## Flock transparency enrichment

When a registry entry has `flock_active_slug` set and there's crawled data
under `assets/transparency.flocksafety.com/<slug>/`, the build script
attaches a `flock_snapshot` to the agency payload: live camera count,
30-day activity stats, and a link to the Flock portal. If two snapshots
are at least 14 days apart, the older one is also included as
`flock_snapshot_prior` so the UI can show "cameras dropped from X to Y".

Cancellations on agencies whose Flock portals are still up are especially
interesting — the UI shows the live portal alongside the cancellation
event.

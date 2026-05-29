# SMPD ALPR Investigation

Tools and source documents for investigating the San Mateo Police Department's Automated License Plate Reader program.

**[View the interactive sharing map](https://none-below.github.io/sm-alpr/sharing_map.html)** | **[Read the findings (PDF)](https://none-below.github.io/sm-alpr/SMPD_ALPR_Findings.pdf)** | **[Scoreboard](https://none-below.github.io/sm-alpr/scoreboard.html)**

[![Sharing Map](https://raw.githubusercontent.com/none-below/sm-alpr/readme-assets/sharing_map.png)](https://none-below.github.io/sm-alpr/sharing_map.html)

[![Scoreboard](https://raw.githubusercontent.com/none-below/sm-alpr/readme-assets/scoreboard.png)](https://none-below.github.io/sm-alpr/scoreboard.html)

## Publishing

GitHub Pages deploys automatically on merge to `main` via GitHub Actions.

To set up: repo Settings → Pages → Source → **GitHub Actions**

The CI workflow runs on PRs to validate builds. The deploy workflow runs on merge to main.

To rebuild locally:
```sh
sh scripts/publish_docs.sh
```

## Scripts

### Flock Transparency Portal Archiver

Archives Flock Safety transparency portal pages as PDF, raw DOM text, and structured JSON.

```sh
# Default: just San Mateo
uv run python scripts/flock_transparency.py crawl

# San Mateo County agencies referenced in the findings
uv run python scripts/flock_transparency.py crawl --related

# All agencies SMPD shares with (slow — respects rate limits)
uv run python scripts/flock_transparency.py crawl --all --delay 60

# Batch mode for drip-feeding
uv run python scripts/flock_transparency.py crawl --all --batch 5 --delay 300

# Recursive: follow sharing links N levels deep
uv run python scripts/flock_transparency.py crawl --depth 3 --delay 300

# Via Tor proxy
uv run python scripts/flock_transparency.py crawl --all --proxy socks5://localhost:9050

# Re-parse all stored .txt files into .json (no network)
uv run python scripts/flock_transparency.py parse
uv run python scripts/flock_transparency.py parse --force  # overwrite existing
uv run python scripts/flock_transparency.py parse --slug ncric

# Analyze sharing graph from stored .json files (no network)
uv run python scripts/flock_transparency.py aggregate
uv run python scripts/flock_transparency.py aggregate --json --out outputs/sharing.json
```

### PrimeGov Meeting Packet Fetcher

Downloads meeting packets from PrimeGov-powered city portals, extracts text with page markers. Works with any PrimeGov city — defaults to San Mateo.

```sh
# Fetch all San Mateo packets 2020-2026
uv run python scripts/primegov_packets.py fetch
uv run python scripts/primegov_packets.py fetch --council-only
uv run python scripts/primegov_packets.py fetch --year 2023 --year 2024

# Fetch from other cities
uv run python scripts/primegov_packets.py --city fostercity fetch
uv run python scripts/primegov_packets.py --city sanbruno fetch --year 2025
uv run python scripts/primegov_packets.py --city cityofsancarlos fetch
uv run python scripts/primegov_packets.py --city atherton fetch

# Fetch a specific meeting
uv run python scripts/primegov_packets.py fetch --meeting-id 1391

# OCR/text-extract all downloaded packets
uv run python scripts/primegov_packets.py ocr
uv run python scripts/primegov_packets.py ocr --meeting-id 1391

# Build searchable metadata index
uv run python scripts/primegov_packets.py index
```

### Findings PDF Generator

Builds the investigation findings PDF from the markdown source. Also runs automatically via pre-commit hook when the markdown or generator script is staged.

```sh
uv run python scripts/md_to_pdf.py
uv run python scripts/md_to_pdf.py outputs/SMPD_ALPR_Findings.md outputs/SMPD_ALPR_Findings.pdf
```

This runs automatically via pre-commit hook when `outputs/SMPD_ALPR_Findings.md` or `scripts/md_to_pdf.py` is staged.

### PII Scanner

Scans PDF assets for personal information (emails, phone numbers) with allowlists for known public contacts.

```sh
uv run python scripts/pii_scan.py                     # scan all assets
uv run python scripts/pii_scan.py --staged             # pre-commit mode
uv run python scripts/pii_scan.py --files a.pdf b.pdf
```

### OCR Sidecar Generator

Generates text sidecars for image-based PDFs. Skips PDFs with native text.

```sh
uv run python scripts/ocr_sidecar.py                  # all assets
uv run python scripts/ocr_sidecar.py --staged          # pre-commit mode
uv run python scripts/ocr_sidecar.py --force           # regenerate all
```

## Pre-commit Hooks

Located in `.githooks/`. Configure with:

```sh
git config core.hooksPath .githooks
```

The pre-commit hook:
1. Rebuilds the findings PDF if the markdown or generator script changed
2. Generates OCR sidecars for staged PDFs in `assets/`
3. Scans staged PDFs for PII

## Setup

```sh
uv sync
uv run playwright install chromium
```

Tesseract is required for OCR: `brew install tesseract`

## Secrets (for the article-registry pipeline)

The hourly `Crawl & Curate Articles` workflow runs the article pipeline
in CI and reads two optional credentials from repo secrets. Both have
graceful no-op fallbacks — the workflow won't fail if either is unset,
it just runs in a degraded mode for that integration.

### `ANTHROPIC_API_KEY` — Phase 2 semantic enrichment

Without it the workflow runs Phase 1 (mechanical curation, free) only;
articles land in the registry as `curation_status: "mechanical"` with
no summary/quotes/genre fields.

Recommended setup:

1. Sign in at <https://console.anthropic.com>
2. Create a dedicated workspace (sidebar workspace dropdown → "Create
   workspace"), e.g. `sm-alpr`. This isolates spend from any other
   work in your org and bounds blast-radius if the key leaks.
3. Set a monthly spend cap on the new workspace under **Manage →
   Limits** (or org Billing for the default workspace). Recommended:
   $50/month — comfortably above the ~$0.05/article enrichment cost,
   well below typical credit balances.
4. Generate the key under **Manage → API keys** (in the new
   workspace, not Default). Key is shown once; copy it.
5. Plant in repo secrets:

   ```sh
   gh secret set ANTHROPIC_API_KEY -R none-below/sm-alpr
   ```

Defaults to Claude Opus 4.7. Override with `SM_ALPR_CURATE_MODEL` env
var (set in the workflow or locally) for `claude-sonnet-4-6` or
`claude-haiku-4-5` if you want cheaper enrichment.

### `IA_ACCESS_KEY` + `IA_SECRET_KEY` — Wayback async SPN

Without them the crawler falls back to the slower anonymous Wayback
flow (Availability API check + sync save with up to 90s blocking
wait). With them it uses the authenticated async Save Page Now API:
submit returns a job_id in ~1s, status polled at the tail of the run,
and `if_not_archived_within=86400` does server-side dedup so we don't
re-trigger saves of URLs archived in the last 24 hours.

Setup:

1. Get keys at <https://archive.org/account/s3.php> (logged in to your
   Internet Archive account). Returns an access key + secret pair.
   Secret is shown once; copy both.
2. Plant both in repo secrets:

   ```sh
   gh secret set IA_ACCESS_KEY -R none-below/sm-alpr
   gh secret set IA_SECRET_KEY -R none-below/sm-alpr
   ```

Saves submitted with auth show up under your IA account — useful for
attribution/integrity ("zero-below saved this on date X" beats
"anonymous"). The keys carry no payment authority, only rate limits
and identity; if leaked the worst case is rate-limited Wayback abuse,
not money.

## Adding sources to the discoverer

`scripts/discover_articles.py` polls RSS feeds declared in
`assets/sources.json` (any source with a `feed_url` field) and
auto-appends ALPR/Flock-related items to the queue. Runs daily via
`.github/workflows/discover-articles.yml`. To add a new feed:

1. Confirm the publisher's domain is in `sources.json` with a tier
   and stance. Add it if not.
2. Add `"feed_url": "https://..."` to that entry.
3. Optionally test locally:
   ```sh
   uv run python scripts/discover_articles.py --source <domain> --dry-run
   ```
4. Commit. Next daily tick will start polling it.

The keyword filter (in `discover_articles.py` → `KEYWORDS`) is
hardcoded. Adjust it there if you want to broaden/narrow the topic
scope. The discoverer is state-free: re-running over the same feed
produces no new queue entries because `article_queue_add.py` dedupes
URLs.

## License

Copyright © 2026 zero-below.

This repository is licensed in two layers, plus a carve-out for third-party
material.

**Code → AGPL-3.0.** All original source code — scripts, web assets, build
tooling — is licensed under the **GNU Affero General Public License v3.0**
(see [LICENSE](LICENSE)). Anyone may use, study, modify, and redistribute it.
The copyleft terms require derivative works — including modified versions
offered to the public over a network — to be released under the same license,
with source available. You can build on it; you can't take it private.

**Findings and written content → CC BY 4.0.** The investigation's original
prose, analysis, and findings (e.g. `docs/SMPD_ALPR_Findings.md` and the
generated PDF) are licensed under the **Creative Commons Attribution 4.0
International** license (see [LICENSE-CONTENT](LICENSE-CONTENT)). Republish,
quote, adapt, and build on them freely — including commercially — as long as
you give credit. A credit such as *"Findings by zero-below —
github.com/none-below/sm-alpr"* satisfies the attribution requirement. (Note:
reporting the underlying *facts* and quoting for commentary needs no license
at all; this just makes wholesale reuse explicit.)

**Third-party source material → not covered.** Content under `assets/` that
was scraped or collected from external sources — Flock Safety transparency
portals, news/analysis articles, government meeting packets, and FBI Crime
Data Explorer data — remains the property of its respective owners and is
included here for research, journalism, and archival purposes. Neither license
above grants any rights in that source material.

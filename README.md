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

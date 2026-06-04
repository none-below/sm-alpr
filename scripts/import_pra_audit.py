#!/usr/bin/env python3
"""
Import a PRA-recovered Flock audit log into the transparency-portal pipeline.

The PRA folder stays a bitwise copy of what the vendor produced (e.g. SMPD's
PDF exports). This script reads the already-parsed `audit_rows.json` from a
PRA folder and writes a minimal portal-shape JSON into the matching agency's
transparency-scrape folder. `build_audit_log.py` only consumes the
`search_audit_csv` key, so the imported file contains just that key — no need
to fake the full portal-scrape schema.

The imported file is named `pra-<request-id>.json` (not `YYYY-MM-DD.json`)
to keep PRA-derived rows visibly distinct from real portal scrapes when
inspecting the folder.

Row normalization matches what Flock's CSV-export pipeline produces:
  - searchDate → ISO 8601 (UTC, "Z" suffix), so it sorts and merges with
    rows recovered from real portal scrapes
  - networkCount → string (Flock CSVs are text; downstream code expects strings)

Usage:
  uv run python scripts/import_pra_audit.py <pra-folder> --slug <agency-slug>
  uv run python scripts/import_pra_audit.py assets/san-mateo-public-records/W012541-041426 --slug san-mateo-ca-pd
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PORTAL_ROOT = REPO / "assets" / "transparency.flocksafety.com"

# "01/23/2023, 06:15:22 PM UTC"  →  components
PDF_DATETIME_RE = re.compile(
    r"^(\d{2})/(\d{2})/(\d{4}),\s+(\d{2}):(\d{2}):(\d{2})\s+(AM|PM)\s+UTC$"
)


def to_iso_utc(raw: str) -> str:
    """SMPD's PDF datetime → ISO 8601 with Z suffix. Raises on mismatch so a
    schema drift in a future production is loud, not silent."""
    m = PDF_DATETIME_RE.match(raw.strip())
    if not m:
        raise ValueError(f"unrecognized PDF datetime: {raw!r}")
    mm, dd, yyyy, h, mi, ss, ampm = m.groups()
    h = int(h)
    if ampm == "PM" and h != 12:
        h += 12
    elif ampm == "AM" and h == 12:
        h = 0
    return f"{yyyy}-{mm}-{dd}T{h:02d}:{mi}:{ss}Z"


def normalize_row(row: dict) -> dict:
    """Coerce a parser row into the portal-scrape row shape."""
    return {
        "id": row["id"],
        "userId": row.get("userId", "***"),
        "searchDate": to_iso_utc(row["searchDate"]),
        "networkCount": str(row["networkCount"]),
        "reason": row.get("reason", ""),
    }


def request_id_from_folder(folder: Path) -> str:
    """Use the folder's basename if it matches the W-id pattern, else use it raw."""
    name = folder.name
    if re.match(r"^W\d{6}-\d{6}$", name):
        return name
    return name


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("folder", help="PRA folder containing audit_rows.json")
    ap.add_argument("--slug", required=True,
                    help="Agency slug under assets/transparency.flocksafety.com/")
    ap.add_argument("--rows", default="audit_rows.json",
                    help="Parsed rows file inside the PRA folder (default: audit_rows.json)")
    args = ap.parse_args()

    folder = Path(args.folder)
    rows_path = folder / args.rows
    if not rows_path.is_file():
        print(f"missing {rows_path} — run scripts/parse_pra_audit.py first",
              file=sys.stderr)
        return 1

    portal_dir = PORTAL_ROOT / args.slug
    if not portal_dir.is_dir():
        print(f"slug folder does not exist: {portal_dir}\n"
              f"create it manually if this agency has never been scraped",
              file=sys.stderr)
        return 1

    payload = json.loads(rows_path.read_text())
    raw_rows = payload.get("rows", [])
    if not raw_rows:
        print(f"no rows in {rows_path}", file=sys.stderr)
        return 1

    normalized = [normalize_row(r) for r in raw_rows]
    normalized.sort(key=lambda r: (r["searchDate"], r["id"]))

    # Carry the raw-order integrity forward from parse_pra_audit. search_audit_csv
    # below is re-sorted by date (to merge with portal scrapes), so the per-user
    # block structure only survives in this block's per-file date_resets — keep it
    # visible in the file we actually read, not just the PRA folder's audit_rows.
    out = {}
    if payload.get("integrity"):
        out["integrity"] = {**payload["integrity"], "files": payload.get("files", [])}
    out["search_audit_csv"] = normalized

    request_id = request_id_from_folder(folder)
    out_path = portal_dir / f"pra-{request_id}.json"
    out_path.write_text(json.dumps(out, indent=2) + "\n")

    dates = [r["searchDate"][:10] for r in normalized]
    print(f"wrote {out_path}")
    print(f"  {len(normalized)} rows, {min(dates)} .. {max(dates)}")
    print(f"\nrebuild audit log with:")
    print(f"  uv run python scripts/build_audit_log.py --portal {args.slug}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Slug probe crawler — find the Flock transparency portal slug for agencies
where our guess 404'd.

Standalone from the main flock_transparency crawler so it can run on its own
schedule without eating that crawler's rate budget. Hits at most --limit
candidates per run (default 3) against the Flock portal and records what it
tried in a state file so the next run doesn't repeat itself.

When a candidate returns a live portal, the registry entry is updated so the
primary crawler picks it up on its next pass:
  - the found slug is appended to flock_slugs (if not already there)
  - flock_active_slug is repointed at the found slug
  - the old failing slug is removed from .failed_slugs.json so the main
    crawler will try again

Usage:
  uv run python scripts/slug_probe.py              # probe 3 candidates
  uv run python scripts/slug_probe.py --limit 10   # probe more
  uv run python scripts/slug_probe.py --dry-run    # list candidates, no HTTP
  uv run python scripts/slug_probe.py --agency <agency_id>   # force one agency

State:
  assets/transparency.flocksafety.com/.slug_probe_state.json
"""

import functools
print = functools.partial(print, flush=True)

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import (
    BASE_URL, FAILED_FILE, REGISTRY_PATH, USER_AGENT,
    dedupe, load_json, load_registry, save_json,
)

DEFAULT_DATA_DIR = Path("assets/transparency.flocksafety.com")
STATE_FILE = ".slug_probe_state.json"

# Per-role suffix variants. Order matters — most common first so we probe
# the likely winners before exotic forms. State-specific spellings (SMCSO,
# ACSO) are handled per-agency via tags, not here.
ROLE_SUFFIXES = {
    "police":        ["pd", "po", "police-department", "police", "ps"],
    "sheriff":       ["so", "sd", "sheriffs-office", "sheriffs-department", "sheriff"],
    "da":            ["da", "district-attorney"],
    "fire":          ["fd", "fire", "fire-department"],
    "campus_safety": ["pd", "ps", "police", "public-safety", "campus-safety"],
    "parks":         ["parks", "parks-pd"],
    "highway_patrol":["chp", "highway-patrol"],
    "corrections":   ["doc", "corrections"],
    "intelligence":  [],
    "other":         ["pd", "po"],
}

# Place-type prefixes that sometimes appear in slugs. "Town of X" and
# "City of X" are common for smaller municipalities in the Flock portal.
PLACE_PREFIXES = ["", "city-of-", "town-of-", "village-of-"]


# ═══════════════════════════════════════════════════════════
# Candidate generation
# ═══════════════════════════════════════════════════════════

_PUNCT_RE = re.compile(r"[^a-z0-9\s-]")
_WHITESPACE_RE = re.compile(r"\s+")
_MULTI_DASH_RE = re.compile(r"-+")


def normalize_name(name):
    """Lowercase, drop parenthesized groups, strip punctuation, hyphen-join."""
    s = name.strip().lower()
    # Drop parenthesized qualifiers like (CA), (SMCSO), (ACSO)
    s = re.sub(r"\([^)]*\)", " ", s)
    # Possessives: sheriff's -> sheriffs
    s = re.sub(r"['’]s\b", "s", s)
    s = _PUNCT_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub("-", s.strip())
    s = _MULTI_DASH_RE.sub("-", s).strip("-")
    return s


_STRIP_HEAD = ["city-of-", "town-of-", "village-of-", "the-"]
_STRIP_TAIL_ROLES = {
    "pd", "po", "ps", "police", "police-department", "department",
    "so", "sd", "sheriff", "sheriffs-office", "sheriffs-department",
    "da", "fd", "fire", "chp", "doc", "corrections",
}


def extract_hints(name, state=None, agency_role=None):
    """Pull structured hints out of a display name.

    Returns a dict with:
      base:      the bare place / agency name (e.g. "woodside", "mendocino-county")
      state:     two-letter lowercase state code or None
      role:      "police"|"sheriff"|... or None
      prefixes:  list of place prefixes observed ("town-of-", "city-of-") that
                 should be tried in addition to no-prefix
    """
    n = normalize_name(name)
    tokens = n.split("-")

    # Strip leading place prefix
    prefixes = []
    changed = True
    while changed and tokens:
        changed = False
        for p in _STRIP_HEAD:
            p_tokens = p.strip("-").split("-")
            if len(tokens) > len(p_tokens) and tokens[:len(p_tokens)] == p_tokens:
                prefixes.append(p)
                tokens = tokens[len(p_tokens):]
                changed = True
                break

    # Strip trailing role tokens and state codes, alternating — a display
    # like "Shafter PD CA" has role BEFORE state, so we need multiple passes
    # to peel off both. Keep going until nothing strips.
    from build_agency_registry import ALL_STATES
    role_from_name = None
    state_from_name = None
    changed = True
    while changed and tokens:
        changed = False
        # Try to strip trailing role (two-word forms before one-word forms)
        last1 = tokens[-1]
        last2 = "-".join(tokens[-2:]) if len(tokens) >= 2 else None
        if last2 in _STRIP_TAIL_ROLES:
            role_from_name = _map_role_token(last2) or role_from_name
            tokens = tokens[:-2]
            changed = True
            continue
        if last1 in _STRIP_TAIL_ROLES:
            role_from_name = _map_role_token(last1) or role_from_name
            tokens = tokens[:-1]
            changed = True
            continue
        # Try to strip trailing state code
        if tokens and len(tokens[-1]) == 2 and tokens[-1].upper() in ALL_STATES:
            state_from_name = tokens[-1]
            tokens = tokens[:-1]
            changed = True

    base = "-".join(tokens)

    return {
        "base": base,
        "state": (state or state_from_name or "").lower() or None,
        "role": agency_role or role_from_name,
        "prefixes": prefixes or [""],
    }


def _map_role_token(token):
    """Map a trailing slug token to a canonical agency_role."""
    t = token.lower()
    if t in ("pd", "po", "ps", "police", "police-department", "department"):
        return "police"
    if t in ("so", "sd", "sheriff", "sheriffs-office", "sheriffs-department"):
        return "sheriff"
    if t == "da":
        return "da"
    if t in ("fd", "fire", "fire-department"):
        return "fire"
    if t == "chp":
        return "highway_patrol"
    if t in ("doc", "corrections"):
        return "corrections"
    return None


def generate_candidates(entry):
    """Generate candidate slugs for a registry entry, most-likely first.

    Uses display name + state + agency_role as structured hints, combined
    combinatorially with known Flock URL conventions.
    """
    # Prefer display_name, then latest flock_name
    name = entry.get("display_name") or (entry.get("flock_names") or [None])[-1] or entry.get("slug", "")
    state = (entry.get("geo") or {}).get("state") or entry.get("state")
    role = entry.get("agency_role")

    hints = extract_hints(name, state=state, agency_role=role)
    base = hints["base"]
    state_l = hints["state"]
    role_canon = hints["role"]
    observed_prefixes = hints["prefixes"]

    if not base:
        return []

    suffixes = ROLE_SUFFIXES.get(role_canon, ROLE_SUFFIXES["other"])

    bases = [base]
    # Dehyphenated variant for compound names (e.g. foothill-deanza -> foothilldeanza)
    if "-" in base:
        bases.append(base.replace("-", ""))

    # Place prefixes — always include bare ("") plus any observed; for county/SO
    # entries the city-of/town-of prefix doesn't help, so only add observed ones.
    prefixes_to_try = dedupe([""] + observed_prefixes)

    candidates = []

    for pfx in prefixes_to_try:
        for b in bases:
            pfx_base = pfx + b

            # {prefix}{base}-{state}-{suffix}  — default convention
            if state_l:
                for suf in suffixes:
                    candidates.append(f"{pfx_base}-{state_l}-{suf}")
                # No role suffix (e.g. "city-of-lemoore-ca")
                candidates.append(f"{pfx_base}-{state_l}")

            # {prefix}{base}-{suffix}-{state}  — swapped order (e.g. -el-cajon-pd-ca)
            if state_l:
                for suf in suffixes:
                    candidates.append(f"{pfx_base}-{suf}-{state_l}")

            # {prefix}{base}-{suffix}  — no state code at all
            for suf in suffixes:
                candidates.append(f"{pfx_base}-{suf}")

            # {prefix}{base}-{suffix}{state}  — collapsed (e.g. mendocino-county-soca)
            if state_l:
                for suf in suffixes:
                    if len(suf) == 2:  # only collapse short codes
                        candidates.append(f"{pfx_base}-{suf}{state_l}")

            # {prefix}{base}  — bare, no role or state (rare but happens)
            candidates.append(pfx_base)

    # Leading-dash variants (e.g. -el-cajon-pd-ca). Flock stores some agencies
    # this way; probably a portal-import artifact.
    candidates = candidates + ["-" + c for c in candidates if not c.startswith("-")]

    return dedupe(candidates)


# ═══════════════════════════════════════════════════════════
# Probe: HTTP GET, check for portal markers
# ═══════════════════════════════════════════════════════════

# Markers that distinguish a real portal page from a 200-OK SPA shell /
# marketing page. Any one of these in the rendered body is strong evidence.
PORTAL_MARKERS = [
    "Provided by Flock Safety",
    "Transparency Portal",
    "What's Detected",
    "Hotlist Policy",
    "Acceptable Use Policy",
    "Policies",
    "Usage",
]

WAIT_MS = 5000  # matches flock_transparency.py — let SPA hydrate


def probe(page, slug, timeout_ms=30000):
    """Probe a candidate slug via playwright.

    Flock blocks non-browser clients at the edge (403 on curl/urllib), so we
    need a real Chromium. Returns ("hit"|"miss"|"rate_limited"|"error", detail).
    """
    url = f"{BASE_URL}/{slug}"
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as e:
        return "error", f"navigation:{e}"

    if response is None:
        return "error", "no_response"
    if response.status == 429:
        return "rate_limited", "http_429"
    if response.status == 404:
        return "miss", "http_404"
    if response.status >= 400:
        return "miss", f"http_{response.status}"

    page.wait_for_timeout(WAIT_MS)
    body = page.inner_text("body")

    if any(marker in body for marker in PORTAL_MARKERS):
        return "hit", f"http_{response.status}"

    return "miss", f"http_{response.status}_no_marker"


# ═══════════════════════════════════════════════════════════
# State management
# ═══════════════════════════════════════════════════════════


def load_state(data_dir):
    path = data_dir / STATE_FILE
    if path.exists():
        return json.loads(path.read_text())
    return {"version": 1, "updated": None, "agencies": {}}


def save_state(data_dir, state):
    state["updated"] = datetime.now(timezone.utc).isoformat()
    save_json(data_dir / STATE_FILE, state)


def agency_state(state, agency_id):
    return state["agencies"].setdefault(agency_id, {
        "tried": {},
        "found": None,
        "last_probed": None,
        "exhausted": False,
    })


# ═══════════════════════════════════════════════════════════
# Target selection
# ═══════════════════════════════════════════════════════════


def select_targets(registry, failed_slugs, state, only_agency=None):
    """Return registry entries whose active slug is failing and not yet found."""
    targets = []
    for e in registry:
        aid = e["agency_id"]
        if only_agency and aid != only_agency:
            continue
        if state.get("agencies", {}).get(aid, {}).get("found"):
            continue
        if state.get("agencies", {}).get(aid, {}).get("exhausted"):
            continue
        active = e.get("flock_active_slug")
        if not active:
            continue
        if active in failed_slugs:
            targets.append(e)
    return targets


# ═══════════════════════════════════════════════════════════
# Registry promotion
# ═══════════════════════════════════════════════════════════


def promote_slug(registry, agency_id, found_slug):
    """Update the registry in-place: add found_slug and make it active."""
    for e in registry:
        if e["agency_id"] == agency_id:
            slugs = e.setdefault("flock_slugs", [])
            if found_slug not in slugs:
                slugs.append(found_slug)
            e["flock_active_slug"] = found_slug
            return True
    return False


def clear_failed(failed_slugs, slug):
    failed_slugs.pop(slug, None)


# ═══════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="Probe Flock portal for missing agency slugs")
    parser.add_argument("--limit", type=int, default=3,
                        help="Max number of HTTP probes per run (default: 3)")
    parser.add_argument("--delay", type=int, default=90,
                        help="Seconds between probes, jittered (default: 90)")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--dry-run", action="store_true",
                        help="List candidates for targets, make no HTTP calls")
    parser.add_argument("--agency", default=None,
                        help="Probe one specific agency_id only")
    parser.add_argument("--reset-exhausted", action="store_true",
                        help="Clear 'exhausted' flags so probes resume for agencies that ran out of candidates")
    args = parser.parse_args()

    registry = load_registry()
    data_dir = args.data_dir
    failed_slugs = load_json(data_dir / FAILED_FILE)
    state = load_state(data_dir)

    if args.reset_exhausted:
        for aid, st in state.get("agencies", {}).items():
            st["exhausted"] = False
        save_state(data_dir, state)
        print(f"Cleared 'exhausted' flags for {len(state.get('agencies', {}))} agencies")

    targets = select_targets(registry, failed_slugs, state, only_agency=args.agency)
    print(f"Found {len(targets)} target agencies (slug in failed_slugs, not yet resolved)")

    if args.agency and not targets:
        print(f"  (no target for agency_id={args.agency})")
        return

    probes_done = 0
    hits = []

    # Round-robin across agencies so one agency's long candidate list doesn't
    # starve the others. Build per-agency candidate queues up front.
    queues = []
    for e in targets:
        aid = e["agency_id"]
        tried = state["agencies"].get(aid, {}).get("tried", {})
        # Skip candidates already in the failed_slugs.json — we know those 404.
        # The primary crawler populated that file; no point re-probing.
        candidates = [
            c for c in generate_candidates(e)
            if c not in tried and c not in failed_slugs
        ]
        if not candidates:
            agency_state(state, aid)["exhausted"] = True
            continue
        queues.append((e, candidates))

    # Shuffle queue order so we don't always probe the same few agencies first.
    random.shuffle(queues)

    if args.dry_run:
        for entry, candidates in queues[:10]:
            name = entry.get("display_name") or (entry.get("flock_names") or ["?"])[-1]
            print(f"\n{name}  [{entry['agency_id']}]")
            print(f"  current active: {entry.get('flock_active_slug')}")
            print(f"  candidates ({len(candidates)}):")
            for c in candidates[:20]:
                print(f"    {c}")
            if len(candidates) > 20:
                print(f"    ... and {len(candidates) - 20} more")
        return

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--headless=new"])
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=USER_AGENT,
        )
        page = context.new_page()

        # Round-robin: pull one candidate from each queue in turn until limit hit
        q_idx = 0
        while probes_done < args.limit and queues:
            entry, candidates = queues[q_idx % len(queues)]
            if not candidates:
                queues.pop(q_idx % len(queues))
                if not queues:
                    break
                continue
            candidate = candidates.pop(0)
            aid = entry["agency_id"]
            name = entry.get("display_name") or (entry.get("flock_names") or ["?"])[-1]

            print(f"\n[{probes_done + 1}/{args.limit}] {name}  ->  {candidate}")
            result, detail = probe(page, candidate)
            st = agency_state(state, aid)
            st["tried"][candidate] = detail
            st["last_probed"] = datetime.now(timezone.utc).isoformat()
            probes_done += 1

            if result == "hit":
                print(f"    HIT ({detail}) — promoting to registry")
                st["found"] = candidate
                old_slug = entry.get("flock_active_slug")
                promote_slug(registry, aid, candidate)
                if old_slug:
                    clear_failed(failed_slugs, old_slug)
                clear_failed(failed_slugs, candidate)
                hits.append((name, candidate))
                queues.pop(q_idx % len(queues))
            elif result == "miss":
                print(f"    miss ({detail})")
                q_idx += 1
            elif result == "rate_limited":
                print(f"    RATE LIMITED — stopping this run")
                # Roll back the consumed candidate — we don't want to mark it
                # tried without actually knowing the answer.
                st["tried"].pop(candidate, None)
                probes_done -= 1
                break
            else:  # error
                print(f"    error ({detail}) — leaving as tried to avoid retry loops")
                q_idx += 1

            # Save after every probe so a crash mid-run doesn't lose progress
            save_state(data_dir, state)
            save_json(data_dir / FAILED_FILE, failed_slugs)
            if hits:
                REGISTRY_PATH.write_text(json.dumps(registry, indent=2) + "\n")

            if probes_done < args.limit and queues:
                sleep_for = args.delay * random.uniform(0.7, 1.3)
                print(f"    sleeping {sleep_for:.0f}s...")
                time.sleep(sleep_for)

        browser.close()

    # Final save
    save_state(data_dir, state)
    save_json(data_dir / FAILED_FILE, failed_slugs)

    print(f"\nDone: {probes_done} probe(s), {len(hits)} hit(s)")
    if hits:
        print("Hits:")
        for name, slug in hits:
            print(f"  {name} -> {slug}")
        # Expose hits to GitHub Actions via output file, if requested.
        import os
        gha_output = os.environ.get("GITHUB_OUTPUT")
        if gha_output:
            with open(gha_output, "a") as f:
                f.write("has_hits=true\n")
                f.write(f"hit_slugs={' '.join(slug for _, slug in hits)}\n")


if __name__ == "__main__":
    main()

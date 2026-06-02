#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Slug probe crawler — find the Flock transparency portal slug for agencies
where our guess 404'd.

Standalone from the main flock_transparency crawler so it can run on its own
schedule without eating that crawler's rate budget. Hits at most --limit
candidates per run (default 3) against the Flock portal and records what it
tried in a state file so the next run doesn't repeat itself.

When a candidate returns a live portal, the probe captures the page and writes
a hand-off file under .slug_probe_hits/<agency_id>.json. It deliberately does
NOT touch agency_registry.json, .content_hashes.json, or .failed_slugs.json —
those belong to the main crawler. On its next run the crawler ingests the
hand-off (flock_transparency.py ingest-probe-hits), promoting the slug,
clearing the old/new failed entries, and recording the content hash. Keeping
the probe out of the crawler's shared files is what stops the two bots'
parallel branches from colliding at merge time — the same single-writer
hand-off idiom as the article queue.

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
    BASE_URL, FAILED_FILE, USER_AGENT,
    dedupe, load_json, load_registry, save_json,
)

DEFAULT_DATA_DIR = Path("assets/transparency.flocksafety.com")
STATE_FILE = ".slug_probe_state.json"
# Hand-off directory: one .slug_probe_hits/<agency_id>.json per portal we find,
# drained by flock_transparency.ingest_probe_hits() on the crawler's next run.
HITS_DIR = ".slug_probe_hits"

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

# A 403 from Flock's edge is a bot-challenge, not a real answer about the
# slug — the same block the module dodges with Chromium, leaking through
# intermittently. They arrive in bursts once the session is fingerprinted,
# so we back off (exponential, jittered, capped) instead of plowing through
# and burning candidates as permanent misses, and bail the run entirely
# after this many consecutive 403s.
MAX_CONSECUTIVE_FORBIDDEN = 3
FORBIDDEN_COOLDOWN_CAP = 120  # seconds — ceiling on per-strike backoff


def probe(page, slug, timeout_ms=30000):
    """Probe a candidate slug via playwright.

    Flock blocks non-browser clients at the edge (403 on curl/urllib), so we
    need a real Chromium. Even then the edge bot-challenges a fraction of our
    requests with a 403 — that's a "we flagged you," NOT a "this slug is dead,"
    so it's reported distinctly from a genuine 404/4xx miss.
    Returns ("hit"|"miss"|"forbidden"|"rate_limited"|"error", detail).
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
    if response.status == 403:
        return "forbidden", "http_403"
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
    """Return target agencies split into two priority tiers.

    Tier A — registry entries with no `flock_active_slug` set yet but at
    least one `flock_names` entry. These are agencies discovered through
    peer-outbound sharing whose actual slug we haven't found. Single-try
    hit rate from `name_to_slug`-style guesses tends to be high (~50%
    among police), so this tier earns the larger share of the budget.

    Tier B — registry entries whose active slug is in failed_slugs. We
    confirmed once the guess didn't work; now we slog through variants.
    Lower hit rate per probe but still worth working through.

    Caller drives the budget split between tiers; this function just
    classifies them and skips already-resolved/exhausted agencies.
    """
    tier_a = []
    tier_b = []
    for e in registry:
        aid = e["agency_id"]
        if only_agency and aid != only_agency:
            continue
        st = state.get("agencies", {}).get(aid, {})
        if st.get("found"):
            continue
        if st.get("exhausted"):
            continue
        active = e.get("flock_active_slug")
        if active is None:
            # Need a name to derive candidates from — entries with no
            # name and no slug aren't probeable yet.
            if e.get("flock_names") or e.get("display_name"):
                tier_a.append(e)
        elif active in failed_slugs:
            tier_b.append(e)
        # else: active is set and not failed → cmd_crawl handles refresh,
        # probe stays out of the way.
    return tier_a, tier_b


def eyesonflock_hint(entry, eof_index):
    """Return eyesonflock's authoritative slug for `entry` if there's a
    clean geographic + role match, else None.

    Conservative mapping: only place/police → PD and county/sheriff → SD
    are matched, since those are the kinds eyesonflock's payload carries.
    Other registry shapes (cousub township police, ambiguous boundaries,
    state-level agencies) have no eyesonflock counterpart so we don't try
    to match them — the cost of a wrong match (poisoned slug promoted to
    the registry) is much higher than the cost of falling through to
    variant generation.

    `eof_index` is the dict produced by eyesonflock_lookup.index_by_geo().
    Pass {} to disable lookup (e.g. when the API fetch failed).
    """
    if not eof_index:
        return None
    from eyesonflock_lookup import locality_key
    geo = entry.get("geo") or {}
    name = geo.get("name")
    state = geo.get("state")
    if not name or not state:
        return None
    role = entry.get("agency_role")
    kind = geo.get("kind")
    if kind == "place" and role == "police":
        eof_type = "PD"
    elif kind == "county" and role == "sheriff":
        eof_type = "SD"
    else:
        return None
    return eof_index.get((locality_key(name), state.upper(), eof_type))


def load_eyesonflock_index():
    """Fetch + validate eyesonflock's portal API and return the geo index.
    Returns {} on any failure — slug_probe falls through to variant
    generation as if eyesonflock didn't exist. The lookup is a hint, not
    a dependency; an outage shouldn't break the run.
    """
    try:
        from eyesonflock_lookup import fetch_api, index_by_geo, parse_payload
        text = fetch_api()
        records = parse_payload(text)
        index, conflicts = index_by_geo(records)
        if conflicts:
            print(f"  eyesonflock: {len(conflicts)} (city,state,type) conflicts skipped")
        return index
    except Exception as e:
        print(f"  eyesonflock: lookup unavailable ({type(e).__name__}: {e})")
        return {}


def allocate_tier_budget(limit, tier_a_count, tier_b_count, tier_b_share=1/3):
    """Split a per-run probe budget between tier A and tier B.

    Returns (a_budget, b_budget) summing to at most `limit`. Defaults
    favor tier A (`tier_b_share=1/3` → at limit=3, the split is 2 A + 1 B)
    because tier-A single-try hit rate is meaningfully higher than tier B's
    variant-search hit rate. When either tier is empty the other tier
    gets the full budget.
    """
    if tier_a_count == 0 and tier_b_count == 0:
        return 0, 0
    if tier_a_count == 0:
        return 0, limit
    if tier_b_count == 0:
        return limit, 0
    b_budget = max(1, int(limit * tier_b_share))
    a_budget = max(0, limit - b_budget)
    return a_budget, b_budget


# ═══════════════════════════════════════════════════════════
# Hand-off to the main crawler
# ═══════════════════════════════════════════════════════════


def record_hit(data_dir, agency_id, found_slug, old_slug, content_hash, probed_at):
    """Write one hand-off file the main crawler ingests on its next run.

    The probe records each portal it finds as its own file under
    ``.slug_probe_hits/<agency_id>.json`` and never edits the shared registry,
    content-hash, or failed-slug files directly. The crawler is the sole writer
    of those and drains this directory via
    ``flock_transparency.ingest_probe_hits()``. One file per agency
    (create-by-probe, delete-by-crawler) means the two bots only ever touch
    different paths, so their parallel branches never collide at merge time —
    the same idiom as the article queue (assets/articles/queue/<urlhash>.json).
    """
    hits_dir = data_dir / HITS_DIR
    hits_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "agency_id": agency_id,
        "found_slug": found_slug,
        "old_slug": old_slug,
        "content_hash": content_hash,
        "probed_at": probed_at,
    }
    # Atomic write: temp then rename, so the crawler never reads a half-written
    # hand-off file (ingest treats any *.json under the dir as a complete hit).
    target = hits_dir / f"{agency_id}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n")
    tmp.replace(target)


# ═══════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="Probe Flock portal for missing agency slugs")
    parser.add_argument("--limit", type=int, default=3,
                        help="Max number of HTTP probes per run (default: 3)")
    parser.add_argument("--delay", type=int, default=5,
                        help="Seconds between probes, jittered (default: 5)")
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

    tier_a, tier_b = select_targets(registry, failed_slugs, state,
                                    only_agency=args.agency)
    print(f"Tier A (no slug yet, peer-outbound discoveries): {len(tier_a)}")
    print(f"Tier B (slug in failed_slugs, variant search):   {len(tier_b)}")

    if args.agency and not tier_a and not tier_b:
        print(f"  (no target for agency_id={args.agency})")
        return

    a_budget, b_budget = allocate_tier_budget(args.limit, len(tier_a), len(tier_b))
    print(f"Budget split: tier A = {a_budget}, tier B = {b_budget}")

    probes_done = 0
    consecutive_forbidden = 0  # carried across both tiers — the edge flag is session-wide
    hits = []

    # Authoritative-hint lookup: an external crawler (eyesonflock.com)
    # publishes confirmed Flock portal slugs for ~900 agencies. When their
    # geo/type matches one of our entries we try their slug first — much
    # higher hit rate than name_to_slug variant generation. Sanitization
    # gate in eyesonflock_lookup.py keeps poisoned input out.
    if not args.dry_run:
        eof_index = load_eyesonflock_index()
        print(f"Eyesonflock index: {len(eof_index)} (locality,state,type) keys")
    else:
        eof_index = {}

    def build_queues(targets):
        """Build per-agency candidate queues. Skip candidates already
        tried (from state) or known to 404 (in failed_slugs). Prepend
        the eyesonflock hint when we have a clean geo+role match —
        single highest-confidence guess goes first, variant generation
        is the fallback."""
        queues = []
        for e in targets:
            aid = e["agency_id"]
            tried = state["agencies"].get(aid, {}).get("tried", {})
            ordered = []
            hint = eyesonflock_hint(e, eof_index)
            if hint:
                ordered.append(hint)
            ordered.extend(generate_candidates(e))
            candidates = []
            seen = set()
            for c in ordered:
                if c in seen or c in tried or c in failed_slugs:
                    continue
                seen.add(c)
                candidates.append(c)
            if not candidates:
                agency_state(state, aid)["exhausted"] = True
                continue
            queues.append((e, candidates))
        random.shuffle(queues)
        return queues

    queues_a = build_queues(tier_a)
    queues_b = build_queues(tier_b)

    if args.dry_run:
        for label, queues in (("TIER A", queues_a), ("TIER B", queues_b)):
            if not queues:
                continue
            print(f"\n=== {label} ===")
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

    # Lazy import: archive_agency lives in flock_transparency, which pulls
    # in playwright/parsing — only import when we'll actually probe.
    from flock_transparency import archive_agency

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--headless=new"])
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=USER_AGENT,
        )
        page = context.new_page()

        # Wrap the inner loop so we can run it twice (tier A then tier B)
        # against separate queues with separate budgets. Return value is
        # whether we hit a rate_limit and should stop the whole run.
        def run_tier(queues, budget, label):
            nonlocal probes_done, consecutive_forbidden
            q_idx = 0
            consumed = 0
            while consumed < budget and queues:
                entry, candidates = queues[q_idx % len(queues)]
                if not candidates:
                    queues.pop(q_idx % len(queues))
                    if not queues:
                        break
                    continue
                candidate = candidates.pop(0)
                aid = entry["agency_id"]
                name = entry.get("display_name") or (entry.get("flock_names") or ["?"])[-1]

                print(f"\n[{label} {probes_done + 1}/{args.limit}] {name}  ->  {candidate}")
                result, detail = probe(page, candidate)
                st = agency_state(state, aid)
                st["tried"][candidate] = detail
                st["last_probed"] = datetime.now(timezone.utc).isoformat()
                probes_done += 1
                consumed += 1
                if result != "forbidden":
                    consecutive_forbidden = 0

                if result == "hit":
                    print(f"    HIT ({detail}) — recording hand-off for the crawler")
                    st["found"] = candidate
                    old_slug = entry.get("flock_active_slug")
                    hits.append((name, candidate))
                    queues.pop(q_idx % len(queues))

                    # Capture the page now so it's on disk immediately. This
                    # writes only the new slug's own directory (.txt/.html/
                    # .json/.pdf) — a brand-new path that can't collide with
                    # the crawler's files. We pass a throwaway hash map purely
                    # to recover the page's content hash; we deliberately do
                    # NOT write the shared .content_hashes.json here, because
                    # the crawler owns that file. archive_agency re-navigates
                    # (5s dwell) and produces the full artifact set.
                    capture_hashes = {}
                    archive_agency(page, candidate, data_dir,
                                   force=True, hashes=capture_hashes)

                    # Hand the find off to the crawler instead of editing the
                    # registry / failed-slugs / hash files directly. The
                    # crawler's ingest_probe_hits() promotes the slug, clears
                    # the old+new failed entries, and writes the content hash
                    # on its next run — single writer per shared file, so the
                    # probe and crawler branches never conflict.
                    record_hit(
                        data_dir, aid, candidate, old_slug,
                        content_hash=capture_hashes.get(candidate),
                        probed_at=st["last_probed"],
                    )
                elif result == "miss":
                    print(f"    miss ({detail})")
                    q_idx += 1
                elif result == "rate_limited":
                    print(f"    RATE LIMITED — stopping this run")
                    # Roll back the consumed candidate — we don't want to mark it
                    # tried without actually knowing the answer.
                    st["tried"].pop(candidate, None)
                    probes_done -= 1
                    consumed -= 1
                    return True  # signal: stop the whole run
                elif result == "forbidden":
                    # Edge bot-challenge, not a real answer. Roll it back like a
                    # rate-limit (don't burn the candidate — it regenerates next
                    # run) and don't charge it against the probe budget. Then
                    # slow down: 403s come serially once the session is flagged,
                    # so back off before the next try and bail if they persist.
                    st["tried"].pop(candidate, None)
                    probes_done -= 1
                    consumed -= 1
                    consecutive_forbidden += 1
                    if consecutive_forbidden >= MAX_CONSECUTIVE_FORBIDDEN:
                        print(f"    FORBIDDEN x{consecutive_forbidden} — edge is "
                              f"blocking us, stopping this run")
                        return True  # signal: stop the whole run
                    cooldown = min(args.delay * 2 ** consecutive_forbidden,
                                   FORBIDDEN_COOLDOWN_CAP) * random.uniform(0.7, 1.3)
                    print(f"    forbidden (http_403) — backing off {cooldown:.0f}s "
                          f"(strike {consecutive_forbidden}/{MAX_CONSECUTIVE_FORBIDDEN})")
                    save_state(data_dir, state)
                    time.sleep(cooldown)
                    q_idx += 1
                    continue  # skip the normal save + inter-probe sleep tail
                else:  # error
                    print(f"    error ({detail}) — leaving as tried to avoid retry loops")
                    q_idx += 1

                # Save after every probe so a crash mid-run doesn't lose
                # progress. Only the probe-owned state file is written here;
                # hits are already durable (record_hit writes them atomically),
                # and the shared registry / failed-slug / hash files belong to
                # the crawler, which ingests the hand-off on its next run.
                save_state(data_dir, state)

                if probes_done < args.limit and queues:
                    sleep_for = args.delay * random.uniform(0.7, 1.3)
                    print(f"    sleeping {sleep_for:.0f}s...")
                    time.sleep(sleep_for)
            return False  # not rate-limited

        if a_budget:
            stopped = run_tier(queues_a, a_budget, "A")
            if not stopped and b_budget:
                run_tier(queues_b, b_budget, "B")
        elif b_budget:
            run_tier(queues_b, b_budget, "B")

        browser.close()

    # Final save — probe-owned state only (the crawler owns the shared files;
    # see the per-probe save note above).
    save_state(data_dir, state)

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

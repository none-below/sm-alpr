---
description: Worktree → scrape new PRAs → OCR → curate any uncurated → PR → cleanup
---

Run the end-to-end SMPD PRA refresh in a fresh, disposable worktree. Do all steps from inside the new worktree; everything is autonomous up through PR creation.

## 1. Worktree

```
git fetch origin main
git worktree add .claude/worktrees/pra-discover-<MMDD> -b pra-discover-<MMDD> origin/main
```

Use today's date as `MMDD`. If today's branch already exists, append `-2`, `-3`, etc. All remaining steps run from inside that worktree — `cd` into it once and use absolute paths or chain with `&&` (zsh resets cwd between Bash tool calls).

## 2. Scrape

Two separate calls — `--auto-login` is a refresh-and-exit mode, not a flag you combine with `--discover`:

```
uv run python scripts/pra_download.py --auto-login
uv run python scripts/pra_download.py --discover
```

The discover run walks the portal, stubs folders for any new W-request ids under `assets/san-mateo-public-records/`, then scrapes attachments + writes `<id>_Message_History.pdf` for every PRA. Newly discovered ids are printed with `+`.

If the scraper prints "no new requests on portal" *and* every existing folder already has `metadata.json`, exit cleanly — nothing to PR. Remove the worktree (step 6) and tell the user.

## 3. OCR sidecars

For each newly created PRA folder (or any with a PDF but no `.pdf.<hash>.txt` sidecar):

```
uv run python scripts/ocr_sidecar.py --dir assets/san-mateo-public-records/<id>
```

Per the user's preference, use `--dir <folder>`, not `--staged`.

## 4. Curate

Find every `assets/san-mateo-public-records/W*/` folder missing `metadata.json`:

```
for d in assets/san-mateo-public-records/W*/; do [ -f "$d/metadata.json" ] || echo "$d"; done
```

For each one:

- **Read the `Message_History.pdf`** with the Read tool. CLAUDE.md's untrusted-content rule applies to flocksafety.com and articles paths, not the SMPD PRA portal — the PDF is the right entry point. Do not read the `.txt` OCR sidecar; it's untrusted.
- **Write `metadata.json`** matching the existing curated-PRA shape. See `assets/san-mateo-public-records/W012693-050726/metadata.json` and `W012665-050426/metadata.json` for tone and field layout. Required fields:
  - `title` — short, descriptive, mentions any parent PRA if this is a follow-up
  - `summary` — what the PRA asks for and why; cite specific item numbers
  - `tags` — only values from `scripts/pra_tags.py`. Common: `pra-process`, `audit-compliance`, `policy-463.10`, `sop-205.5.1`, `uop`, `ncric`, `external-sharing`, `flock-msa`, `msa-5.3`, `website-posting`. Adding a new tag means editing `pra_tags.py`.
  - `policy_promises` — list of `{policy, quote, tests}` objects naming the statutes/policies the PRA tests against
  - `prior_pra_refs` — list of `{ref, relation, note}` for cross-referenced W-ids
  - `filed_because` — one-paragraph "why this PRA exists"
  - `status_override` — usually `null`. Set to `"closed"` if the PRA's context paragraph quotes closure language about a *parent* request (e.g., "on YYYY-MM-DD SMPD closed PRA W012XXX") — the parser anchors on "considers ... closed" and gets confused by quoted parent-closure language. Set to `"awaiting_initial"` for just-filed PRAs the parser may otherwise mark `needs_review` due to similar quoting.
  - `notes` — disposition summary if closed, statutory due date if open

- **Validate** by running `uv run python scripts/build_pra_registry.py`. It errors on unknown tags. Re-run `python3 scripts/lint_findings.py` to catch any docs/SMPD_ALPR_Findings.md issues.

- Verify status via `jq '.pras[] | select(.id=="<W-id>") | {id, title: .curated.title, status: .display_status}' docs/data/pra_registry.json` and fix `status_override` if the displayed status is wrong.

## 5. Commit + push + PR

Stage only the new PRA folders and metadata.json files (not `docs/data/pra_registry.json` — it's gitignored). Commit message style: `chore: scrape + curate N PRAs (Wxxxx, ...)`. No Co-Authored-By. No "Generated with Claude Code" in the PR body.

```
git push -u origin pra-discover-<MMDD>
gh pr create --title "..." --body "..."
```

Report the PR URL.

## 6. Cleanup

After the PR is created (the remote branch persists; nothing is lost):

```
git worktree remove .claude/worktrees/pra-discover-<MMDD>
```

If the remove complains about uncommitted changes, stop and report — don't `--force`.

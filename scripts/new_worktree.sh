#!/usr/bin/env bash
# Create a worktree under .claude/worktrees/<name> that skips the raw Flock
# transparency-portal scrapes (assets/transparency.flocksafety.com/**/*.html
# and *.pdf). Those two extensions alone account for ~3GB per full checkout
# (see git ls-tree -r -l origin/main -- assets/transparency.flocksafety.com);
# nothing in `make build`/`make test` or the curators reads them directly —
# generators consume the parsed *.json, and CLAUDE.md already forbids reading
# the raw *.html there. The OCR *.txt sidecars and *.json stay, so the
# worktree still has everything the pipeline and parser-adjacent debugging
# normally need.
#
# Usage: scripts/new_worktree.sh <name>
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: $0 <worktree-name>" >&2
  exit 1
fi
name="$1"

repo_root="$(git rev-parse --show-toplevel)"
worktree_path="$repo_root/.claude/worktrees/$name"

git -C "$repo_root" fetch origin main
git -C "$repo_root" worktree add --no-checkout "$worktree_path" -b "$name" origin/main

git -C "$worktree_path" sparse-checkout init --no-cone
git -C "$worktree_path" sparse-checkout set --stdin <<'PATTERNS'
/*
!/assets/transparency.flocksafety.com/**/*.html
!/assets/transparency.flocksafety.com/**/*.pdf
PATTERNS
git -C "$worktree_path" checkout "$name"

echo "Worktree ready: $worktree_path"
echo "Sparse: Flock raw *.html/*.pdf excluded (json + OCR txt sidecars kept)."
echo
echo "Need the raw html/pdf for one agency (e.g. parser debugging)?"
echo "  git -C \"$worktree_path\" sparse-checkout add '/assets/transparency.flocksafety.com/<agency>/*.html' '/assets/transparency.flocksafety.com/<agency>/*.pdf'"
echo "Need everything back?"
echo "  git -C \"$worktree_path\" sparse-checkout disable"

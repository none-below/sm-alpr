#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
#
# Post a log's `PARSE_ERROR:` lines to the rolling parser issue.
#
#   scripts/report_parse_errors.sh <log-file> <intro-markdown-file>
#
# Shared by refresh-flock-data.yml and probe-slugs.yml so their issue
# handling can't drift. Needs `gh` (GH_TOKEN) and `jq`.
#
# - Only the issue titled "$TITLE" is the rolling issue. Picking "newest
#   open issue labeled parser" sent 420 bot comments to a human-filed
#   parser bug (#645) instead.
# - A failed lookup skips posting. Treating it as "no issue" minted
#   duplicate rolling issues.
# - The dedupe hash ignores capture dates, so an unfixed breakage doesn't
#   look new every UTC day, and is checked against every hash already on
#   the issue, not just the last comment, so alternating error sets don't
#   repost.
set -uo pipefail

TITLE="Parser tripped on a new Flock portal variant"
LOG=$1
INTRO=$2

[ -f "$LOG" ] || exit 0
ERRORS=$(grep -E "PARSE_ERROR: " "$LOG" | sort -u || true)
[ -n "$ERRORS" ] || exit 0

ERRORS_HASH=$(printf '%s\n' "$ERRORS" \
  | sed -E 's/[0-9]{4}-[0-9]{2}-[0-9]{2}//g' \
  | sort -u | sha256sum | cut -d' ' -f1)
MARKER="parse-errors-hash: $ERRORS_HASH"

BODY=$(mktemp)
trap 'rm -f "$BODY"' EXIT
{
  cat "$INTRO"
  echo
  echo '```'
  printf '%s\n' "$ERRORS"
  echo '```'
  echo
  echo "Run: ${GITHUB_SERVER_URL:-}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"
  echo
  echo "<!-- $MARKER -->"
} > "$BODY"

if ! ISSUES=$(gh issue list --label parser --state open --limit 100 \
    --json number,title); then
  echo "::warning::Couldn't list parser issues; not reporting parse errors this run."
  exit 0
fi
EXISTING=$(printf '%s' "$ISSUES" | jq -r --arg t "$TITLE" \
  '[.[] | select(.title == $t)] | sort_by(.number) | .[0].number // empty')

if [ -z "$EXISTING" ]; then
  gh issue create --title "$TITLE" --label parser --body-file "$BODY"
  exit
fi

if ! ISSUE=$(gh api "repos/{owner}/{repo}/issues/$EXISTING") \
   || ! COMMENTS=$(gh api --paginate "repos/{owner}/{repo}/issues/$EXISTING/comments"); then
  echo "::warning::Couldn't read issue #$EXISTING; not reporting parse errors this run."
  exit 0
fi
# Not `grep -q`: an early exit can SIGPIPE jq, and pipefail would turn a
# match into a miss.
if { printf '%s' "$ISSUE" | jq -r '.body // ""'
     printf '%s' "$COMMENTS" | jq -r '.[].body // ""'
   } | grep -F "$MARKER" > /dev/null; then
  echo "These parse errors are already reported on issue #$EXISTING; not commenting."
  exit 0
fi
gh issue comment "$EXISTING" --body-file "$BODY"

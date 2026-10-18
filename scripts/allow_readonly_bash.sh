#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 zero-below
#
# PreToolUse(Bash) permission hook: auto-allow provably read-only inspection
# commands — including shell for-loops, test brackets, command substitution,
# and `git -C <path> <read-only-subcommand>` forms that prefix-based
# permission allow rules cannot express.
#
# Scoped write carve-outs (still auto-allowed): cp / pdftotext writing ONLY
# into the session tmp root (/private/tmp/claude-501/...), and curl fetching
# ONLY https://web.archive.org/ with output confined to the same root —
# web.archive.org is already an allowed WebFetch domain, so this is the same
# trust decision.
#
# Fail-safe direction: any doubt produces NO output, which falls through to
# the normal permission flow (a prompt). A bug here costs an extra prompt,
# never a silent allow of a mutating command — mutators are vetoed before
# the allow path is reached.

set -uo pipefail

cmd=$(jq -r '.tool_input.command // empty' 2>/dev/null) || exit 0
[ -n "$cmd" ] || exit 0

deny() { exit 0; } # no output -> normal permission flow decides

TMPROOT='/private/tmp/claude-501/'

# --- veto 1: any output redirection except >/dev/null and N>&M ------------
stripped=$(printf '%s' "$cmd" | sed -E 's#[0-9]*&?>+[[:space:]]*/dev/null##g; s#[0-9]*>&[0-9]+##g')
case "$stripped" in *'>'*) deny ;; esac

# --- veto 2: mutating / network / interpreter / escape-hatch tokens -------
printf '%s\n' "$cmd" | grep -qE '\b(rm|mv|dd|chmod|chown|chgrp|mkdir|rmdir|touch|ln|install|tee|truncate|shred|wget|nc|ncat|telnet|ssh|scp|sftp|rsync|kill|pkill|killall|sudo|doas|python|python3|node|deno|bun|perl|ruby|php|osascript|xargs|eval|exec|source|env|make|brew|npm|npx|pnpm|yarn|pip|pip3|uv|gh|git-lfs|open|launchctl|defaults|crontab|system)\b' && deny
printf '%s\n' "$cmd" | grep -qE '\bsed[[:space:]]+-[a-zA-Z]*i' && deny
printf '%s\n' "$cmd" | grep -qE '\bfind\b[^;|&]*(-delete|-exec|-execdir|-ok|-okdir|-fprint|-fprintf|-fls)\b' && deny

# --- carve-out: cp may write only into the session tmp root ----------------
if printf '%s\n' "$cmd" | grep -qE '\bcp\b'; then
  while IFS= read -r c; do
    [ -n "$c" ] || continue
    dest=$(printf '%s' "$c" | sed -E 's/[[:space:]]+$//' | awk '{print $NF}' | tr -d '"' | tr -d "'")
    case "$dest" in "$TMPROOT"*) : ;; *) deny ;; esac
  done < <(printf '%s\n' "$cmd" | grep -oE '\bcp[[:space:]][^;|&`$()]*')
fi

# --- carve-out: pdftotext may write only to stdout (-) or the tmp root -----
if printf '%s\n' "$cmd" | grep -qE '\bpdftotext\b'; then
  while IFS= read -r c; do
    [ -n "$c" ] || continue
    last=$(printf '%s' "$c" | sed -E 's/[[:space:]]+$//' | awk '{print $NF}' | tr -d '"' | tr -d "'")
    case "$last" in -|"$TMPROOT"*) : ;; *) deny ;; esac
  done < <(printf '%s\n' "$cmd" | grep -oE '\bpdftotext[[:space:]][^;|&`$()]*')
fi

# --- carve-out: curl only to web.archive.org, output only to tmp root ------
if printf '%s\n' "$cmd" | grep -qE '\bcurl\b'; then
  # attached-form -oFILE is unparseable here; require the spaced form
  printf '%s\n' "$cmd" | grep -qE '(^|[[:space:]])-o[^[:space:]]' && deny
  while IFS= read -r c; do
    [ -n "$c" ] || continue
    urls=$(printf '%s' "$c" | grep -oE 'https?://[^"'"'"'[:space:]]+' || true)
    [ -n "$urls" ] || deny
    while IFS= read -r u; do
      case "$u" in https://web.archive.org/*) : ;; *) deny ;; esac
    done <<< "$urls"
    outs=$(printf '%s' "$c" | grep -oE '(-o|--output|-O)[[:space:]]+[^[:space:]]+' | awk '{print $2}' | tr -d '"' | tr -d "'" || true)
    if [ -n "$outs" ]; then
      while IFS= read -r o; do
        case "$o" in "$TMPROOT"*|/dev/null|https://*) : ;; *) deny ;; esac
      done <<< "$outs"
    fi
  done < <(printf '%s\n' "$cmd" | grep -oE '\bcurl[[:space:]][^;|&`$()]*')
fi

# --- veto 3: git usages must be read-only subcommands ----------------------
while IFS= read -r g; do
  [ -n "$g" ] || continue
  sub=$(printf '%s' "$g" \
    | sed -E 's/^git[[:space:]]+//; s/((-C|--git-dir|--work-tree)[[:space:]]+[^[:space:]]+[[:space:]]+|-c[[:space:]]+[^[:space:]]+[[:space:]]+|--no-pager[[:space:]]+)*//' \
    | awk '{print $1}')
  case "$sub" in
    log|show|diff|status|blame|grep|shortlog|describe|reflog|rev-parse|rev-list|cat-file|ls-files|ls-tree) : ;;
    worktree) printf '%s' "$g" | grep -qE '\bworktree[[:space:]]+list\b' || deny ;;
    *) deny ;;
  esac
done < <(printf '%s\n' "$cmd" | grep -oE '\bgit[[:space:]][^;|&`$()]*' || true)

# --- positive check: every command-position word must be a known reader ----
SAFE=' ls sed grep rg find wc cat head tail jq diff mdls mdfind echo printf true false pwd date awk sort uniq cut tr column basename dirname stat file which type test set cd git realpath readlink du df nl od strings comm join paste rev expr seq shasum sha256sum cksum cp curl pdftotext '

norm=$(printf '%s' "$cmd" | tr '\n' ';')
# Single-quoted spans are pure data in shell (no expansion, no execution) —
# blank them BEFORE splitting so pipes inside rg patterns / jq programs
# ('a|b', '[.x] | @tsv') don't masquerade as shell pipes. Vetoes above
# already scanned the raw text, quotes included.
norm=$(printf '%s' "$norm" | sed -E "s/'[^']*'/ QSTR /g")
norm=${norm//&&/;}
norm=${norm//||/;}
norm=${norm//|/;}
norm=$(printf '%s' "$norm" | sed -E 's/\$\(/;/g; s/`/;/g; s/[(){}]/;/g')

IFS=';' read -r -a parts <<< "$norm"
for p in "${parts[@]}"; do
  p="${p#"${p%%[![:space:]]*}"}"          # ltrim
  [ -n "$p" ] || continue
  # quote/punctuation residue left over from substitution splitting
  if printf '%s' "$p" | grep -qE '^["'"'"'[:space:]:.,-]*$'; then continue; fi
  # argument fragments: splitting on $( ) inside a quoted string strands the
  # rest of the string (e.g. ` $d"` from `echo "$(wc -l < "$f") $d"`) in fake
  # command position. A token starting with $ / " / ' is a string fragment,
  # not a command name. Residual risk (a command hidden in a variable built by
  # concatenation) is accepted: veto 2 scans the raw text for literal mutator
  # names, and the OS sandbox backstops actual writes.
  case "$p" in
    \$*|\"*|\'*) continue ;;
  esac
  # strip leading '!' and compound keywords (if/while run a command; strip & recheck)
  keep=1
  while [ $keep -eq 1 ]; do
    keep=0
    case "$p" in
      '! '*) p="${p#!}"; p="${p#"${p%%[![:space:]]*}"}"; keep=1 ;;
      do\ *|then\ *|else\ *|if\ *|elif\ *|while\ *|until\ *) p="${p#* }"; keep=1 ;;
    esac
  done
  case "$p" in
    do|done|then|else|fi|esac) continue ;;
    done\ *|fi\ *) continue ;;             # trailing redirects already vetted
    for\ *) continue ;;                    # iteration spec; vetoes already ran
    \[*) continue ;;                       # test expression
  esac
  # strip leading VAR=value assignments
  while printf '%s' "$p" | grep -qE '^[A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*([[:space:]]|$)'; do
    rest="${p#* }"
    [ "$rest" = "$p" ] && { p=""; break; }
    p="$rest"
  done
  [ -n "$p" ] || continue
  first="${p%%[[:space:]	]*}"
  case "$SAFE" in
    *" $first "*) : ;;
    *) deny ;;
  esac
done

printf '%s' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"read-only inspection command"}}'

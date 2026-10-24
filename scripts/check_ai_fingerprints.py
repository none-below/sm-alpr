#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Flag AI-assistant fingerprints in text: chat-UI citation residue, AI utm
tracking parameters, assistant attribution lines and boilerplate, and hidden
or non-standard Unicode.

These are the deterministic leftovers that copy-paste carries along. The
scanner cannot see statistical watermarks (SynthID, Claude's), which need
the vendor's key, and a clean result says nothing about who wrote the text.

Usage:
  check_ai_fingerprints.py FILE...          scan files ("-" reads stdin)
  check_ai_fingerprints.py --tree           scan tracked text (skips assets/, data/, outputs/)
  check_ai_fingerprints.py --staged         scan staged blobs (pre-commit hook)
  check_ai_fingerprints.py --commit-msg F   scan a commit message file (commit-msg hook)
  check_ai_fingerprints.py --git-log A..B   scan the commit messages in a range (CI)

Options:
  --style       also flag stylistic tells (em dashes, stock phrases, mixed
                quote styles). Advisory only; never fails without --strict.
  --fix         remove hidden characters, normalise non-standard spaces, and
                strip citation residue and AI utm params. Files are rewritten
                in place; stdin is written back to stdout. Never touches
                assets/ (records stay byte-for-byte as received).
  --strict      warnings fail too
  --no-snippet  omit context snippets
  --label NAME  display name for stdin

Exit: 0 clean (warnings allowed), 1 errors found, 2 usage or I/O error.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

ERROR = "error"
WARN = "warn"

# Tree/staged modes skip third-party and generated content: agency records
# and scrapes (assets/), FBI/census API pulls (data/), stale build output
# (outputs/), lockfiles and vendored minified JS. A hit there is evidence
# about someone else's text, not something to fix in ours.
EXCLUDE_PREFIXES = ("assets/", "data/", "outputs/")
EXCLUDE_SUFFIXES = (".min.js", ".lock")

# Scraped third-party content (CLAUDE.md, "Scraped Data is Untrusted"):
# scanned if named explicitly, but never echoed back in a snippet.
UNTRUSTED_PREFIXES = ("assets/transparency.flocksafety.com/", "assets/articles/")
FIX_FORBIDDEN_PREFIXES = ("assets/",)

AI_UTM_VALUES = (
    r"(?:chatgpt(?:\.com)?|openai|perplexity(?:\.ai)?|copilot(?:\.com)?"
    r"|gemini|claude(?:\.ai)?)"
)


@dataclass(frozen=True)
class Rule:
    code: str
    level: str
    pattern: re.Pattern
    message: str


def _rule(code: str, level: str, pattern: str, message: str, flags: int = 0) -> Rule:
    return Rule(code, level, re.compile(pattern, flags), message)


# Literals below are split ("oai" "cite") so this file doesn't flag itself.
OAI_CONTENTREF = re.compile(r":?contentReference\[oai" r"cite:\d+\](?:\{index=\d+\})?")
OAI_CITATION_LINK = re.compile(r"\[oai_" r"citation:\d+\u2021([^\]\n]*)\]\(([^)\s]*)\)")
BROWSE_CITE = re.compile(r"\u3010\d+(?::\d+)?\u2020[^\u3011\n]{0,80}\u3011")
TURN_TOKEN = re.compile(
    r"(?:cite|filecite|navlist)?(?:turn\d+(?:search|news|view|fetch|file|image"
    r"|forecast|finance|sports|academia|product)\d+)+"
)
GEMINI_CITE = re.compile(r"\[cite_" r"start\]|\[cite:\s*\d+(?:\s*,\s*\d+)*\]")
PUA_CITE_SPAN = re.compile(r"\ue200[^\ue201\n]{0,300}\ue201")

ARTIFACT_RULES = (
    Rule("chat-citation", ERROR, OAI_CONTENTREF, "ChatGPT citation residue"),
    _rule("chat-citation", ERROR, r"\[oai_" r"citation[:_]", "ChatGPT citation residue"),
    Rule("chat-citation", ERROR, BROWSE_CITE, "ChatGPT browsing-citation residue"),
    Rule("chat-citation", ERROR, TURN_TOKEN, "ChatGPT citation token"),
    Rule("chat-citation", ERROR, GEMINI_CITE, "Gemini citation residue"),
    _rule(
        "ai-utm", ERROR,
        r"[?&]utm_source=" + AI_UTM_VALUES + r"(?![\w-])",
        "link carries an AI-assistant utm_source (added when an assistant cites it)",
        re.IGNORECASE,
    ),
    _rule(
        "ai-attribution", ERROR,
        r"co-authored-by:\s*(?:claude|copilot|chatgpt|cursor|gemini|codex|devin)\b",
        "AI co-author trailer", re.IGNORECASE,
    ),
    _rule("ai-attribution", ERROR, r"noreply@anthropic\.com", "Anthropic noreply address",
          re.IGNORECASE),
    _rule(
        "ai-attribution", ERROR,
        r"generated (?:with|by) \[?(?:claude(?: code)?|chatgpt|copilot|cursor|codex|gemini)\b",
        "AI 'generated with' line", re.IGNORECASE,
    ),
    _rule(
        "assistant-boilerplate", ERROR,
        r"\bas an ai(?: language)? model\b|\bas a large language model\b",
        "assistant disclaimer", re.IGNORECASE,
    ),
    _rule(
        "assistant-boilerplate", WARN,
        r"\bI hope this helps\b"
        r"|^\s*(?:certainly|sure|absolutely|of course)!"
        r"|\bhere(?:'s|\u2019s| is) (?:a|the|your) (?:revised|polished|refined|updated"
        r"|rewritten|tightened|cleaned-up|final) (?:version|draft)\b"
        r"|\blet me know if you(?:'d|\u2019d| would) like\b"
        r"|\bfeel free to (?:adjust|tweak|modify|customi[sz]e)\b",
        "chat-assistant boilerplate", re.IGNORECASE,
    ),
    # Title Case or ALL CAPS only, and not right after an identifier, so
    # `rec[name]` indexing and lowercase `[name]` redaction tokens pass.
    _rule(
        "placeholder", WARN,
        r"(?<![\w\]])\[(?:"
        r"Your (?:Name|Title|Organization|Email|Phone(?: Number)?|Address)|Recipient(?:'s Name)?"
        r"|Name|Date|Insert [^\]\n]{1,40}"
        r"|YOUR (?:NAME|TITLE|ORGANIZATION|EMAIL|PHONE(?: NUMBER)?|ADDRESS)|RECIPIENT|NAME|DATE"
        r"|INSERT [^\]\n]{1,40})\]",
        "unfilled template placeholder",
    ),
)

ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u180e]")
HIDDEN_RULES = (
    _rule("invisible-operator", ERROR, r"[\u2061-\u2064]", "invisible math operator"),
    _rule("bidi-control", ERROR, r"[\u200e\u200f\u061c\u202a-\u202e\u2066-\u2069]",
          "bidirectional control character"),
    _rule("tag-character", ERROR, r"[\U000e0000-\U000e007f]",
          "Unicode tag character (can carry hidden ASCII)"),
    _rule("variation-selector", ERROR, r"[\U000e0100-\U000e01ef]|[\ufe00-\ufe0f]{2,}",
          "variation-selector run (can carry hidden data)"),
    _rule("private-use", ERROR, r"[\ue000-\uf8ff\U000f0000-\U000ffffd\U00100000-\U0010fffd]",
          "private-use character (chat UIs use these as citation markers)"),
    _rule("invisible-filler", ERROR, r"[\u034f\u115f\u1160\u3164\uffa0\u2800\ufff9-\ufffb]",
          "invisible filler character"),
    _rule("soft-hyphen", WARN, r"\u00ad", "soft hyphen (invisible unless the line breaks)"),
    _rule("odd-space", WARN, r"[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]",
          "non-standard space (word processors and some chat UIs insert these)"),
    _rule("line-separator", WARN, r"[\u2028\u2029]", "Unicode line/paragraph separator"),
)

STOCK_PHRASES = (
    r"delv(?:e|es|ed|ing)", r"tapestry", r"testament to",
    r"it(?:'s|\u2019s| is) (?:important|worth|crucial) to (?:note|remember|mention)",
    r"worth noting", r"in today(?:'s|\u2019s) (?:fast-paced|digital|ever-changing|modern) world",
    r"ever-(?:evolving|changing)", r"evolving landscape",
    r"navigat(?:e|ing) the (?:complexities|landscape)",
    r"plays? a (?:crucial|pivotal|vital|key) role", r"pivotal", r"multifaceted",
    r"underscor(?:e|es|ed|ing)", r"serves as a (?:testament|reminder|beacon)",
    r"stands as a", r"embark(?:s|ed|ing)? on", r"(?:deep|closer) dive", r"dive (?:deeper )?into",
    r"game[- ]changer", r"(?:unlock|harness)(?:es|s|ed|ing)? the (?:power|potential)",
    r"seamless(?:ly)?", r"holistic", r"in conclusion", r"in summary", r"to summari[sz]e",
    r"moreover", r"furthermore", r"myriad", r"plethora", r"realm", r"intricate",
    r"bustling", r"nestled", r"showcas(?:e|es|ed|ing)",
    r"(?:hope|trust) this (?:email|message|note) finds you well", r"I wanted to reach out",
    r"(?:great|excellent) question", r"you(?:'re|\u2019re| are) absolutely right",
    r"at the end of the day",
)
STYLE_RULES = (
    _rule("em-dash", WARN, r"\u2014", "em dash (a common AI tell when frequent)"),
    _rule("stock-phrase", WARN, r"\b(?:" + "|".join(STOCK_PHRASES) + r")\b",
          "stock AI phrase", re.IGNORECASE),
    _rule(
        "not-x-but-y", WARN,
        r"\bnot (?:just|only|merely|simply)\b[^.!?\n]{1,80}?\bbut\b"
        r"|\b(?:isn't|isn\u2019t|is not|aren't|aren\u2019t|wasn't|wasn\u2019t) "
        r"(?:just|only|merely) about\b",
        "'not just X but Y' construction", re.IGNORECASE,
    ),
)

ODD_SPACE_CHARS = re.compile(r"[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]")
STRIP_CHARS = re.compile(
    r"[\u200b\u200c\u2060\u180e\u2061-\u2064\u200e\u200f\u061c\u202a-\u202e\u2066-\u2069"
    r"\U000e0000-\U000e007f\U000e0100-\U000e01ef\ue000-\uf8ff\U000f0000-\U000ffffd"
    r"\U00100000-\U0010fffd\u034f\u115f\u1160\u3164\uffa0\u2800\ufff9-\ufffb\u00ad]"
)
URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]]+")
AI_UTM_SEGMENT = re.compile(r"utm_source=" + AI_UTM_VALUES, re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    label: str
    line: int
    col: int
    level: str
    code: str
    message: str
    snippet: str | None


@dataclass
class Input:
    label: str
    text: str
    snippets_ok: bool = True
    writable: Path | None = None
    ignore: set[int] | None = None
    is_stdin: bool = False


def _is_emoji(ch: str) -> bool:
    o = ord(ch)
    return o >= 0x1F000 or 0x2600 <= o <= 0x27BF or ch == "\ufe0f"


def _emoji_zwj(line: str, i: int) -> bool:
    """A ZWJ joining two emoji (family/profession sequences) is legitimate."""
    return (
        line[i] == "\u200d"
        and 0 < i < len(line) - 1
        and _is_emoji(line[i - 1])
        and _is_emoji(line[i + 1])
    )


def _render(s: str) -> str:
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if ch == "\t":
            out.append(" ")
        elif cat in ("Cc", "Cf", "Co", "Cs", "Zl", "Zp") or (cat == "Zs" and ch != " ") \
                or ch in "\u034f\u115f\u1160\u3164\uffa0\u2800" or 0xE0100 <= ord(ch) <= 0xE01EF \
                or 0xFE00 <= ord(ch) <= 0xFE0F:
            out.append(f"<U+{ord(ch):04X}>")
        else:
            out.append(ch)
    return "".join(out)


def _snippet(line: str, start: int, end: int) -> str:
    lo, hi = max(0, start - 30), min(len(line), end + 30)
    s = _render(line[lo:hi]).strip()
    return ("..." if lo else "") + s[:120] + ("..." if hi < len(line) else "")


def _char_message(base: str, text: str) -> str:
    cps = sorted({f"U+{ord(c):04X}" for c in text})
    return f"{base} ({', '.join(cps[:4])})"


def scan_text(text: str, label: str, *, style: bool = False, snippets: bool = True,
              ignore_lines: set[int] | None = None) -> list[Finding]:
    findings: list[Finding] = []
    lines = text.split("\n")

    def add(lineno: int, line: str, m: re.Match, rule: Rule, message: str | None = None) -> None:
        findings.append(Finding(
            label, lineno, m.start() + 1, rule.level, rule.code, message or rule.message,
            _snippet(line, m.start(), m.end()) if snippets else None,
        ))

    zw_rule = Rule("zero-width", ERROR, ZERO_WIDTH, "zero-width character")
    for lineno, raw in enumerate(lines, 1):
        if ignore_lines and lineno in ignore_lines:
            continue
        line = raw[:-1] if raw.endswith("\r") else raw
        for rule in ARTIFACT_RULES:
            for m in rule.pattern.finditer(line):
                add(lineno, line, m, rule)
        for m in ZERO_WIDTH.finditer(line):
            if _emoji_zwj(line, m.start()):
                continue
            if lineno == 1 and m.start() == 0 and m.group() == "\ufeff":
                add(lineno, line, m, Rule("byte-order-mark", WARN, ZERO_WIDTH, ""),
                    "byte-order mark at start of text")
            else:
                add(lineno, line, m, zw_rule, _char_message(zw_rule.message, m.group()))
        for rule in HIDDEN_RULES:
            for m in rule.pattern.finditer(line):
                add(lineno, line, m, rule, _char_message(rule.message, m.group()))
        if style:
            for rule in STYLE_RULES:
                for m in rule.pattern.finditer(line):
                    msg = rule.message
                    if rule.code == "stock-phrase":
                        msg = f"{rule.message}: '{m.group()}'"
                    add(lineno, line, m, rule, msg)

    if style:
        findings.extend(_mixed_quotes(lines, label, snippets))
    findings.sort(key=lambda f: (f.line, f.col))
    return findings


def _mixed_quotes(lines: list[str], label: str, snippets: bool) -> list[Finding]:
    """Straight and curly quotes in one text usually means pieces were
    pasted from different sources (a chat window vs a word processor)."""
    out = []
    for straight, curly, name in (('"', "\u201c\u201d", "double quotes"),
                                  ("'", "\u2018\u2019", "apostrophes/single quotes")):
        has_straight = any(straight in ln for ln in lines)
        for lineno, line in enumerate(lines, 1):
            idx = next((i for i, c in enumerate(line) if c in curly), -1)
            if idx >= 0:
                if has_straight:
                    out.append(Finding(
                        label, lineno, idx + 1, WARN, "mixed-quotes",
                        f"mix of straight and curly {name} (pasted from more than one source?)",
                        _snippet(line, idx, idx + 1) if snippets else None,
                    ))
                break
    return out


def _strip_ai_utm(url: str) -> str:
    trail = ""
    while url and url[-1] in ".,;:!?":
        trail = url[-1] + trail
        url = url[:-1]
    base, sep, rest = url.partition("?")
    if not sep:
        return url + trail
    query, hsep, frag = rest.partition("#")
    kept = [seg for seg in query.split("&") if not AI_UTM_SEGMENT.fullmatch(seg)]
    if len(kept) == len(query.split("&")):
        return url + trail
    new = base + ("?" + "&".join(kept) if kept else "") + (hsep + frag if hsep else "")
    return new + trail


def fix_text(text: str) -> str:
    """Remove the mechanically fixable fingerprints. Leaves attribution lines,
    boilerplate, placeholders and style alone: those need a human edit."""
    text = PUA_CITE_SPAN.sub("", text)
    text = OAI_CONTENTREF.sub("", text)
    text = OAI_CITATION_LINK.sub(r"[\1](\2)", text)
    text = BROWSE_CITE.sub("", text)
    text = GEMINI_CITE.sub("", text)
    text = TURN_TOKEN.sub("", text)
    text = URL_RE.sub(lambda m: _strip_ai_utm(m.group()), text)
    text = STRIP_CHARS.sub("", text)
    text = re.sub(r"[\ufe00-\ufe0f]{2,}", "", text)
    text = re.sub(r"\ufeff", "", text)
    text = re.sub(r"\u200d", lambda m: "\u200d" if _emoji_zwj(m.string, m.start()) else "", text)
    text = ODD_SPACE_CHARS.sub(" ", text)
    return re.sub(r"[\u2028\u2029]", "\n", text)


def _under(rel: str, prefixes: tuple[str, ...]) -> bool:
    return any(rel.startswith(p) or ("/" + p) in rel for p in prefixes)


def _excluded(rel: str) -> bool:
    return rel.startswith(EXCLUDE_PREFIXES) or rel.endswith(EXCLUDE_SUFFIXES)


def _git(*args: str) -> bytes:
    return subprocess.run(("git",) + args, check=True, capture_output=True).stdout


def _decode(data: bytes) -> str | None:
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _commit_msg_ignored(text: str) -> set[int]:
    ignored, cut = set(), False
    for lineno, line in enumerate(text.split("\n"), 1):
        if line.startswith("# ") and ">8" in line and "---" in line:
            cut = True
        if cut or line.startswith("#"):
            ignored.add(lineno)
    return ignored


def _gha_escape(s: str, prop: bool = False) -> str:
    s = s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return s.replace(":", "%3A").replace(",", "%2C") if prop else s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--tree", action="store_true")
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--commit-msg", metavar="FILE")
    mode.add_argument("--git-log", metavar="RANGE")
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--style", action="store_true")
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--no-snippet", action="store_true")
    ap.add_argument("--label", default="<stdin>")
    args = ap.parse_args(argv)

    tree_mode = args.tree or args.staged or args.git_log
    if args.paths and (tree_mode or args.commit_msg):
        ap.error("file arguments can't be combined with --tree/--staged/--commit-msg/--git-log")
    if not args.paths and not (tree_mode or args.commit_msg):
        ap.error("nothing to scan: give files, '-' for stdin, or a mode flag")
    if args.fix and (args.staged or args.git_log or args.commit_msg):
        ap.error("--fix works on files, --tree or stdin only")

    inputs: list[Input] = []
    try:
        if args.tree or args.staged:
            root = Path(_git("rev-parse", "--show-toplevel").decode().strip())
            if args.tree:
                names = _git("-C", str(root), "ls-files", "-z").decode().split("\0")
            else:
                names = _git("-C", str(root), "diff", "--cached", "--name-only", "-z",
                             "--diff-filter=ACMR").decode().split("\0")
            for rel in filter(None, names):
                if _excluded(rel):
                    continue
                if args.tree:
                    p = root / rel
                    if not p.is_file() or p.is_symlink():
                        continue  # sparse checkout, or a link we won't follow
                    data = p.read_bytes()
                else:
                    data = _git("-C", str(root), "show", f":{rel}")
                text = _decode(data)
                if text is not None:
                    inputs.append(Input(rel, text, writable=root / rel if args.tree else None))
        elif args.git_log:
            log = _git("log", "--format=%H%x00%B%x1e", args.git_log).decode("utf-8", "replace")
            for rec in filter(str.strip, log.split("\x1e")):
                sha, _, body = rec.strip("\n").partition("\0")
                inputs.append(Input(f"commit {sha[:9]}", body))
        elif args.commit_msg:
            text = Path(args.commit_msg).read_text(encoding="utf-8", errors="replace")
            inputs.append(Input("commit message", text, ignore=_commit_msg_ignored(text)))
        else:
            for path in args.paths:
                if path == "-":
                    text = sys.stdin.read()
                    inputs.append(Input(args.label, text, is_stdin=True))
                    continue
                p = Path(path)
                try:
                    rel = p.resolve().relative_to(Path.cwd().resolve()).as_posix()
                except ValueError:
                    rel = p.resolve().as_posix()
                if p.is_symlink():
                    print(f"{path}: symlink, not followed", file=sys.stderr)
                    continue
                text = _decode(p.read_bytes())
                if text is None:
                    print(f"{path}: binary or not UTF-8, skipped", file=sys.stderr)
                    continue
                writable = None if _under(rel, FIX_FORBIDDEN_PREFIXES) else p
                if args.fix and writable is None:
                    print(f"{path}: under assets/, --fix won't modify records", file=sys.stderr)
                inputs.append(Input(path, text, snippets_ok=not _under(rel, UNTRUSTED_PREFIXES),
                                    writable=writable))
    except subprocess.CalledProcessError as e:
        print(f"git failed: {e.stderr.decode(errors='replace').strip()}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    findings: list[Finding] = []
    fixed_files = 0
    stdin_fixed: str | None = None
    for inp in inputs:
        text = inp.text
        if args.fix and (inp.writable is not None or inp.is_stdin):
            text = fix_text(text)
            if inp.is_stdin:
                stdin_fixed = text
            elif text != inp.text:
                inp.writable.write_text(text, encoding="utf-8")
                fixed_files += 1
        findings.extend(scan_text(text, inp.label, style=args.style,
                                  snippets=inp.snippets_ok and not args.no_snippet,
                                  ignore_lines=inp.ignore))

    out = sys.stderr if stdin_fixed is not None else sys.stdout
    gha = os.environ.get("GITHUB_ACTIONS") == "true"
    for f in findings:
        line = f"{f.label}:{f.line}:{f.col}: {f.level}: {f.code}: {f.message}"
        print(line + (f"  | {f.snippet}" if f.snippet else ""), file=out)
        if gha and f.level == ERROR and not f.label.startswith(("commit ", "<", "PR ")):
            print(f"::error file={_gha_escape(f.label, True)},line={f.line},col={f.col}"
                  f"::{_gha_escape(f.code + ': ' + f.message)}", file=out)

    n_err = sum(f.level == ERROR for f in findings)
    n_warn = len(findings) - n_err
    summary = f"check_ai_fingerprints: {n_err} error(s), {n_warn} warning(s) in {len(inputs)} input(s)"
    if fixed_files:
        summary += f"; rewrote {fixed_files} file(s)"
    out.flush()
    # Hooks stay silent on a clean commit; everything else confirms the run.
    if findings or fixed_files or not (args.staged or args.commit_msg):
        print(summary, file=sys.stderr)
    if stdin_fixed is not None:
        sys.stdout.write(stdin_fixed)

    return 1 if n_err or (args.strict and n_warn) else 0


if __name__ == "__main__":
    sys.exit(main())

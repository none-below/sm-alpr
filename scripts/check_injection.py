#!/usr/bin/env python3
"""Heuristic scanner for prompt-injection content in untrusted files.

Triage tool for files before letting an LLM read them. Complements the
PreToolUse hook in check_untrusted_read.sh, which blocks reads outright;
this script lets you decide whether a specific file is safe to clear.

Detects:
  - Imperative override language ("ignore previous instructions", role tags)
  - ChatML / system-prompt delimiters (<|im_start|>, [INST], <system>, ...)
  - Anthropic-style sentinels (<..., <system-reminder>)
  - Hidden Unicode (tag chars E0000-E007F, RTL override, zero-width density)
  - HTML hazards (long comments, hidden CSS, suspicious attribute payloads)
  - Encoded payload candidates (long base64/hex blobs)
  - Tool-call leakage (Bash(, Edit(, etc.)

Usage:
  scripts/check_injection.py FILE...                human-readable report
  scripts/check_injection.py --json FILE...         JSON output
  scripts/check_injection.py --threshold N FILES    nonzero exit if score > N
  scripts/check_injection.py --recursive DIR        scan a directory tree
  scripts/check_injection.py --staged               scan staged at-risk files
  scripts/check_injection.py --files a.html b.txt   explicit list

Exit codes:
  0  clean (or LOW/MEDIUM only and score <= threshold)
  1  warnings only (LOW/MEDIUM, score above threshold)
  2  HIGH or CRITICAL findings present (always — regardless of threshold)
  3  read error
"""

import argparse
import html as _html
import json
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path

AT_RISK_PATTERN = re.compile(
    r"^assets/transparency\.flocksafety\.com/.*\.(?:html|txt)$",
    re.IGNORECASE,
)

LOW, MEDIUM, HIGH, CRITICAL = "low", "medium", "high", "critical"
SCORE = {LOW: 1, MEDIUM: 3, HIGH: 7, CRITICAL: 15}

PATTERNS = [
    (CRITICAL, "ignore-previous", re.compile(
        r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,60}"
        r"\b(?:above|previous|prior|earlier|prompt|instruction|directive|"
        r"rule|system|context|guideline)s?\b",
        re.IGNORECASE,
    )),
    (HIGH, "new-role", re.compile(
        r"\b(?:you\s+are\s+now|act\s+as|pretend\s+to\s+be|"
        r"from\s+now\s+on|your\s+new\s+(?:role|task|persona|identity))\b",
        re.IGNORECASE,
    )),
    (HIGH, "new-instructions", re.compile(
        r"\b(?:new|updated|revised|real|true|actual)\s+"
        r"(?:instructions?|task|directive|prompt|role|system\s+message)\b",
        re.IGNORECASE,
    )),
    (HIGH, "role-prefix-line", re.compile(
        r"(?m)^\s*(?:system|assistant|user|human)\s*:\s*\S",
        re.IGNORECASE,
    )),
    (CRITICAL, "chatml-delim", re.compile(
        r"<\|(?:im_start|im_end|endoftext|start|end|system|user|assistant)\|>"
    )),
    (CRITICAL, "fake-role-tag", re.compile(
        r"<\s*/?\s*(?:system|instruction|assistant|user|human|prompt|"
        r"system-reminder|important)\s*>",
        re.IGNORECASE,
    )),
    (HIGH, "anthropic-sentinel", re.compile(
        r"<\s*/?\s*antml:|<\s*/?\s*function_calls\b|<\s*/?\s*invoke\b",
        re.IGNORECASE,
    )),
    (HIGH, "inst-block", re.compile(r"\[INST\]|\[/INST\]")),
    (HIGH, "execute-cmd", re.compile(
        r"\b(?:execute|run|evaluate|invoke)\s+(?:the\s+)?(?:following|this|these)\b",
        re.IGNORECASE,
    )),
    (MEDIUM, "do-not-refuse", re.compile(
        r"\bdo\s+not\s+(?:refuse|decline|object|hesitate|warn|apologize)\b",
        re.IGNORECASE,
    )),
    (MEDIUM, "respond-only", re.compile(
        r"\b(?:respond|reply|answer|output)\s+only\s+(?:with|in)\b",
        re.IGNORECASE,
    )),
    (MEDIUM, "do-not-mention", re.compile(
        r"\bdo\s+not\s+(?:mention|tell|reveal|disclose|notify|inform)\b",
        re.IGNORECASE,
    )),
    (HIGH, "tool-call-syntax", re.compile(
        r"(?:^|[\s>(])"
        r"(?:Bash|Edit|Write|Read|Grep|Glob|WebFetch|WebSearch|TodoWrite|Agent|Task)"
        r"\s*\(\s*\{",
        re.MULTILINE,
    )),
    (HIGH, "jailbreak-name", re.compile(
        r"\b(?:DAN|developer\s+mode|jailbreak|unfiltered\s+mode|god\s+mode|"
        r"safety\s+(?:off|disabled))\b",
        re.IGNORECASE,
    )),
    (MEDIUM, "exfiltrate-data", re.compile(
        r"\b(?:send|post|exfiltrat|forward|upload|email)\b[^.\n]{0,40}"
        r"\b(?:credentials?|secrets?|tokens?|keys?|password|environment|\.env)\b",
        re.IGNORECASE,
    )),
]

HIDDEN_CSS = re.compile(
    r"(?:display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0|"
    r"font-size\s*:\s*0(?:px|pt|em)?|"
    r"color\s*:\s*(?:white|#fff(?:fff)?|rgb\(\s*255\s*,\s*255\s*,\s*255))",
    re.IGNORECASE,
)

# Vendor pseudo-elements that target browser UI chrome (scrollbars,
# resizers, file-upload buttons, etc.). A hiding rule scoped only to
# these cannot hide document text content, so it isn't a prompt-
# injection signal. Flock portals legitimately use
# `::-webkit-scrollbar { display: none }` to suppress the scrollbar.
SAFE_UI_PSEUDO = re.compile(
    r"::?-(?:webkit|moz|ms|o)-(?:scrollbar(?:-[\w-]+)?|"
    r"resizer|slider-thumb|slider-runnable-track|"
    r"progress-(?:bar|value)|meter-(?:bar|inner-element)|"
    r"file-upload-button|search-(?:cancel|decoration)-button|"
    r"inner-spin-button|outer-spin-button|placeholder|"
    r"calendar-picker-indicator|color-swatch|details-marker)",
    re.IGNORECASE,
)
HTML_COMMENT = re.compile(r"<!--(.*?)-->", re.DOTALL)
SUSPICIOUS_ATTRS = re.compile(
    r'\b(?:alt|title|aria-label|aria-describedby|data-[\w-]+)\s*='
    r'\s*"([^"]{300,})"',
    re.IGNORECASE,
)
SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)

TAG_CHARS = re.compile(r"[\U000E0000-\U000E007F]")
ZERO_WIDTH = re.compile(r"[\u200B-\u200D\u2060\uFEFF]")
BIDI_OVERRIDE = re.compile(r"[\u202A-\u202E\u2066-\u2069]")
PRIVATE_USE = re.compile(r"[\uE000-\uF8FF]")

LONG_BASE64 = re.compile(r"(?:[A-Za-z0-9+/]{4}){80,}={0,2}")
LONG_HEX = re.compile(r"\b[0-9a-fA-F]{400,}\b")

# Inline image data URIs are routine in HTML and not a useful signal —
# whitelist them so they don't dominate the LOW/MEDIUM noise floor.
DATA_IMAGE_URI_PREFIX = re.compile(r"data:image/[a-zA-Z0-9.+-]+;base64,")

# Confusables fold table. NFKC does NOT collapse cross-script homoglyphs
# (Cyrillic 'е' U+0435 -> ASCII 'e' is not a compatibility relationship in
# Unicode); the reference Confusables table is at
#   https://www.unicode.org/Public/security/latest/confusables.txt
# This is a curated minimal subset covering the ASCII letters that appear
# in the detector patterns. Folded text is scanned in addition to (not
# instead of) the raw text.
CONFUSABLE_FOLD = str.maketrans({
    # Cyrillic small -> Latin small
    "а": "a", "в": "v", "е": "e", "и": "i", "к": "k",
    "м": "m", "н": "n", "о": "o", "р": "p", "с": "c",
    "т": "t", "у": "y", "х": "x",
    "ѕ": "s", "і": "i", "ј": "j", "ҫ": "c",
    # Cyrillic capital -> Latin capital
    "А": "A", "В": "B", "Е": "E", "К": "K",
    "М": "M", "Н": "H", "О": "O", "Р": "P",
    "С": "C", "Т": "T", "Х": "X", "Ѕ": "S",
    "Ј": "J",
    # Greek small -> Latin small (the obvious confusables)
    "α": "a", "ε": "e", "η": "n", "ι": "i", "μ": "m",
    "ν": "v", "ο": "o", "ρ": "p", "σ": "s", "τ": "t",
    "υ": "u", "χ": "x",
    # Greek capital -> Latin capital
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z",
    "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M",
    "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T",
    "Υ": "Y", "Χ": "X",
    # Mathematical alphanumerics that look like ASCII (small subset)
    "Ⅰ": "I", "Ⅱ": "II", "Ⅴ": "V", "Ⅹ": "X",
})

# Invisible characters that visually disappear in nearly every renderer
# but break regex contiguity. An attacker can splice these between every
# letter of <system> and evade pattern matching unless we strip them and
# rescan. Source: https://invisible-characters.com/ + Unicode bidi/format
# control categories.
INVISIBLE_STRIP = re.compile(
    r"["
    r"\u00AD"              # SOFT HYPHEN
    r"\u034F"              # COMBINING GRAPHEME JOINER
    r"\u061C"              # ARABIC LETTER MARK
    r"\u115F\u1160"        # HANGUL CHOSEONG/JUNGSEONG FILLER
    r"\u17B4\u17B5"        # KHMER VOWEL INHERENT AQ/AA
    r"\u180B-\u180E"       # MONGOLIAN free variation selectors + sep
    r"\u200B-\u200F"       # ZWSP/ZWNJ/ZWJ + LRM/RLM
    r"\u202A-\u202E"       # bidi format controls
    r"\u2060"              # WORD JOINER
    r"\u2066-\u2069"       # bidi isolates
    r"\uFE00-\uFE0F"       # variation selectors 1-16
    r"\uFEFF"              # ZWNBSP / BOM
    r"\uFFF9-\uFFFB"       # interlinear annotation anchors
    r"\U000E0000-\U000E007F"  # tag characters
    r"\U000E0100-\U000E01EF"  # variation selectors 17-256
    r"]"
)

MAX_FINDINGS_PER_FILE = 100


@dataclass
class Finding:
    severity: str
    category: str
    line: int
    col: int
    snippet: str

    def score(self) -> int:
        return SCORE[self.severity]


def line_col(text: str, offset: int) -> tuple[int, int]:
    line = text.count("\n", 0, offset) + 1
    col = offset - (text.rfind("\n", 0, offset) + 1) + 1
    return line, col


DANGEROUS_UNICODE = re.compile(
    r"[\u200B-\u200F"
    r"\u202A-\u202E"
    r"\u2060\uFEFF"
    r"\u2066-\u2069"
    r"\uE000-\uF8FF"
    r"\U000E0000-\U000E007F"
    r"]"
)

SENTINEL_BREAK_PATTERNS = [
    (re.compile(r"<\|"), "‹|"),
    (re.compile(r"\|>"), "|›"),
    (re.compile(
        r"<(?=\s*/?\s*(?:antml:|system|assistant|user|human|"
        r"instruction|prompt|function_calls|invoke|system-reminder)\b)",
        re.IGNORECASE,
    ), "‹"),
    (re.compile(r"\[(?=/?INST\])", re.IGNORECASE), "⟦"),
]


def sanitize_snippet(s: str, max_len: int = 160) -> str:
    r"""Render a payload-bearing snippet so it cannot inject into terminals,
    GHA workflow logs, or downstream LLM contexts.

    - C0/C1 controls (incl. ESC, NUL, BEL, OSC) -> \xNN
    - Dangerous Unicode (zero-width, bidi, tag chars, private use) -> \u/\U
    - Sentinel tokens (<|im_start|>, <system>, [INST], etc.) -> broken with sigil
    - GHA workflow-command prefix `::` -> `\:\:`
    """
    s = s.replace("\\", "\\\\")
    out = []
    for ch in s:
        cp = ord(ch)
        if ch in ("\t", "\n", "\r"):
            out.append(" ")
        elif cp < 0x20 or cp == 0x7F or 0x80 <= cp <= 0x9F:
            out.append(f"\\x{cp:02x}")
        else:
            out.append(ch)
    s = "".join(out)

    def _esc(m: "re.Match") -> str:
        return "".join(
            f"\\u{ord(c):04x}" if ord(c) < 0x10000 else f"\\U{ord(c):08x}"
            for c in m.group(0)
        )

    s = DANGEROUS_UNICODE.sub(_esc, s)

    for pat, repl in SENTINEL_BREAK_PATTERNS:
        s = pat.sub(repl, s)

    s = s.replace("::", r"\:\:")

    s = re.sub(r" +", " ", s).strip()
    if len(s) > max_len:
        head = max_len // 2
        tail = max_len - head - 3
        s = s[:head] + " ... " + s[-tail:]
    return s


def make_snippet(text: str, start: int, end: int, ctx: int = 30) -> str:
    s = max(0, start - ctx)
    e = min(len(text), end + ctx)
    body = sanitize_snippet(text[s:e])
    return ("..." if s > 0 else "") + body + ("..." if e < len(text) else "")


def emit(findings, severity, category, text, start, end):
    line, col = line_col(text, start)
    findings.append(Finding(
        severity=severity,
        category=category,
        line=line,
        col=col,
        snippet=make_snippet(text, start, end),
    ))


def emit_decoded(findings, severity, category, decoded, start, end):
    """Emit a finding from a decoded/normalised text. Line/col are 0
    because the decoded offsets don't map back to source positions
    without a per-codepoint position table; the snippet still shows
    what was matched, prefixed so reviewers know not to grep for it."""
    findings.append(Finding(
        severity=severity,
        category=category,
        line=0,
        col=0,
        snippet="[source position unmappable] "
                + make_snippet(decoded, start, end),
    ))


def _stylesheet_hides_content(body: str) -> bool:
    """Return True if a <style> body contains a hiding rule whose
    selector could hide document text content (not just UI chrome).

    Splits on `}` rule boundaries — over-matches on nested @-rules in
    the conservative direction (still flags content-hiding inside
    @media). For each rule with a hiding declaration, accept it as
    benign only if every comma-separated selector targets a known
    browser UI pseudo-element (scrollbar, resizer, etc.).
    """
    cleaned = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
    for rule in cleaned.split("}"):
        if not HIDDEN_CSS.search(rule):
            continue
        if "{" not in rule:
            continue
        selector_part = rule.split("{", 1)[0]
        selectors = [s.strip() for s in selector_part.split(",") if s.strip()]
        if not selectors:
            return True
        if all(SAFE_UI_PSEUDO.search(s) for s in selectors):
            continue
        return True
    return False


def scan_text(text: str, *, html: bool) -> list[Finding]:
    findings: list[Finding] = []

    def capped() -> bool:
        """Cap findings per file at MAX_FINDINGS_PER_FILE. A pattern-rich
        adversarial scrape can otherwise produce 100k+ findings, blowing
        out scan time, JSON size, and CI log volume. Past the cap, emit
        a single CRITICAL too-many-findings entry and stop."""
        if len(findings) >= MAX_FINDINGS_PER_FILE:
            if not findings or findings[-1].category != "too-many-findings":
                findings.append(Finding(
                    severity=CRITICAL,
                    category="too-many-findings",
                    line=0, col=0,
                    snippet=sanitize_snippet(
                        f"capped at {MAX_FINDINGS_PER_FILE} findings; "
                        f"further matches in this file not reported"
                    ),
                ))
            return True
        return False

    # ── Raw-text patterns ────────────────────────────────────────
    for severity, category, pattern in PATTERNS:
        if capped(): return findings
        for m in pattern.finditer(text):
            if capped(): return findings
            emit(findings, severity, category, text, m.start(), m.end())

    for m in TAG_CHARS.finditer(text):
        if capped(): return findings
        emit(findings, CRITICAL, "unicode-tag-char", text, m.start(), m.end())
    for m in BIDI_OVERRIDE.finditer(text):
        if capped(): return findings
        emit(findings, HIGH, "unicode-bidi-override", text, m.start(), m.end())

    zw_count = sum(1 for _ in ZERO_WIDTH.finditer(text))
    if zw_count > 5:
        m = ZERO_WIDTH.search(text)
        if m and not capped():
            emit(findings, MEDIUM, f"zero-width-density({zw_count})",
                 text, m.start(), m.end())

    pu_count = sum(1 for _ in PRIVATE_USE.finditer(text))
    if pu_count > 0:
        m = PRIVATE_USE.search(text)
        if m and not capped():
            emit(findings, MEDIUM, f"private-use-chars({pu_count})",
                 text, m.start(), m.end())

    for m in LONG_BASE64.finditer(text):
        if capped(): return findings
        # Skip inline data:image/...;base64,... URIs — they're routine in
        # HTML/SPA output and not a useful signal. Look at the 64 bytes
        # immediately preceding the match for the URI prefix.
        before = text[max(0, m.start() - 64):m.start()]
        if DATA_IMAGE_URI_PREFIX.search(before):
            continue
        emit(findings, MEDIUM, "long-base64-blob", text, m.start(), m.end())
    for m in LONG_HEX.finditer(text):
        if capped(): return findings
        emit(findings, MEDIUM, "long-hex-blob", text, m.start(), m.end())

    # ── HTML-structure passes (HTML files only) ──────────────────
    if html:
        for m in HTML_COMMENT.finditer(text):
            if capped(): return findings
            body = m.group(1)
            if len(body.strip()) > 200:
                emit(findings, MEDIUM, "long-html-comment",
                     text, m.start(), m.end())
            for severity, category, pattern in PATTERNS:
                if pattern.search(body):
                    emit(findings, HIGH, f"in-comment::{category}",
                         text, m.start(), m.end())
                    break

        for m in SCRIPT_BLOCK.finditer(text):
            if capped(): return findings
            body = m.group(1)
            for severity, category, pattern in PATTERNS:
                if pattern.search(body):
                    emit(findings, HIGH, f"in-script::{category}",
                         text, m.start(), m.end())
                    break

        for m in STYLE_BLOCK.finditer(text):
            if capped(): return findings
            if _stylesheet_hides_content(m.group(1)):
                emit(findings, HIGH, "hidden-css-rule-with-content",
                     text, m.start(), m.end())

        for m in SUSPICIOUS_ATTRS.finditer(text):
            if capped(): return findings
            emit(findings, MEDIUM, "long-attribute-value",
                 text, m.start(), m.end())

        for m in HIDDEN_CSS.finditer(text):
            if capped(): return findings
            emit(findings, LOW, "inline-hidden-css", text, m.start(), m.end())

    # ── Chained normalisation passes (apply to ALL files, not just HTML) ──
    # Each pass operates on the buffer produced by all preceding
    # transformations, so multi-vector evasions (e.g. entity-encoded +
    # invisible-spliced + confusable-substituted) are caught at the
    # latest stage that yields a contiguous match.
    #
    # Dedup against the raw text AND the immediately preceding buffer so
    # a payload revealed at stage N isn't also re-emitted at stages
    # N+1, N+2 (where the same string survives unchanged).
    chained_passes = [
        # 1. Strip invisibles — defeats ZWSP/soft-hyphen splicing evasion
        #    like `<s[ZWSP]y[ZWSP]s[ZWSP]t[ZWSP]e[ZWSP]m>` where [ZWSP]=U+200B.
        ("invisible-stripped", lambda s: INVISIBLE_STRIP.sub("", s)),
        # 2. HTML-entity decode — catches `&lt;system&gt;`. Operates on
        #    the stripped buffer so entity+invisible combos are caught.
        ("entity-decoded",     _html.unescape),
        # 3. NFKC normalise — catches compatibility forms (fullwidth,
        #    ligatures). Does NOT fold cross-script homoglyphs.
        ("nfkc-normalised",    lambda s: unicodedata.normalize("NFKC", s)),
        # 4. Confusables fold — catches cross-script homoglyphs
        #    (Cyrillic 'е', Greek 'τ', etc.).
        ("confusable-folded",  lambda s: s.translate(CONFUSABLE_FOLD)),
    ]
    current = text
    prev = text
    for prefix, transform in chained_passes:
        new_buf = transform(current)
        if new_buf == current:
            continue
        current = new_buf
        for severity, category, pattern in PATTERNS:
            if capped(): return findings
            for m in pattern.finditer(current):
                if capped(): return findings
                matched = m.group(0)
                if matched in text or matched in prev:
                    continue
                emit_decoded(findings, severity,
                             f"{prefix}::{category}",
                             current, m.start(), m.end())
        for m in TAG_CHARS.finditer(current):
            if capped(): return findings
            matched = m.group(0)
            if matched in text or matched in prev:
                continue
            emit_decoded(findings, CRITICAL,
                         f"{prefix}::unicode-tag-char",
                         current, m.start(), m.end())
        for m in BIDI_OVERRIDE.finditer(current):
            if capped(): return findings
            matched = m.group(0)
            if matched in text or matched in prev:
                continue
            emit_decoded(findings, HIGH,
                         f"{prefix}::unicode-bidi-override",
                         current, m.start(), m.end())
        prev = current

    return findings


HTML_HEAD_RE = re.compile(
    r"<\s*(html|head|body|div|span|script|style|svg)\b",
    re.IGNORECASE,
)


def is_html(path: Path, text: str) -> bool:
    if path.suffix.lower() in {".html", ".htm", ".xhtml", ".svg"}:
        return True
    return bool(HTML_HEAD_RE.search(text[:8192]))


def expand_paths(paths, recursive: bool) -> list[Path]:
    """Resolve positional arguments to a list of files. Fails closed
    (sys.exit 3) on missing files so `python check_injection.py -- foo.html
    bar.html` behaves symmetrically with `--files foo.html bar.html` —
    a vanished input must not silently pass."""
    out = []
    missing = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            if not recursive:
                print(f"WARNING: skipping directory {path} (use --recursive)",
                      file=sys.stderr)
                continue
            for sub in sorted(path.rglob("*")):
                if sub.is_file():
                    out.append(sub)
        elif path.is_file():
            out.append(path)
        else:
            missing.append(str(path))
    if missing:
        print(
            f"ERROR: {len(missing)} input file(s) not found:",
            file=sys.stderr,
        )
        for m in missing:
            print(f"  {m}", file=sys.stderr)
        sys.exit(3)
    return out


def get_staged_at_risk_files() -> list[Path]:
    """Staged HTML/TXT under at-risk paths (added/copied/modified).

    Fails closed (sys.exit 3) on git failure so a corrupt/missing repo
    state cannot pass through as an empty list.
    """
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print(
            f"ERROR: git diff --cached failed (rc={result.returncode}): "
            f"{result.stderr.strip()}",
            file=sys.stderr,
        )
        sys.exit(3)
    return [
        Path(line) for line in result.stdout.splitlines()
        if AT_RISK_PATTERN.match(line)
    ]


def severity_rank(s: str) -> int:
    return {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(s, 0)


def render_text(results, *, verdict: str) -> str:
    lines = []
    for r in results:
        path = r["path"]
        score = r["score"]
        findings = r["findings"]
        if not findings:
            lines.append(f"\n=== {path}  (score: 0)  CLEAN ===")
            continue
        worst = max(severity_rank(f.severity) for f in findings)
        tag = ["", "low", "medium", "high", "CRITICAL"][worst].upper() or "CLEAN"
        lines.append(f"\n=== {path}  (score: {score})  worst: {tag} ===")
        for f in sorted(
            findings,
            key=lambda x: (-severity_rank(x.severity), x.line, x.col),
        ):
            lines.append(
                f"  [{f.severity:>8}] {f.category:36s} "
                f"L{f.line}:{f.col}  {f.snippet}"
            )
    lines.append(f"\n--- VERDICT: {verdict} ---")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Heuristic prompt-injection scanner. See module docstring.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("paths", nargs="*",
                   help="files (or directories with --recursive)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of text")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="recurse into directory arguments")
    p.add_argument("--threshold", type=int, default=0,
                   help="warn (exit 1) if score exceeds this; HIGH/CRITICAL "
                        "always exit 2 regardless (default: 0)")
    p.add_argument("--max-bytes", type=int, default=20 * 1024 * 1024,
                   help="skip files larger than this many bytes (default: 20MB)")
    p.add_argument("--staged", action="store_true",
                   help="scan only staged HTML/TXT files under "
                        "assets/transparency.flocksafety.com/")
    p.add_argument("--files", nargs="+",
                   help="scan specific files (alternative to positional paths)")
    args = p.parse_args()

    if args.staged:
        files = get_staged_at_risk_files()
        if not files:
            return 0
    elif args.files:
        files = [Path(f) for f in args.files]
    elif args.paths:
        files = expand_paths(args.paths, args.recursive)
    else:
        p.error("provide paths, --files, or --staged")

    if not files:
        print("ERROR: no files to scan", file=sys.stderr)
        return 3

    results = []
    total_score = 0
    worst_severity_seen = 0

    for path in files:
        if path.is_symlink():
            print(f"WARNING: refusing to follow symlink {path}", file=sys.stderr)
            results.append({
                "path": str(path),
                "score": 0,
                "skipped": "symlink refused",
                "findings": [],
            })
            continue
        try:
            size = path.stat().st_size
            if size > args.max_bytes:
                # Fail closed: an attacker who pads a scrape past --max-bytes
                # could otherwise smuggle injection content through a silent
                # skip. Emit a HIGH-severity finding so it surfaces in CI/hook.
                f = Finding(
                    severity=HIGH,
                    category="oversized-file-not-scanned",
                    line=0, col=0,
                    snippet=sanitize_snippet(
                        f"file size {size} bytes exceeds --max-bytes {args.max_bytes}"
                    ),
                )
                score = f.score()
                total_score += score
                worst_severity_seen = max(
                    worst_severity_seen, severity_rank(HIGH),
                )
                results.append({
                    "path": str(path),
                    "score": score,
                    "skipped": f"size {size} > max-bytes {args.max_bytes}",
                    "findings": [f],
                })
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"ERROR: {path}: {e}", file=sys.stderr)
            return 3

        findings = scan_text(text, html=is_html(path, text))
        score = sum(f.score() for f in findings)
        total_score += score
        if findings:
            worst_severity_seen = max(
                worst_severity_seen,
                max(severity_rank(f.severity) for f in findings),
            )
        results.append({"path": str(path), "score": score, "findings": findings})

    if total_score == 0:
        verdict = "CLEAN — no findings"
    elif worst_severity_seen >= 3:
        verdict = f"REVIEW REQUIRED — {total_score} pts, HIGH/CRITICAL findings"
    else:
        verdict = f"WARNINGS — {total_score} pts, low/medium only"

    if args.json:
        out = []
        for r in results:
            entry = {"path": r["path"], "score": r["score"]}
            if "skipped" in r:
                entry["skipped"] = r["skipped"]
            entry["findings"] = [asdict(f) for f in r["findings"]]
            out.append(entry)
        print(json.dumps(
            {"verdict": verdict, "total_score": total_score, "files": out},
            indent=2,
        ))
    else:
        print(render_text(results, verdict=verdict))

    if worst_severity_seen >= 3:
        return 2
    if total_score > args.threshold:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

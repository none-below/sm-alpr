#!/usr/bin/env python3
"""Generate a local HTML review UI for `mechanical`-status articles.

Reads assets/article_registry.json, pulls the first ~500 chars of each
entry's extracted .txt for inline preview, and writes a single static
HTML file with one row per article. Radios per row (approve/reject/skip),
localStorage persistence, and a "Copy decisions" button at the bottom.

Usage:
  scripts/review_mechanical.py                 # writes /tmp/review_mechanical.html
  scripts/review_mechanical.py --open          # also `open` it
  scripts/review_mechanical.py --out path.html

Output format the button copies — paste back into chat:
  approve: art_001 art_002 ...
  reject:  art_003 art_004 ...
"""

import argparse
import html
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "assets" / "article_registry.json"
DEFAULT_OUT = Path("/tmp/review_mechanical.html")
EXCERPT_CHARS = 600


def excerpt_for(entry: dict) -> str:
    txt_rel = (entry.get("paths") or {}).get("txt")
    if not txt_rel:
        return ""
    path = ROOT / txt_rel
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > EXCERPT_CHARS:
        text = text[:EXCERPT_CHARS].rsplit(" ", 1)[0] + "…"
    return text


def score_of(entry: dict) -> int:
    m = re.search(r"(\d+) pts", entry.get("scanner_verdict") or "")
    return int(m.group(1)) if m else 9999


def build_rows(registry: list[dict]) -> list[dict]:
    rows = []
    for e in registry:
        if e.get("curation_status") != "mechanical":
            continue
        rows.append({
            "id": e.get("article_id"),
            "domain": e.get("source_domain") or "",
            "tier": e.get("tier"),
            "score": score_of(e),
            "title": e.get("title") or "(no title)",
            "url": e.get("url") or "",
            "excerpt": excerpt_for(e),
        })
    rows.sort(key=lambda r: (r["tier"] or 99, r["domain"], r["score"]))
    return rows


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Article review — mechanical backlog</title>
<style>
  :root {
    --bg: #fafaf7; --fg: #1a1a1a; --muted: #666;
    --card: #fff; --border: #e0ddd5;
    --approve: #2d7a2d; --reject: #a33; --skip: #888;
    --tier1: #1a4480; --tier2: #555;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #1a1a1a; --fg: #e8e8e8; --muted: #999;
            --card: #242424; --border: #333; }
  }
  * { box-sizing: border-box; }
  body { font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI",
         sans-serif; background: var(--bg); color: var(--fg);
         margin: 0; padding: 0 0 96px 0; }
  header { position: sticky; top: 0; background: var(--bg);
           border-bottom: 1px solid var(--border); padding: 12px 20px;
           z-index: 10; display: flex; gap: 24px; align-items: baseline; }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; }
  header .counts { font-variant-numeric: tabular-nums; color: var(--muted); }
  header .counts b { color: var(--fg); }
  header .filter { margin-left: auto; }
  header input[type=search] { padding: 4px 8px; font: inherit;
    background: var(--card); border: 1px solid var(--border);
    color: var(--fg); border-radius: 4px; width: 200px; }
  main { padding: 16px 20px; }
  .row { background: var(--card); border: 1px solid var(--border);
         border-radius: 6px; padding: 10px 12px; margin: 0 0 8px;
         display: grid; grid-template-columns: auto 1fr auto;
         gap: 10px 14px; align-items: start; }
  .row[data-state=approve] { border-left: 3px solid var(--approve); }
  .row[data-state=reject]  { border-left: 3px solid var(--reject); }
  .meta { font-size: 12px; color: var(--muted); white-space: nowrap;
          font-variant-numeric: tabular-nums; }
  .meta .id { font-family: ui-monospace, SFMono-Regular, monospace;
              color: var(--fg); }
  .meta .tier1 { color: var(--tier1); font-weight: 600; }
  .meta .tier2 { color: var(--tier2); }
  .meta .score-low  { color: var(--approve); }
  .meta .score-mid  { color: #b87a00; }
  .meta .score-high { color: var(--reject); }
  .title a { color: var(--fg); text-decoration: none;
             font-weight: 500; }
  .title a:hover { text-decoration: underline; }
  .excerpt { font-size: 13px; color: var(--muted);
             margin-top: 4px; max-width: 75ch; }
  .controls { display: flex; gap: 6px; align-items: center; }
  .controls label { padding: 4px 10px; border: 1px solid var(--border);
                    border-radius: 4px; cursor: pointer; font-size: 12px;
                    user-select: none; }
  .controls input { display: none; }
  .controls input:checked + span.lbl-approve { background: var(--approve);
    color: #fff; border-radius: 4px; padding: 4px 10px; margin: -4px -10px; }
  .controls input:checked + span.lbl-reject  { background: var(--reject);
    color: #fff; border-radius: 4px; padding: 4px 10px; margin: -4px -10px; }
  .controls input:checked + span.lbl-skip    { background: var(--skip);
    color: #fff; border-radius: 4px; padding: 4px 10px; margin: -4px -10px; }
  .row.hidden { display: none; }
  footer { position: fixed; bottom: 0; left: 0; right: 0;
           background: var(--bg); border-top: 1px solid var(--border);
           padding: 12px 20px; display: flex; gap: 14px;
           align-items: center; z-index: 10; }
  footer button { font: inherit; padding: 8px 14px;
    background: var(--fg); color: var(--bg); border: 0;
    border-radius: 4px; cursor: pointer; font-weight: 500; }
  footer button.secondary { background: transparent; color: var(--fg);
    border: 1px solid var(--border); font-weight: 400; }
  footer .preview { color: var(--muted); font-size: 12px;
    font-family: ui-monospace, monospace; flex: 1;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .toast { position: fixed; bottom: 70px; left: 50%;
           transform: translateX(-50%); background: var(--fg);
           color: var(--bg); padding: 8px 16px; border-radius: 4px;
           opacity: 0; transition: opacity .2s; pointer-events: none; }
  .toast.show { opacity: 1; }
</style>
</head>
<body>
<header>
  <h1>Article review — mechanical backlog</h1>
  <span class="counts">
    <b id="c-total">0</b> total ·
    <b id="c-approve">0</b> approve ·
    <b id="c-reject">0</b> reject ·
    <b id="c-skip">0</b> skip
  </span>
  <span class="filter">
    <input type="search" id="filter" placeholder="filter by domain/title…">
  </span>
</header>
<main id="rows"></main>
<footer>
  <button id="copy">Copy decisions</button>
  <button id="reset" class="secondary">Reset all</button>
  <span class="preview" id="preview"></span>
</footer>
<div class="toast" id="toast">copied</div>
<script>
const ROWS = __ROWS_JSON__;
const STORAGE_KEY = 'sm-alpr-review-mechanical-v1';
const state = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');

function scoreClass(s) {
  if (s < 50) return 'score-low';
  if (s < 130) return 'score-mid';
  return 'score-high';
}

function render() {
  const main = document.getElementById('rows');
  main.innerHTML = ROWS.map(r => {
    const st = state[r.id] || 'skip';
    const esc = s => s.replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    return `<div class="row" data-state="${st}" data-id="${r.id}"
      data-search="${esc((r.domain + ' ' + r.title).toLowerCase())}">
      <div class="meta">
        <div class="id">${esc(r.id)}</div>
        <div class="tier${r.tier}">tier ${r.tier}</div>
        <div class="${scoreClass(r.score)}">${r.score} pts</div>
      </div>
      <div>
        <div class="title">
          <a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.title)}</a>
          <span style="color:var(--muted);font-size:12px"> · ${esc(r.domain)}</span>
        </div>
        ${r.excerpt ? `<div class="excerpt">${esc(r.excerpt)}</div>` : ''}
      </div>
      <div class="controls">
        <label><input type="radio" name="s-${r.id}" value="approve"
          ${st==='approve'?'checked':''}><span class="lbl-approve">✓</span></label>
        <label><input type="radio" name="s-${r.id}" value="reject"
          ${st==='reject'?'checked':''}><span class="lbl-reject">✗</span></label>
        <label><input type="radio" name="s-${r.id}" value="skip"
          ${st==='skip'?'checked':''}><span class="lbl-skip">–</span></label>
      </div>
    </div>`;
  }).join('');
  updateCounts();
  updatePreview();
}

function updateCounts() {
  let a=0, r=0, s=0;
  ROWS.forEach(row => {
    const v = state[row.id] || 'skip';
    if (v === 'approve') a++;
    else if (v === 'reject') r++;
    else s++;
  });
  document.getElementById('c-total').textContent = ROWS.length;
  document.getElementById('c-approve').textContent = a;
  document.getElementById('c-reject').textContent = r;
  document.getElementById('c-skip').textContent = s;
}

function decisionsText() {
  const approve = ROWS.filter(r => state[r.id] === 'approve').map(r => r.id);
  const reject  = ROWS.filter(r => state[r.id] === 'reject').map(r => r.id);
  const lines = [];
  if (approve.length) lines.push('approve: ' + approve.join(' '));
  if (reject.length)  lines.push('reject: '  + reject.join(' '));
  return lines.join('\\n') || '(no decisions)';
}

function updatePreview() {
  document.getElementById('preview').textContent =
    decisionsText().replace(/\\n/g, ' · ');
}

document.addEventListener('change', e => {
  if (e.target.type !== 'radio') return;
  const row = e.target.closest('.row');
  const id = row.dataset.id;
  state[id] = e.target.value;
  row.dataset.state = e.target.value;
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  updateCounts();
  updatePreview();
});

document.getElementById('copy').addEventListener('click', async () => {
  const txt = decisionsText();
  await navigator.clipboard.writeText(txt);
  const toast = document.getElementById('toast');
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), 1200);
});

document.getElementById('reset').addEventListener('click', () => {
  if (!confirm('Clear all decisions?')) return;
  Object.keys(state).forEach(k => delete state[k]);
  localStorage.removeItem(STORAGE_KEY);
  render();
});

document.getElementById('filter').addEventListener('input', e => {
  const q = e.target.value.toLowerCase().trim();
  document.querySelectorAll('.row').forEach(row => {
    row.classList.toggle('hidden', q && !row.dataset.search.includes(q));
  });
});

render();
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--open", action="store_true",
                    help="open the generated HTML after writing")
    args = ap.parse_args()

    registry = json.loads(REGISTRY.read_text())
    rows = build_rows(registry)
    if not rows:
        print("no mechanical entries pending")
        return 0

    page = HTML_TEMPLATE.replace("__ROWS_JSON__",
                                 json.dumps(rows, ensure_ascii=False))
    args.out.write_text(page, encoding="utf-8")
    print(f"wrote {args.out}  ({len(rows)} rows)")
    if args.open:
        subprocess.run(["open", str(args.out)], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())

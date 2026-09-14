"""Ordering/token invariants for .github/workflows/discover-articles.yml.

Every scheduled discovery run from 2026-07-20 to 2026-09-11 failed — and
silently discarded that day's queued URLs — because the triage-issue step
ran *before* the commit/push step and died on a token that lacks the
Issues scope. Both halves of that failure are pinned here: bookkeeping
steps run after the work is persisted, and issue calls use the workflow
token (the workflow declares `issues: write`), not REFRESH_PAT.

Text-parsed rather than YAML-parsed — pyyaml is not a project dependency.
"""

import re
from pathlib import Path

WORKFLOW = (
    Path(__file__).parent.parent
    / ".github"
    / "workflows"
    / "discover-articles.yml"
)

STEP_RE = re.compile(r"^      - name: (.+)$")


def step_blocks() -> dict[str, str]:
    """Map step name -> that step's YAML block."""
    lines = WORKFLOW.read_text().splitlines()
    starts = [(i, m.group(1).strip())
              for i, line in enumerate(lines)
              if (m := STEP_RE.match(line))]
    blocks = {}
    for idx, (start, name) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        blocks[name] = "\n".join(lines[start:end])
    return blocks


def test_queue_is_pushed_before_triage_bookkeeping():
    names = list(step_blocks())
    assert names.index("Commit and push") < names.index(
        "Triage issue for off-allowlist rejects"
    ), (
        "the triage-issue step must run after the queue is committed and "
        "pushed — otherwise an issue-API failure throws away the URLs "
        "discovered in that run"
    )


def test_triage_step_uses_the_workflow_token():
    block = step_blocks()["Triage issue for off-allowlist rejects"]
    gh_token = [ln for ln in block.splitlines()
                if ln.strip().startswith("GH_TOKEN:")]
    assert gh_token, "triage step must set GH_TOKEN explicitly"
    assert "REFRESH_PAT" not in gh_token[0], (
        "REFRESH_PAT has no Issues scope; issue ops must use "
        "secrets.GITHUB_TOKEN (the workflow declares issues: write)"
    )

#!/usr/bin/env python3
"""Parity gate for the justifications date-window slider.

The slider re-aggregates a narrowed window in the browser from
docs/js/justifications_agg.js. That file is a hand-maintained port of
scripts/build_justifications.py's per-agency aggregation; if the two
drift, the page silently shows numbers that disagree with the canonical
build. scripts/verify_justifications_agg.js asserts they agree at the
full window for every own-audit agency. This test just runs it under
pytest so `make test` / CI catch drift.

Skipped when Node is unavailable (the aggregation is JS); CI installs
Node so the gate runs there.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
VERIFIER = ROOT / "scripts" / "verify_justifications_agg.js"
JUST = ROOT / "docs" / "data" / "justifications.json"
AUDIT_DIR = ROOT / "docs" / "data" / "audit"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_aggregation_matches_build():
    assert JUST.exists(), "run build_justifications.py first (make build)"
    assert AUDIT_DIR.exists(), "run build_audit_log.py first (make build)"
    result = subprocess.run(
        ["node", str(VERIFIER)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    # On failure the verifier prints a per-field breakdown to stderr.
    assert result.returncode == 0, (
        "justifications_agg.js diverged from build_justifications.py:\n"
        + result.stdout
        + result.stderr
    )

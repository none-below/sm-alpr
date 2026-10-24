# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below

"""Pin scripts/report_parse_errors.sh, the rolling parser-issue reporter.

Its inline predecessors in refresh-flock-data.yml and probe-slugs.yml:
- commented on the newest open issue labeled `parser`, so a human-filed parser
  bug (#645) received 420 bot comments meant for the rolling issue (#527);
- treated a failed `gh issue list` as "no issue" and minted a duplicate;
- hashed the capture date, so an unfixed breakage looked new every UTC day;
- compared only against the last comment, so alternating error sets reposted;
- in probe-slugs.yml, had no dedupe at all.

The mock `gh` returns raw API JSON and leaves all jq processing to the script,
so it can't normalize away the behavior under test.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SCRIPT = REPO / "scripts" / "report_parse_errors.sh"
WORKFLOWS = REPO / ".github" / "workflows"
TITLE = "Parser tripped on a new Flock portal variant"

MOCK_GH = r'''#!/usr/bin/env python3
import json, os, shutil, sys
state = json.load(open(os.environ["GH_STATE"]))
rec = os.environ["REC"]
args = sys.argv[1:]

def record(action, issue=None):
    body_file = args[args.index("--body-file") + 1]
    entry = {"action": action, "issue": issue, "args": args,
             "body": open(body_file).read()}
    with open(os.path.join(rec, "calls.jsonl"), "a") as f:
        f.write(json.dumps(entry) + "\n")

if args[:2] == ["issue", "list"]:
    if state.get("list_fail"):
        sys.stderr.write("HTTP 503: Service Unavailable\n")
        sys.exit(1)
    print(json.dumps(state.get("issues", [])))
elif args[:2] == ["issue", "comment"]:
    record("comment", args[2])
elif args[:2] == ["issue", "create"]:
    record("create")
elif args[0] == "api":
    if state.get("read_fail"):
        sys.stderr.write("HTTP 502: Bad Gateway\n")
        sys.exit(1)
    path = [a for a in args[1:] if not a.startswith("--")][0]
    number = path.split("/issues/")[1].split("/")[0]
    if path.endswith("/comments"):
        assert "--paginate" in args, "comments must be paginated"
        # `gh api --paginate` writes each page's JSON array back to back.
        pages = state.get("comment_pages", {}).get(number, [[]])
        sys.stdout.write("".join(json.dumps(p) for p in pages))
    else:
        body = state.get("issue_bodies", {}).get(number, "")
        sys.stdout.write(json.dumps({"number": int(number), "body": body}))
else:
    sys.exit(f"mock gh: unexpected call {args}")
'''

ERRORS_A = (
    "PARSE_ERROR: hollister-ca-pd 2026-09-17 :: hollister-ca-pd 2026-09-17: "
    "bold headings not in _HEADING_MAP: ['Suspect Identified']\n"
)
ERRORS_B = (
    "PARSE_ERROR: murfreesboro-tn-pd 2026-09-24 :: murfreesboro-tn-pd "
    "2026-09-24: bold headings not in _HEADING_MAP: ['LPR Data Retention "
    "Length']\n"
)

ROLLING = {"number": 527, "title": TITLE}
HUMAN = {"number": 645,
         "title": "Parser comma-splits multi-part Flock account names"}


@pytest.fixture(scope="module", autouse=True)
def _needs_tools():
    for tool in ("bash", "jq", "sha256sum"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not available")


def _report(tmp_path, log_text, state):
    """Run the script once; return the gh write calls it made."""
    run_dir = tmp_path / f"run{len(list(tmp_path.glob('run*')))}"
    (run_dir / "bin").mkdir(parents=True)
    (run_dir / "rec").mkdir()
    gh = run_dir / "bin" / "gh"
    gh.write_text(MOCK_GH)
    gh.chmod(0o755)
    (run_dir / "state.json").write_text(json.dumps(state))
    (run_dir / "crawl.log").write_text(
        "Crawling 3 agencies...\n" + log_text + "Done.\n")
    (run_dir / "intro.md").write_text("Parser tripped on one or more portals.\n")
    env = {
        **os.environ,
        "PATH": f"{run_dir / 'bin'}:{os.environ['PATH']}",
        "GH_STATE": str(run_dir / "state.json"),
        "REC": str(run_dir / "rec"),
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "none-below/sm-alpr",
        "GITHUB_RUN_ID": "123",
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT), str(run_dir / "crawl.log"),
         str(run_dir / "intro.md")],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    calls = run_dir / "rec" / "calls.jsonl"
    if not calls.exists():
        return []
    return [json.loads(line) for line in calls.read_text().splitlines()]


def _on_rolling(comment_pages, body=""):
    return {"issues": [ROLLING], "issue_bodies": {"527": body},
            "comment_pages": {"527": comment_pages}}


def _comments(*bodies):
    return [{"body": b} for b in bodies]


def test_newer_human_parser_issue_is_not_hijacked(tmp_path):
    # `gh issue list` returns newest first; the human issue is newer.
    state = {"issues": [HUMAN, ROLLING], "comment_pages": {"527": [[]]}}
    calls = _report(tmp_path, ERRORS_A, state)
    assert [(c["action"], c["issue"]) for c in calls] == [("comment", "527")]


def test_creates_rolling_issue_when_only_human_issues_are_open(tmp_path):
    calls = _report(tmp_path, ERRORS_A, {"issues": [HUMAN]})
    assert [c["action"] for c in calls] == ["create"]
    args = calls[0]["args"]
    assert args[args.index("--title") + 1] == TITLE
    assert args[args.index("--label") + 1] == "parser"


def test_creates_rolling_issue_when_none_is_open(tmp_path):
    calls = _report(tmp_path, ERRORS_A, {"issues": []})
    assert [c["action"] for c in calls] == ["create"]


def test_failed_issue_lookup_posts_nothing(tmp_path):
    assert _report(tmp_path, ERRORS_A, {"list_fail": True}) == []


def test_failed_issue_read_posts_nothing(tmp_path):
    state = {**_on_rolling([[]]), "read_fail": True}
    assert _report(tmp_path, ERRORS_A, state) == []


def test_no_parse_errors_makes_no_calls(tmp_path):
    assert _report(tmp_path, "", {"list_fail": True}) == []


def test_new_errors_are_posted_with_intro_errors_and_run_link(tmp_path):
    calls = _report(tmp_path, ERRORS_A, _on_rolling([[]]))
    assert [(c["action"], c["issue"]) for c in calls] == [("comment", "527")]
    body = calls[0]["body"]
    assert body.startswith("Parser tripped on one or more portals.")
    assert ERRORS_A.strip() in body  # dates kept in the posted text
    assert "Run: https://github.com/none-below/sm-alpr/actions/runs/123" in body
    assert "<!-- parse-errors-hash: " in body


def test_identical_rerun_is_not_reposted(tmp_path):
    first = _report(tmp_path, ERRORS_A, _on_rolling([[]]))[0]["body"]
    assert _report(tmp_path, ERRORS_A, _on_rolling([_comments(first)])) == []


def test_same_breakage_on_a_later_capture_date_is_not_reposted(tmp_path):
    first = _report(tmp_path, ERRORS_A, _on_rolling([[]]))[0]["body"]
    next_day = ERRORS_A.replace("2026-09-17", "2026-09-18")
    assert _report(tmp_path, next_day, _on_rolling([_comments(first)])) == []


def test_errors_reported_before_the_last_comment_are_not_reposted(tmp_path):
    a = _report(tmp_path, ERRORS_A, _on_rolling([[]]))[0]["body"]
    b = _report(tmp_path, ERRORS_B, _on_rolling([[]]))[0]["body"]
    # A on the first page, B as the newest comment on the second.
    pages = [_comments(a, "unrelated human comment"), _comments(b)]
    assert _report(tmp_path, ERRORS_A, _on_rolling(pages)) == []


def test_errors_reported_in_issue_body_are_not_reposted(tmp_path):
    created = _report(tmp_path, ERRORS_A, {"issues": []})[0]["body"]
    assert _report(tmp_path, ERRORS_A, _on_rolling([[]], body=created)) == []


def test_changed_error_set_is_posted(tmp_path):
    a = _report(tmp_path, ERRORS_A, _on_rolling([[]]))[0]["body"]
    calls = _report(tmp_path, ERRORS_A + ERRORS_B, _on_rolling([_comments(a)]))
    assert [(c["action"], c["issue"]) for c in calls] == [("comment", "527")]


@pytest.mark.parametrize("workflow,log", [
    ("refresh-flock-data.yml", "/tmp/crawl.log"),
    ("probe-slugs.yml", "/tmp/probe.log"),
])
def test_workflows_report_through_the_shared_script(workflow, log):
    text = (WORKFLOWS / workflow).read_text()
    start = text.index("- name: Surface parser failures as an issue")
    end = text.index("\n      - name: ", start + 1)
    step = text[start:end]
    assert f"bash scripts/report_parse_errors.sh {log} " in step
    assert "gh issue" not in step, (
        "issue selection and dedupe belong in report_parse_errors.sh")

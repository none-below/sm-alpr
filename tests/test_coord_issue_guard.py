# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below

"""Pin the "only comment when the list changed" guard on the geocoding issue.

Issue #673 took 2,500 comments in 28 days and hit GitHub's hard comment cap,
which then failed the step that ran *before* `Commit and push` — so a week of
Flock refreshes were crawled and thrown away.

The guard was never actually working. It compared

    A = gh issue view N --json body -q .body | tr -d '\\r'
    B = the freshly built body file

but `-q .body` runs the stored body through `jq -r`, which appends its own
newline to a body GitHub already stored with one. A is therefore always exactly
one blank line longer than B, `diff` always reported "changed", and the rendered
changelog came out EMPTY because the sole differing line is a bare "-" that
`grep -E '^[-+][^-+]'` filters out. 1,643 of #673's 2,500 comments (66%) were
those empty-diff no-ops.

These tests execute the REAL guard sliced out of each workflow against a mocked
`gh`, so they fail if someone reintroduces an un-normalized comparison. The
mock must not normalize either — emulating `jq -r` sloppily silently applies the
very fix under test and every scenario passes against the broken code.
"""

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
WORKFLOWS = REPO / ".github" / "workflows"

# (workflow file, step name) for every place that maintains the geocoding issue.
GUARD_SITES = [
    ("refresh-flock-data.yml", "Check recipient coordinates"),
    ("ci.yml", "Open issue for missing coordinates"),
]

BODY_TMPL = (
    "The following agencies receive data from public agencies but have no "
    "lat/lng in the registry:\n\n```\n{block}```\n\nAdd coordinates to "
    "`assets/agency_registry.json` so they appear on the sharing map."
)

LIST_A = (
    "2 recipient(s) missing coordinates:\n\n"
    "  Alpha PD  <- Beta PD\n"
    "  Gamma PD  <- Delta PD\n"
)
LIST_B = (
    "3 recipient(s) missing coordinates:\n\n"
    "  Alpha PD  <- Beta PD\n"
    "  Gamma PD  <- Delta PD\n"
    "  Epsilon PD  <- Zeta PD\n"
)

# `$2` is the gh subcommand ("view"/"edit"/"comment") for `gh issue <sub> ...`.
MOCK_GH = """\
#!/usr/bin/env bash
case "$2" in
  view)
    # Simulate a transient gh failure (502, secondary rate limit, ...):
    # no output, non-zero exit.
    if [ -n "${GH_VIEW_FAIL:-}" ]; then exit 1; fi
    # Exactly what `--json body -q .body` emits: the stored body verbatim,
    # then the newline `jq -r` appends. Deliberately does NOT normalize —
    # normalizing here would apply the fix under test and mask a regression.
    cat "$STORED_BODY"; printf '\\n'
    ;;
  edit)
    shift; while [ "$1" != "--body-file" ]; do shift; done
    cp "$2" "$REC/edited_body.md"; echo EDIT >> "$REC/actions"
    ;;
  comment)
    shift; while [ "$1" != "--body-file" ]; do shift; done
    cp "$2" "$REC/comment.md"; echo COMMENT >> "$REC/actions"
    ;;
esac
exit 0
"""


def _extract_guard(workflow, step_name):
    """Slice the guard shell out of a workflow's `run:` block.

    Text-based on purpose: pyyaml is not a project dependency. Starts at the
    `gh issue view "$EXISTING"` line and runs to the end of that step. Raises
    if the anchor is missing, so a rename fails the test loudly instead of
    silently testing nothing.
    """
    text = (WORKFLOWS / workflow).read_text()
    step_at = text.find(f"- name: {step_name}")
    assert step_at != -1, f"step {step_name!r} not found in {workflow}"
    tail = text[step_at:]
    # The step ends where the next `- name:` at the same indentation begins.
    nxt = re.search(r"\n( *)- name: ", tail[1:])
    if nxt:
        tail = tail[: 1 + nxt.start()]
    anchor = tail.find('gh issue view "$EXISTING"')
    assert anchor != -1, (
        f"{workflow} :: {step_name} no longer contains the "
        'gh issue view "$EXISTING" anchor — update this test'
    )
    line_start = tail.rfind("\n", 0, anchor) + 1
    lines = tail[line_start:].split("\n")
    indent = len(lines[0]) - len(lines[0].lstrip())
    # Stop at the first line shallower than the anchor. In ci.yml the guard
    # lives inside `if [ -n "$EXISTING" ]; then … else … fi`, so running to the
    # end of the step would capture a dangling `else`.
    out = [lines[0]]
    for line in lines[1:]:
        if line.strip() and (len(line) - len(line.lstrip())) < indent:
            break
        out.append(line)
    return textwrap.dedent("\n".join(out))


def _run_guard(guard, tmp_path, stored_body, fresh_body, view_fails=False):
    """Run the guard with a mocked gh; return (actions, comment_body)."""
    rec = tmp_path / "rec"
    rec.mkdir()
    binp = tmp_path / "bin"
    binp.mkdir()
    (binp / "gh").write_text(MOCK_GH)
    (binp / "gh").chmod(0o755)

    (tmp_path / "stored_body").write_text(stored_body)
    # Redirect the workflow's hardcoded /tmp paths into tmp_path so concurrent
    # runs cannot collide and the test leaves nothing behind.
    guard = guard.replace("/tmp/coord-issue", str(tmp_path / "coord-issue"))
    (tmp_path / "coord-issue-body.md").write_text(fresh_body)

    env = {
        **os.environ,
        "PATH": f"{binp}:{os.environ['PATH']}",
        "STORED_BODY": str(tmp_path / "stored_body"),
        "REC": str(rec),
    }
    if view_fails:
        env["GH_VIEW_FAIL"] = "1"
    proc = subprocess.run(
        ["bash", "-c", 'EXISTING=999\n' + guard],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, f"guard exited {proc.returncode}: {proc.stderr}"
    actions_file = rec / "actions"
    actions = actions_file.read_text().split() if actions_file.exists() else []
    comment = rec / "comment.md"
    return actions, (comment.read_text() if comment.exists() else "")


@pytest.fixture(scope="module", autouse=True)
def _needs_bash():
    if shutil.which("bash") is None:
        pytest.skip("bash not available")


@pytest.mark.parametrize("workflow,step", GUARD_SITES)
def test_identical_list_posts_no_comment(workflow, step, tmp_path):
    """The #673 flood case: same recipients in, nothing written out.

    The stored body ends with one newline (that is what `gh issue edit
    --body-file` uploaded) and the mock adds jq's newline on read, so the two
    sides differ by a trailing blank line and nothing else.
    """
    guard = _extract_guard(workflow, step)
    stored = BODY_TMPL.format(block=LIST_A) + "\n"
    fresh = BODY_TMPL.format(block=LIST_A) + "\n"
    actions, comment = _run_guard(guard, tmp_path, stored, fresh)
    assert "COMMENT" not in actions, (
        f"{workflow} commented on an unchanged list — the flood is back. "
        f"comment body: {comment!r}"
    )


@pytest.mark.parametrize("workflow,step", GUARD_SITES)
def test_trailing_blank_lines_post_no_comment(workflow, step, tmp_path):
    """Extra trailing blank lines on the stored body are still not a change."""
    guard = _extract_guard(workflow, step)
    stored = BODY_TMPL.format(block=LIST_A) + "\n\n\n"
    fresh = BODY_TMPL.format(block=LIST_A) + "\n"
    actions, comment = _run_guard(guard, tmp_path, stored, fresh)
    assert "COMMENT" not in actions, f"commented on whitespace only: {comment!r}"


@pytest.mark.parametrize("workflow,step", GUARD_SITES)
@pytest.mark.parametrize("stored_list,fresh_list", [(LIST_A, LIST_B), (LIST_B, LIST_A)])
def test_real_change_still_comments(workflow, step, stored_list, fresh_list, tmp_path):
    """A recipient added or removed must still update the body AND comment."""
    guard = _extract_guard(workflow, step)
    stored = BODY_TMPL.format(block=stored_list) + "\n"
    fresh = BODY_TMPL.format(block=fresh_list) + "\n"
    actions, comment = _run_guard(guard, tmp_path, stored, fresh)
    assert "EDIT" in actions, "body must be refreshed on a real change"
    assert "COMMENT" in actions, "a real change must still be announced"
    assert "Epsilon PD" in comment, f"changelog lost the changed line: {comment!r}"


@pytest.mark.parametrize("workflow,step", GUARD_SITES)
def test_carriage_return_in_a_name_is_not_a_change(workflow, step, tmp_path):
    """A stray \\r must not pin the guard permanently open.

    `tr -d '\\r'` used to be applied only to the FETCHED side. Agency display
    names come from raw scraped portal text, so a CR in a name reaches the
    freshly built body, survives there, and is stripped only from the copy
    read back from GitHub — leaving the two permanently unequal. Both sides
    carry the CR here, which is the real shape: the body was uploaded from a
    file that contained it.
    """
    guard = _extract_guard(workflow, step)
    with_cr = LIST_A.replace("Alpha PD", "Alpha\r PD")
    stored = BODY_TMPL.format(block=with_cr) + "\n"
    fresh = BODY_TMPL.format(block=with_cr) + "\n"
    actions, comment = _run_guard(guard, tmp_path, stored, fresh)
    assert "COMMENT" not in actions, f"a carriage return read as a change: {comment!r}"


@pytest.mark.parametrize("workflow,step", GUARD_SITES)
def test_unreadable_body_does_not_post_a_full_list_change(workflow, step, tmp_path):
    """A failed body fetch must skip, not manufacture a whole-list change.

    The normalization turns a 0-byte fetch into a lone newline, so without a
    fail-closed check `diff` reports every recipient as an insertion and the
    step posts the entire list as if it were new.
    """
    guard = _extract_guard(workflow, step)
    body = BODY_TMPL.format(block=LIST_A) + "\n"
    actions, comment = _run_guard(guard, tmp_path, body, body, view_fails=True)
    assert actions == [], (
        f"wrote to the issue despite an unreadable body: {actions} {comment!r}"
    )


@pytest.mark.parametrize("workflow,step", GUARD_SITES)
def test_changed_line_starting_with_a_dash_survives_rendering(workflow, step, tmp_path):
    """A content line whose own first character is '-' must reach the comment.

    Such a line renders as '+-…' in the diff, which a `^[-+][^-+]` pattern
    discards — guard 1 would say "changed", guard 2 would find nothing, and a
    real change would be swallowed with the body silently rewritten.
    """
    guard = _extract_guard(workflow, step)
    odd = LIST_A + "-unindented row  <- Somewhere PD\n"
    stored = BODY_TMPL.format(block=LIST_A) + "\n"
    fresh = BODY_TMPL.format(block=odd) + "\n"
    actions, comment = _run_guard(guard, tmp_path, stored, fresh)
    assert "COMMENT" in actions, "a real change was swallowed"
    assert "-unindented row" in comment, f"changelog dropped the line: {comment!r}"


@pytest.mark.parametrize("workflow,step", GUARD_SITES)
def test_posted_comment_is_never_an_empty_diff(workflow, step, tmp_path):
    """Whatever else happens, never post a changelog with an empty diff block.

    This is the second, independent guard: even if the body comparison were to
    report a spurious change again, a comment carrying no information must not
    be posted.
    """
    guard = _extract_guard(workflow, step)
    for stored_list, fresh_list in [(LIST_A, LIST_A), (LIST_A, LIST_B), (LIST_B, LIST_A)]:
        sub = tmp_path / f"{stored_list[0]}{fresh_list[0]}{len(fresh_list)}"
        sub.mkdir()
        stored = BODY_TMPL.format(block=stored_list) + "\n"
        fresh = BODY_TMPL.format(block=fresh_list) + "\n"
        _, comment = _run_guard(guard, sub, stored, fresh)
        assert "```diff\n```" not in comment, (
            f"{workflow} posted an empty diff block: {comment!r}"
        )

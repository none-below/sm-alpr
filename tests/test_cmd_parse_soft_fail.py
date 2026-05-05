"""cmd_parse must soft-fail per file: one slug whose .txt trips the
parser (e.g. unrecognized bold heading after a Flock layout change)
must not block the other slugs in the same batch from producing JSON.

Mirrors the soft-fail pattern already in cmd_crawl (PARSE_ERROR line
+ keep going). The PARSE_ERROR marker is grepped by the workflow's
issue-surfacing step.
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import flock_transparency
from flock_transparency import cmd_parse


def test_cmd_parse_soft_fails_per_slug(tmp_path, capsys, monkeypatch):
    good = tmp_path / "boomtown-ca-pd"
    good.mkdir()
    (good / "2026-05-05.txt").write_text("good txt body")

    bad = tmp_path / "sometown-ca-pd"
    bad.mkdir()
    (bad / "2026-05-05.txt").write_text("bad txt body")

    # Stub parse_portal_text so the test pins the cmd_parse loop's
    # behavior, not the parser's heading map. The real parser is
    # exercised by tests/test_flock_transparency_headings.py.
    def fake_parse(raw_text, slug, datestamp, bold_headings=None):
        if slug == "sometown-ca-pd":
            raise ValueError(f"{slug} {datestamp}: simulated heading miss")
        return {"crawled_slug": slug, "archived_date": datestamp,
                "camera_count": 12, "sharing_outbound": []}

    monkeypatch.setattr(flock_transparency, "parse_portal_text", fake_parse)

    args = types.SimpleNamespace(data_dir=tmp_path, slug=None, force=False)
    cmd_parse(args)

    assert (good / "2026-05-05.json").exists(), (
        "good slug must still produce JSON when a sibling slug fails"
    )
    assert not (bad / "2026-05-05.json").exists(), (
        "failed slug must NOT produce a stub JSON — that pollutes "
        "downstream consumers (build_history, build_map)"
    )

    out = capsys.readouterr().out
    assert "PARSE_ERROR: sometown-ca-pd 2026-05-05" in out, (
        "PARSE_ERROR marker is grepped by the workflow's issue step"
    )
    assert "Parsed 1 file(s). 1 failed" in out

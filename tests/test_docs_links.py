"""GitHub Pages publishes only docs/ (deploy.yml uploads `path: docs`), so a
page link that climbs out with "../" resolves above /sm-alpr/ and 404s on the
live site even though it works from a local checkout."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
PARENT_LINK = re.compile(r"""(?:href|src)=["']?\.\./""")


def test_no_links_escape_published_root():
    pages = sorted(DOCS.glob("*.html")) + sorted((DOCS / "js").glob("*.js"))
    assert pages, "no docs pages found"
    hits = []
    for path in pages:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if PARENT_LINK.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not hits, "links outside the published docs/ root:\n" + "\n".join(hits)

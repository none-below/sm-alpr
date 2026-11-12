# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""Header repair for agency-edited audit sheets: which label goes with which data column.

Agencies delete columns in Excel before producing, and the header row does not always survive that edit intact.
Two failure modes, detected by type-checking sampled data against each label's expected kind (int / date / text),
never by trusting position alone: a *phantom* label (a label with no data column under it, which would silently
shift every later field one to the left) and an *unlabeled* column (data present, label blank, which would
silently drop the column). Phantom labels are dropped; unlabeled columns are kept as `column_<N>` (N = 1-based
data column). Ambiguity is a hard error rather than a guess. A well-formed sheet aligns straight through, so
this changes nothing for the ordinary case.

Lifted from scripts/xlsx_to_audit_ndjson.py (the committed-NDJSON converter), whose releases are retired by
exclusions.json; the loaders here (muckrock_ingest, portal_ingest) call `repair_header` on the staged CSV, and
the extractor takes it over at assembly (PLAN-audit-db.md). Found the hard way: a 2025 org-audit sheet whose
released header carried a "Total Devices Searched" label over no data loaded shifted on the portal path and
right on the converter path (reports/los-altos-twin-loads-2026-11-12.md in the data repo).
"""
import re

# Expected value kind per known canonical column, used to verify header/data alignment.
EXPECTED_KIND = {
    "Name": "text", "Org Name": "text", "Total Networks Searched": "int",
    "Total Devices Searched": "int", "Time Frame": "date", "License Plate": "text",
    "Reason": "text", "Case #": "text", "Filters": "text", "Search Time": "date",
    "Search Type": "text", "Text Prompt": "text", "Moderation": "text",
}
DATEISH = re.compile(r"\d{2}/\d{2}/\d{4},")
INTISH = re.compile(r"^\d+$")
SAMPLE = 500
MAX_REPAIRS = 2   # phantom labels + unlabeled data columns a sheet may need before it is refused as ambiguous


def cell_kind(v):
    """Coarse type of one cell, for header/data alignment checks."""
    if v is None:
        return "none"
    s = str(v).strip()
    if s == "":
        return "none"
    if DATEISH.search(s):
        return "date"
    if INTISH.match(s):
        return "int"
    return "text"


def column_kinds(sample, ncol):
    """Dominant non-empty kind of each data column across sampled rows."""
    out = []
    for c in range(ncol):
        counts = {}
        for r in sample:
            k = cell_kind(r[c]) if c < len(r) else "none"
            if k != "none":
                counts[k] = counts.get(k, 0) + 1
        out.append(max(counts, key=counts.get) if counts else "none")
    return out


def pair_score(h, kind):
    """Agreement between one header label and its candidate data column."""
    want = EXPECTED_KIND.get(h) if h else None
    if want is None or kind == "none":
        return 0.0          # blank/unknown label, or an all-empty column
    return 1.0 if want == kind else -1.0


def align(hdr, kinds):
    """Map header positions to data positions, tolerating hand-edited headers.

    Returns (mapping, phantom) where mapping[data_index] = header label (or None for an unlabeled column) and
    phantom is the list of labels that matched no data column. Order is preserved on both sides; the assignment
    maximising total type agreement is chosen by dynamic programming, so a label the agency left behind after
    deleting its column is dropped rather than shifting every later field by one. Both kinds of gap are
    penalised, so a well-formed sheet always aligns straight through.
    """
    GAP = -0.25
    # Two labels of the same expected kind (the two "Total ... Searched" counts) score identically against one
    # surviving column, so ties are broken toward binding the earlier label: a deleted column's leftover label is
    # the trailing one. EPS is far below any real agreement score, so it only ever decides exact ties.
    EPS = 1e-6
    n, m = len(hdr), len(kinds)
    NEG = float("-inf")
    dp = [[NEG] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            if dp[i][j] == NEG:
                continue
            if i < n and j < m:                      # label takes this column
                s = dp[i][j] + pair_score(hdr[i], kinds[j]) + EPS * (n - i)
                if s > dp[i + 1][j + 1]:
                    dp[i + 1][j + 1], back[i + 1][j + 1] = s, ("take", i, j)
            if i < n:                                # label with no column
                s = dp[i][j] + GAP
                if s > dp[i + 1][j]:
                    dp[i + 1][j], back[i + 1][j] = s, ("phantom", i, j)
            if j < m:                                # column with no label
                s = dp[i][j] + GAP
                if s > dp[i][j + 1]:
                    dp[i][j + 1], back[i][j + 1] = s, ("unlabeled", i, j)
    mapping, phantom = {}, []
    i, j = n, m
    while back[i][j] is not None:
        kind, pi, pj = back[i][j]
        if kind == "take":
            mapping[pj] = hdr[pi] or None
        elif kind == "phantom":
            if hdr[pi]:
                phantom.append(hdr[pi])
        else:
            mapping[pj] = None
        i, j = pi, pj
    phantom.reverse()
    return mapping, phantom


def verify(mapping, kinds):
    """Labels that still contradict their data column after alignment."""
    return [f"{h}@{d}:want={EXPECTED_KIND[h]},got={kinds[d]}"
            for d, h in mapping.items()
            if h in EXPECTED_KIND and kinds[d] != "none"
            and EXPECTED_KIND[h] != kinds[d]]


def repair_header(header, sample):
    """Per-data-column labels for a staged sheet: (labels, phantom, unlabeled, note).

    `header` is the canonical header row; `sample` is a list of data rows (lists of cell text) in column order.
    `labels` has one entry per header position: the label the data column at that position belongs to, or
    `column_<N>` for an unlabeled one. Cells beyond the header's width are the loader's business (truth drops
    them; the extractor keeps them as overflow), not this function's. When nothing needs repair, labels == header
    and note is None. Raises ValueError when a label still contradicts its column after alignment, or when the
    sheet would need more repairs than a hand edit explains (the ambiguous cases the converter refuses to guess at)."""
    ncol = len(header)
    hdr = [h or "" for h in header]
    kinds = column_kinds(sample, ncol)
    mapping, phantom = align(hdr, kinds)
    bad = verify(mapping, kinds)
    if bad:
        raise ValueError("header/data alignment ambiguous: " + "; ".join(bad))
    n_unlabeled = sum(1 for d in range(ncol) if mapping.get(d) is None and kinds[d] != "none")
    if len(phantom) + n_unlabeled > MAX_REPAIRS:
        # an agency deletes a column or two; a sheet that needs more than that repaired is not one this understands
        raise ValueError(f"header/data alignment ambiguous: {len(phantom)} phantom labels {phantom} and "
                         f"{n_unlabeled} unlabeled data columns against a {len(hdr)}-label header")
    labels, unlabeled = [], []
    for d in range(ncol):
        h = mapping.get(d)
        if h is None:
            labels.append(f"column_{d + 1}")
            if kinds[d] != "none":
                unlabeled.append(d + 1)
        else:
            labels.append(h)
    if not phantom and not unlabeled:
        return labels, [], [], None
    note = "header repaired by type check against the data (header_align.py): " + "; ".join(
        ([f"phantom labels dropped: {phantom}"] if phantom else [])
        + ([f"unlabeled data columns kept as column_<N> (1-based): {unlabeled}"] if unlabeled else []))
    return labels, phantom, unlabeled, note


def sample_rows(path, n=SAMPLE):
    """The first n data rows of a staged CSV (gzip, first column __src_row, header row first), as cell-text lists."""
    import csv
    import gzip
    out = []
    with gzip.open(path, "rt", newline="", encoding="utf-8") as fh:
        rd = csv.reader(fh)
        next(rd, None)   # the header row
        for row in rd:
            out.append(row[1:])
            if len(out) >= n:
                break
    return out

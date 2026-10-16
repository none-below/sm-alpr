"""Which tables in a released file are Flock audit logs: one entry per CSV, workbook sheet or zip member (one level of
zip), with the kind the MuckRock catalog uses (network_audit, search_audit_own, event_log), decided by content: the
header row (the first row with >= 3 alphabetic cells, as staging reads it) and, for audits, how many organizations
searched (a network audit lists other agencies' searches; an own-search audit lists one organization's).

Used for releases that have no MuckRock-style catalog: the public-records-portal harvest (portal_ingest.py) and files
fetched after the MuckRock catalog was built (muckrock_ingest.py profile-resweep).
"""
import collections
import csv
import io
import zipfile
from pathlib import Path

from muckrock_ingest import _is_header, canonical, render, sniff

TABULAR = {".csv", ".xlsx", ".xlsm", ".xls", ".xlsb"}
SCAN_ROWS = 2000   # rows read past the header to count organizations


def _sheets(raw, ext):
    """(sheet name or None, row iterator) per table in one file's bytes."""
    if ext == ".csv":
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
        yield None, csv.reader(io.StringIO(text, newline=""))
    elif ext in (".xlsx", ".xlsm", ".xls", ".xlsb"):
        from python_calamine import CalamineWorkbook
        wb = CalamineWorkbook.from_filelike(io.BytesIO(raw))
        for name in wb.sheet_names:
            yield name, wb.get_sheet_by_name(name).to_python()


def _kind(header, orgs, name):
    if {"Event Type", "Entity Type"} <= set(header):
        return "event_log"
    if not ({"Org Name", "Search Time"} <= set(header) or {"Search Time", "Total Networks Searched"} <= set(header)):
        return None
    low = name.lower()
    if "network" in low:
        return "network_audit"
    if len(orgs) > 1:
        return "network_audit"
    return "search_audit_own"


def profile_bytes(raw, name):
    """Audit tables in one file: [{member, sheet, kind, headers, n_orgs, top_org}] (member set for a zip member)."""
    ext = sniff(raw[:8], Path(name).suffix.lower())
    out = []
    if ext == ".zip" or raw[:4] == b"PK\x03\x04" and Path(name).suffix.lower() == ".zip":
        z = zipfile.ZipFile(io.BytesIO(raw))
        for m in z.namelist():
            if m.endswith("/") or Path(m).suffix.lower() not in TABULAR:
                continue
            for p in profile_bytes(z.read(m), m):
                out.append({**p, "member": m})
        return out
    if ext not in TABULAR:
        return out
    for sheet, rows in _sheets(raw, ext):
        header, orgs, seen = None, collections.Counter(), 0
        for r in rows:
            vals = render(r)
            if header is None:
                if _is_header(vals):
                    header = canonical(vals)
                    if "Org Name" not in header:
                        break
                continue
            oi = header.index("Org Name")
            if oi < len(vals) and vals[oi].strip():
                orgs[vals[oi].strip()] += 1
            seen += 1
            if seen >= SCAN_ROWS:
                break
        if header is None:
            continue
        kind = _kind(header, orgs, f"{name} {sheet or ''}")
        if kind:
            out.append({"member": None, "sheet": sheet, "kind": kind, "headers": header, "n_orgs": len(orgs),
                        "top_org": orgs.most_common(1)[0][0] if orgs else None})
    return out


def profile_file(path, name=None):
    try:
        return profile_bytes(Path(path).read_bytes(), name or Path(path).name), None
    except BaseException as ex:  # noqa: BLE001  calamine raises a pyo3 PanicException (BaseException) on some workbooks
        if isinstance(ex, (KeyboardInterrupt, SystemExit)):
            raise
        return [], f"{type(ex).__name__}: {str(ex)[:120]}"

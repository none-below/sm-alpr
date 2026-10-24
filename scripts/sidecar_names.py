"""OCR sidecar naming: which .txt files are sidecars scripts/ocr_sidecar.py wrote.

A sidecar sits next to its base file as <base name>.<first 8 hex of the base
file's MD5>.txt, for the extensions ocr_sidecar OCRs. Kept free of OCR imports
so the uploader and build scripts can use the rules cheaply.
"""
import hashlib
import re
from pathlib import Path

SUPPORTED_EXTENSIONS = {".pdf", ".doc", ".docx", ".png", ".jpg", ".jpeg"}


def file_hash(file_path):
    """Return first 8 hex chars of the file's MD5."""
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def sidecar_name(name, md5_hex):
    """Sidecar filename for a file called `name` whose MD5 hex digest is `md5_hex`."""
    return f"{name}.{md5_hex[:8]}.txt"


def sidecar_base_name(name):
    """For a name shaped like a sidecar, <base>.<8 hex>.txt where <base> has an
    extension this script OCRs, return <base>; else None. Dated names like
    log.20260918.txt don't qualify."""
    parts = name.rsplit(".", 2)
    if (len(parts) == 3 and parts[2] == "txt" and re.fullmatch(r"[0-9a-f]{8}", parts[1])
            and Path(parts[0]).suffix.lower() in SUPPORTED_EXTENSIONS):
        return parts[0]
    return None


def is_sidecar(name, own_md5):
    """True for a sidecar this script wrote, whether current, stale (its base
    file changed) or orphaned (its base file is gone).

    pra_download renames same-named attachments to <stem>.<8 hex>.<ext> with
    the file's *own* MD5, so a released scan.pdf.txt can become
    scan.pdf.1a2b3c4d.txt. A sidecar carries its base file's MD5 instead, so
    the two are told apart by the file's own content.
    """
    return sidecar_base_name(name) is not None and name.rsplit(".", 2)[1] != own_md5[:8]


def remove_stale_sidecars(file_path, keep):
    """Delete this file's old sidecars (other than `keep`) and return them.

    Only this file's: a sidecar whose base is exactly this file's name (so
    foo.pdf never takes foo.pdf.pdf's, and names aren't used as glob patterns),
    and only what is_sidecar agrees is one (a released attachment that
    pra_download renamed to <name>.<its own MD5>.txt stays).
    """
    file_path = Path(file_path)
    removed = []
    for old in sorted(file_path.parent.iterdir()):
        if (old != keep and sidecar_base_name(old.name) == file_path.name and old.is_file()
                and is_sidecar(old.name, file_hash(old))):
            old.unlink()
            removed.append(old)
    return removed


def sidecar_path_for(file_path):
    """Return the hash-stamped sidecar path for a file."""
    file_path = Path(file_path)
    return file_path.parent / sidecar_name(file_path.name, file_hash(file_path))

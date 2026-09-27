"""Load the plate-token key into a DuckDB session (never into a database file).

Key source, in order: env PLATE_TOKEN_KEY (CI: GitHub secret), else ~/.config/sm-alpr/plate_token_key.
The key must be 32 random bytes as 64 hex chars (openssl rand -hex 32) -- never a passphrase.

plate_token(p) in derived.duckdb computes the same HMAC in SQL, reading the key file itself (any client, any
connection). In CI, run `python plate_key.py --install` to write the PLATE_TOKEN_KEY secret to the key file.
Same token in SQL, Python and CI:  token_py(p) == SELECT plate_token(p).

Key text is normalized the way the SQL reads it (every tab, CR, LF and space removed, so a trailing newline from
`openssl rand -hex 32 > file` is harmless), then must be exactly 64 hex chars; anything else is an error, never a
silently different key. The key file is kept 0600 and its directory 0700 (enforced on every load and install).

  python plate_key.py --check     validate the key file (format, permissions); prints status only, never the key
  python plate_key.py --install   CI: write env PLATE_TOKEN_KEY to the key file
"""
import hashlib
import hmac
import os
import re
import stat
import sys
from pathlib import Path

KEY_FILE = Path("~/.config/sm-alpr/plate_token_key").expanduser()
_WS = re.compile(r"[\t\r\n ]")   # what the SQL strips before validating (derived.duckdb plate_key_pads / plate_token)


def normalize_key(raw: str, where: str = "key") -> str:
    """Key text as the SQL uses it: whitespace removed, lower-cased (plate_pad lower-cases), 64 hex chars or error."""
    k = _WS.sub("", raw or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", k):
        raise SystemExit(f"plate token key ({where}) missing or not 64 hex chars after removing whitespace "
                         "(env PLATE_TOKEN_KEY or ~/.config/sm-alpr/plate_token_key; make one with: openssl rand -hex 32)")
    return k


def enforce_permissions() -> list[str]:
    """Key dir 0700, key file 0600 (tightened in place if looser). Returns what was changed."""
    fixed = []
    for p, mode in ((KEY_FILE.parent, 0o700), (KEY_FILE, 0o600)):
        if p.exists() and stat.S_IMODE(p.stat().st_mode) != mode:
            fixed.append(f"{p}: {stat.S_IMODE(p.stat().st_mode):04o} -> {mode:04o}")
            os.chmod(p, mode)
    return fixed


def _file_key() -> str | None:
    if not KEY_FILE.exists():
        return None
    for f in enforce_permissions():
        print(f"plate_key: tightened permissions {f}", file=sys.stderr)
    return normalize_key(KEY_FILE.read_text(), str(KEY_FILE))


def load_key() -> bytes:
    """The key bytes. If both env and file are set they must agree: SQL reads only the file, so a different env key
    would give Python tokens that differ from the SQL ones."""
    env = os.environ.get("PLATE_TOKEN_KEY")
    k_env = normalize_key(env, "env PLATE_TOKEN_KEY") if env else None
    k_file = _file_key()
    if k_env and k_file and k_env != k_file:
        raise SystemExit("env PLATE_TOKEN_KEY differs from the key file that SQL plate_token() reads; tokens would differ")
    k = k_env or k_file or normalize_key("", "none found")   # the last one raises
    return bytes.fromhex(k)


def install_from_env() -> None:
    """CI: write env PLATE_TOKEN_KEY (normalized) to the key file, file 0600 and directory 0700 even if they existed."""
    k = normalize_key(os.environ.get("PLATE_TOKEN_KEY", ""), "env PLATE_TOKEN_KEY")
    KEY_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(KEY_FILE.parent, 0o700)
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)   # O_CREAT's mode applies only to a new file
    with os.fdopen(fd, "w") as fh:
        fh.write(k)


def check() -> int:
    """Status only (never the key): 0 = file present, valid and private."""
    if not KEY_FILE.exists():
        print(f"no key file at {KEY_FILE}")
        return 1
    fixed = enforce_permissions()
    normalize_key(KEY_FILE.read_text(), str(KEY_FILE))
    print(f"key file OK: 64 hex chars, file 0600, directory 0700{' (tightened: ' + '; '.join(fixed) + ')' if fixed else ''}")
    return 0


def set_plate_key(con) -> None:  # legacy: session-variable pads (connection-local); SQL now reads the key file
    k = load_key().ljust(64, b"\0")
    ipad = bytes(b ^ 0x36 for b in k).hex()
    opad = bytes(b ^ 0x5C for b in k).hex()
    con.execute(f"SET VARIABLE pk_i = unhex('{ipad}')")
    con.execute(f"SET VARIABLE pk_o = unhex('{opad}')")


def token_py(plate: str) -> str | None:
    norm = re.sub(r"[^A-Za-z0-9]", "", plate or "").upper()   # = SQL plate_norm()
    if not norm:
        return None
    return "p1_" + hmac.new(load_key(), ("plate:v1:" + norm).encode(), hashlib.sha256).hexdigest()[:16]


if __name__ == "__main__":
    if "--install" in sys.argv:
        install_from_env()
    elif "--check" in sys.argv:
        sys.exit(check())
    else:
        print(__doc__)

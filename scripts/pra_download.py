#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Download attachments from the San Mateo public records portal (GovQA).

First-time setup (or when session expires):
  uv run playwright install chromium     # one time
  uv run python scripts/pra_download.py --login

In --login mode the script opens a Chromium window. Log in; when you're on
the support home page (the list of your requests), press Enter in the
terminal. The script saves your session cookies and the support home URL
(with the rotating `(S(...))` session token stripped so it survives session
rotation).

Regular use:
  uv run python scripts/pra_download.py W012297-030826     # one request
  uv run python scripts/pra_download.py --all              # every request folder
  uv run python scripts/pra_download.py                    # same as --all
  uv run python scripts/pra_download.py --active           # every folder except closed ones
  uv run python scripts/pra_download.py --discover         # stub new portal ids, then scrape all

After downloading, run OCR to refresh sidecars:
  uv run python scripts/ocr_sidecar.py --staged

Config lives at ~/.config/sm-alpr/pra_config.json.
Auth state lives at ~/.config/sm-alpr/pra_auth.json (not in repo).

Headless auto-login (so the script can run unattended):
  Local laptop — put credentials at ~/.config/sm-alpr/pra_credentials.json:
    {"username": "...", "password": "..."}
  Chmod it 600. Then:
    uv run python scripts/pra_download.py --auto-login
  GitHub Action / CI — set repo secrets PRA_USERNAME and PRA_PASSWORD and
  export them as env vars when running the script. The credentials file is
  optional when env vars are present.
  Regular runs will also auto-re-login if the saved session has expired
  and credentials are available. If the portal serves a CAPTCHA, auto-login
  will fail and you'll need to fall back to --login.
"""

import functools
print = functools.partial(print, flush=True)

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import fitz  # pymupdf, for PDF text fingerprinting

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page


VERBOSE = False
_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
_ANSI = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "cyan": "\033[36m",
}


def _c(text: str, *styles: str) -> str:
    if not _USE_COLOR:
        return text
    return "".join(_ANSI[s] for s in styles if s in _ANSI) + text + _ANSI["reset"]


def diag(msg: str) -> None:
    if VERBOSE:
        print(msg)


def warn(msg: str) -> None:
    print(_c(msg, "yellow"))


def err(msg: str) -> None:
    print(_c(msg, "red"))


# Per-file status glyphs/colors. "new" is green +, "updated" cyan ~,
# "unchanged" is dim = (shown only under --verbose).
_STATUS_STYLE = {
    "new":       ("+", ("green", "bold")),
    "updated":   ("~", ("cyan", "bold")),
    "unchanged": ("=", ("dim",)),
    "skipped":   ("-", ("dim",)),
    "failed":    ("x", ("red", "bold")),
}


def file_status(state: str, name: str, *, indent: str = "     ") -> None:
    glyph, styles = _STATUS_STYLE[state]
    if state == "unchanged" and not VERBOSE:
        return
    print(f"{indent}{_c(glyph, *styles)} {name}")


def pdf_text_fingerprint(pdf_bytes: bytes) -> str | None:
    """MD5 of the PDF's extracted text with whitespace normalized.
    Returns None if the bytes aren't a readable PDF. Lets us detect when a
    re-downloaded MH PDF has identical *content* despite differing metadata
    (CreationDate, ModDate, etc.) — avoids spamming git history with churn."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return None
    try:
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text() or "")
        text = "".join(text_parts)
    finally:
        doc.close()
    normalized = " ".join(text.split())
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


FILENAME_CD_RE = re.compile(
    r"filename[*]?=(?:UTF-8''|)[\"']?([^\"';]+)[\"']?",
    re.IGNORECASE,
)


def filename_from_response(resp, url: str) -> str | None:
    """Infer a filename from Content-Disposition, then URL query, then URL path."""
    cd = resp.headers.get("content-disposition", "")
    m = FILENAME_CD_RE.search(cd)
    if m:
        return m.group(1).strip()
    q = parse_qs(urlparse(url).query)
    for key in ("fileName", "filename", "name", "file"):
        if key in q and q[key]:
            return q[key][0]
    # Fall back to last path segment
    path = urlparse(url).path
    tail = path.rsplit("/", 1)[-1]
    if tail and "." in tail:
        return tail
    return None


def save_pdf_via_click(page: Page, element, *,
                      target_path: Path | None = None,
                      folder: Path | None = None,
                      fallback_name: str | None = None
                      ) -> tuple[Path, str] | None:
    """Click `element`; handle whichever of these the portal returns:
       (a) Content-Disposition: attachment -> Chromium download event
       (b) inline PDF in a new tab -> popup with URL we fetch via request API
       (c) direct href on the anchor -> fetch URL straight via request API.

    Returns (path, state) on success where state is "new"|"updated"|"unchanged",
    else None.
    """
    ctx = page.context

    # (c) Direct href first (fast path).
    href = None
    try:
        href = element.get_attribute("href")
    except Exception:
        pass
    if href and not href.startswith(("javascript:", "#")):
        full_url = urljoin(page.url, href)
        try:
            resp = ctx.request.get(full_url)
            if resp.ok:
                return _write_pdf(resp, full_url, target_path, folder, fallback_name)
        except Exception:
            pass  # fall through to click-race

    # Race: register listeners for download events and new pages BEFORE
    # clicking. Downloads might fire on the main page (Content-Disposition:
    # attachment on the current response) OR on a popup (server opens a new
    # tab that then triggers a download). Catch either by attaching a
    # download listener to every popup too.
    captured: dict = {"download": None, "popup": None}
    extra_popups: list = []  # track to clean up

    def _on_download(d):
        if captured["download"] is None:
            captured["download"] = d

    def _on_popup(p):
        if captured["popup"] is None:
            captured["popup"] = p
        extra_popups.append(p)
        # Forward any download that fires on the popup to our main listener.
        try:
            p.on("download", _on_download)
        except Exception:
            pass

    page.on("download", _on_download)
    ctx.on("page", _on_popup)
    try:
        try:
            element.evaluate("el => el.click()")
        except Exception as exc:
            err(f"   click error: {exc}")
            return None

        # Wait up to DOWNLOAD_TIMEOUT_MS for a download. If only a popup fires
        # first, give it another few seconds in case the popup itself triggers
        # a download.
        slices = DOWNLOAD_TIMEOUT_MS // 200
        popup_grace_slices = 25  # 25 * 200ms = 5s after popup
        popup_seen_at = None
        for i in range(slices):
            if captured["download"]:
                break
            if captured["popup"] and popup_seen_at is None:
                popup_seen_at = i
            if popup_seen_at is not None and (i - popup_seen_at) >= popup_grace_slices:
                break
            try:
                page.wait_for_timeout(200)
            except Exception:
                break
    finally:
        try:
            page.remove_listener("download", _on_download)
        except Exception:
            pass
        try:
            ctx.remove_listener("page", _on_popup)
        except Exception:
            pass
        for p in extra_popups:
            try:
                p.remove_listener("download", _on_download)
            except Exception:
                pass

    dl = captured["download"]
    if dl is not None:
        name = (
            target_path.name if target_path
            else (dl.suggested_filename or fallback_name or "unnamed.pdf")
        )
        result: tuple[Path, str] | None = None
        try:
            tmp_path = dl.path()
        except Exception:
            tmp_path = None
        try:
            if tmp_path:
                new_bytes = Path(tmp_path).read_bytes()
                if target_path is not None:
                    out = target_path
                else:
                    assert folder is not None, "folder required when target_path is None"
                    out = _resolve_collision_path(folder, name, new_bytes)
                state = _write_bytes_with_dedup(new_bytes, out)
                result = (out, state)
            else:
                # No local tmp path; fall back to save_as. We can't dedup, so
                # treat as new/updated depending on whether the target existed.
                out = target_path if target_path else (folder / sanitize(name))
                was_present = out.exists()
                dl.save_as(str(out))
                result = (out, "updated" if was_present else "new")
        except Exception as exc:
            err(f"   download.save_as failed: {exc}")
            result = None
        # Close any popup we opened on the way.
        for p in extra_popups:
            try:
                p.close()
            except Exception:
                pass
        return result

    popup = captured["popup"]
    if popup is not None:
        for _ in range(30):
            if popup.url and not popup.url.startswith("about:"):
                break
            try:
                popup.wait_for_timeout(100)
            except Exception:
                break
        pdf_url = popup.url
        try:
            popup.close()
        except Exception:
            pass
        if pdf_url and not pdf_url.startswith("about:"):
            try:
                resp = ctx.request.get(pdf_url)
                if resp.ok:
                    return _write_pdf(resp, pdf_url, target_path, folder, fallback_name)
            except Exception:
                pass
    return None


def _resolve_collision_path(folder: Path, name: str, new_bytes: bytes) -> Path:
    """Pick a write path for `new_bytes` that won't clobber an unrelated
    attachment with the same suggested filename.

    Two attachments on a single PRA can share a filename (e.g., both
    `search_audit.csv`). Prior versions silently overwrote. To preserve
    both, on collision both files move to hash-suffixed siblings
    `stem.<8hex>.ext`. The hash is per-content, so on re-runs each file
    finds its sibling regardless of download order.
    """
    out = folder / sanitize(name)
    new_hash = hashlib.md5(new_bytes).hexdigest()[:8]
    hashed = folder / f"{out.stem}.{new_hash}{out.suffix}"

    if hashed.exists():
        return hashed

    if out.exists():
        existing = out.read_bytes()
        if existing == new_bytes:
            return out
        new_fp = pdf_text_fingerprint(new_bytes)
        old_fp = pdf_text_fingerprint(existing)
        if new_fp is not None and new_fp == old_fp:
            return out
        existing_hash = hashlib.md5(existing).hexdigest()[:8]
        existing_hashed = folder / f"{out.stem}.{existing_hash}{out.suffix}"
        if existing_hashed != hashed and not existing_hashed.exists():
            out.rename(existing_hashed)
        return hashed

    peer_pattern = re.compile(
        rf"^{re.escape(out.stem)}\.[0-9a-f]{{8}}{re.escape(out.suffix)}$"
    )
    if any(peer_pattern.match(p.name) for p in folder.iterdir()):
        return hashed

    return out


def _write_bytes_with_dedup(new_bytes: bytes, out: Path) -> str:
    """Write `new_bytes` to `out`, skipping if content is unchanged.

    Returns "new" (file didn't exist), "updated" (content differs), or
    "unchanged" (byte-identical or semantically-equivalent text content —
    avoids spamming git history with metadata-only churn on MH PDFs).
    """
    if out.exists():
        existing = out.read_bytes()
        if existing == new_bytes:
            return "unchanged"
        new_fp = pdf_text_fingerprint(new_bytes)
        old_fp = pdf_text_fingerprint(existing)
        if new_fp is not None and new_fp == old_fp:
            return "unchanged"
        out.write_bytes(new_bytes)
        return "updated"
    out.write_bytes(new_bytes)
    return "new"


def _write_pdf(resp, url: str,
               target_path: Path | None,
               folder: Path | None,
               fallback_name: str | None) -> tuple[Path, str]:
    new_bytes = resp.body()
    if target_path is not None:
        out = target_path
    else:
        name = filename_from_response(resp, url) or fallback_name or "unnamed.pdf"
        assert folder is not None, "folder required when target_path is None"
        out = _resolve_collision_path(folder, name, new_bytes)
    state = _write_bytes_with_dedup(new_bytes, out)
    return out, state


REPO = Path(__file__).resolve().parent.parent
PRA_ROOT = REPO / "assets" / "san-mateo-public-records"
CONFIG_DIR = Path.home() / ".config" / "sm-alpr"
AUTH_STATE = CONFIG_DIR / "pra_auth.json"
CONFIG_PATH = CONFIG_DIR / "pra_config.json"
CREDENTIALS_PATH = CONFIG_DIR / "pra_credentials.json"

# Fallback portal URLs for CI runs where no local pra_config.json exists.
# These are public endpoints; they're hardcoded only to spare CI an extra
# config bootstrap step. Local `--login` overrides them.
DEFAULT_PORTAL_BASE = "https://sanmateoca.mycusthelp.com/WEBAPP/_rs"
DEFAULT_SUPPORT_HOME = "https://sanmateoca.mycusthelp.com/WEBAPP/_rs/supporthome.aspx"
# Direct URL to the GovQA login form (supporthome.aspx is a public landing
# page with nav but no login form, so we navigate here explicitly during
# auto-login).
LOGIN_PATH = "login.aspx"
# Authenticated requests-list URL. After login the script saves this as the
# support_home_url so subsequent scrapes go straight to the list.
REQUESTS_LIST_PATH = "CustomerIssues.aspx"

REQUEST_ID_RE = re.compile(r"^W\d{6}-\d{6}$")
W_REQUEST_ID_RE = re.compile(r"\bW\d{6}-\d{6}\b")
SESSION_TOKEN_RE = re.compile(r"/\(S\([^)]+\)\)/")
SSESSIONID_RE = re.compile(r"[?&]sSessionID=[^&]*")
DOWNLOAD_TIMEOUT_MS = 60_000  # portal pre-signs S3 URLs; can take several seconds
NAV_TIMEOUT_MS = 20_000

# Per-request attachment labels that are known to be unretrievable from the
# portal — e.g., uploads where the original filename couldn't be parsed and
# the portal serves a broken anchor. Without skipping, save_pdf_via_click
# hangs the full DOWNLOAD_TIMEOUT_MS waiting for a download event that
# never fires, every run.
SKIP_ATTACHMENTS: dict[str, frozenset[str]] = {
    "W012462-040226": frozenset({"download"}),
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text())


def save_config(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2))


def strip_session_token(url: str) -> str:
    """GovQA URLs carry two rotating session identifiers:
       - path segment /(S(abc123))/
       - query param  ?sSessionID=XYZ
    Drop both so cookie auth alone carries the session across runs.
    """
    url = SESSION_TOKEN_RE.sub("/", url)
    url = SSESSIONID_RE.sub("", url)
    # Clean up possibly dangling ? if sSessionID was the only param.
    if url.endswith("?"):
        url = url[:-1]
    # Normalize ?& → ?
    url = url.replace("?&", "?")
    return url


USERNAME_SELECTORS = [
    # GovQA / DevExpress ASPxFormLayout — the SMPD portal's actual login form.
    "input[name='ASPxFormLayout1$txtUsername']",
    "#ASPxFormLayout1_txtUsername_I",
    "input[aria-label='Email Address']",
    # Generic fallbacks.
    "input[type='email']",
    "input[name*='Email' i]",
    "input[id*='Email' i]",
    "input[name*='Username' i]",
    "input[id*='Username' i]",
    "input[name*='UserName' i]",
    "input[id*='UserName' i]",
    "input[name*='Login' i]:not([type='password']):not([type='submit'])",
]

PASSWORD_SELECTORS = [
    "input[name='ASPxFormLayout1$txtPassword']",
    "#ASPxFormLayout1_txtPassword_I",
    "input[type='password']",
    "input[name*='Password' i]",
    "input[id*='Password' i]",
]

LOGIN_SUBMIT_SELECTORS = [
    "input[name='ASPxFormLayout1$btnLogin']",
    "#ASPxFormLayout1_btnLogin_I",
    "input[type='submit'][value*='Sign In' i]",
    "input[type='submit'][value*='Log In' i]",
    "input[type='submit'][value*='Login' i]",
    # GovQA labels its login submit "Submit"; scope to a form to avoid
    # matching the accessibility-required hidden submit at page root.
    "form#qacLogin input[type='submit']",
    "button:has-text('Sign In')",
    "button:has-text('Log In')",
    "button:has-text('Login')",
    "button[type='submit']",
]


def load_credentials() -> dict | None:
    """Resolve credentials from env vars (CI-friendly) or the local file.
    Env vars win when both are set."""
    env_user = os.environ.get("PRA_USERNAME")
    env_pass = os.environ.get("PRA_PASSWORD")
    if env_user and env_pass:
        return {"username": env_user, "password": env_pass}
    if not CREDENTIALS_PATH.exists():
        return None
    try:
        data = json.loads(CREDENTIALS_PATH.read_text())
    except json.JSONDecodeError as exc:
        err(f"   credentials file is not valid JSON: {exc}")
        return None
    if not data.get("username") or not data.get("password"):
        err(f"   credentials file missing 'username' or 'password'")
        return None
    return data


def _first_visible(page: Page, selectors: list[str]):
    for sel in selectors:
        loc = page.locator(sel).first
        if loc.count() == 0:
            continue
        try:
            loc.wait_for(state="visible", timeout=1_500)
        except PWTimeout:
            continue
        diag(f"   matched: {sel}")
        return loc
    return None


def perform_auto_login(page: Page, portal_base: str, creds: dict) -> bool:
    """Drive the GovQA login form headlessly. Returns True on success.

    Does NOT mutate config — caller is responsible for saving auth state /
    home URL if it wants those captured."""
    login_url = portal_base.rstrip("/") + "/" + LOGIN_PATH
    page.goto(login_url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
    except PWTimeout:
        pass

    # If we land on login.aspx and there's no password field, GovQA may
    # have already redirected us off the login form because cookies were
    # still valid. Verify by navigating to the requests list.
    if page.locator("input[type='password']").count() == 0:
        diag("   no password field on login.aspx — checking if already authed")
        list_url = portal_base.rstrip("/") + "/" + REQUESTS_LIST_PATH
        page.goto(list_url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
        except PWTimeout:
            pass
        if _looks_like_requests_list(page):
            return True
        # Otherwise fall through; we may need to fill the form after all.
        page.goto(login_url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
        except PWTimeout:
            pass

    user_field = _first_visible(page, USERNAME_SELECTORS)
    pass_field = _first_visible(page, PASSWORD_SELECTORS)
    if user_field is None or pass_field is None:
        err("   could not find login form (username/password inputs)")
        _dump_page_diag(page)
        return False

    if page.locator("[id*='captcha' i], [name*='captcha' i], img[src*='captcha' i]").count() > 0:
        err("   page appears to have a CAPTCHA — auto-login won't work; "
            "run --login instead")
        return False

    try:
        user_field.fill(creds["username"])
        pass_field.fill(creds["password"])
    except Exception as exc:
        err(f"   could not fill credentials: {exc}")
        return False

    # DevExpress ASPxTextBox stores the real value on a JS object; Playwright's
    # .fill() fires native events but doesn't always sync the DevExpress state.
    # Belt-and-suspenders: also call SetText on the client object if it exists.
    try:
        page.evaluate(
            "(creds) => {"
            "  if (typeof ASPxFormLayout1_txtUsername !== 'undefined' && ASPxFormLayout1_txtUsername.SetText) {"
            "    ASPxFormLayout1_txtUsername.SetText(creds.u);"
            "  }"
            "  if (typeof ASPxFormLayout1_txtPassword !== 'undefined' && ASPxFormLayout1_txtPassword.SetText) {"
            "    ASPxFormLayout1_txtPassword.SetText(creds.p);"
            "  }"
            "}",
            {"u": creds["username"], "p": creds["password"]},
        )
    except Exception as exc:
        diag(f"   DevExpress SetText fallback failed (ok if non-DX): {exc}")

    # Try DevExpress's client-side DoClick first — the visible submit "button"
    # is actually a styled span with a hidden <input type=submit> sibling, so
    # selector-based .click() often hits the hidden input and does nothing.
    submitted_via = None
    try:
        submitted_via = page.evaluate(
            "() => {"
            "  if (typeof ASPxFormLayout1_btnLogin !== 'undefined' && ASPxFormLayout1_btnLogin.DoClick) {"
            "    ASPxFormLayout1_btnLogin.DoClick();"
            "    return 'devexpress';"
            "  }"
            "  return null;"
            "}"
        )
    except Exception as exc:
        diag(f"   DevExpress DoClick failed: {exc}")

    if not submitted_via:
        # Fallback: force-click the hidden input via JS (bypasses visibility).
        try:
            page.locator("input[name='ASPxFormLayout1$btnLogin']").first.evaluate(
                "el => el.click()"
            )
            submitted_via = "force-click"
        except Exception as exc:
            diag(f"   force-click submit failed: {exc}")

    if not submitted_via:
        submit = _first_visible(page, LOGIN_SUBMIT_SELECTORS)
        if submit is None:
            err("   could not find a submit button")
            return False
        try:
            submit.click()
            submitted_via = "selector"
        except Exception as exc:
            err(f"   submit click failed: {exc}")
            return False

    diag(f"   submitted via: {submitted_via}")
    try:
        page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
    except PWTimeout:
        pass

    if page.locator("input[type='password']").count() > 0:
        # Still on a login form. Look for an inline error message (DevExpress
        # validation balloons live in spans flagged dxEErrorCellSys, and GovQA
        # also drops free-text errors into elements with 'error' in the class).
        err_text = ""
        for sel in (
            "[class*='dxEErrorCell' i]",
            "[class*='ValidationError' i]",
            "[class*='error' i]:not(:empty)",
            "[id*='lblError' i]",
        ):
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    txt = (loc.inner_text() or "").strip()
                    if txt:
                        err_text = txt
                        break
            except Exception:
                pass

        # Check for CAPTCHA elements that may have appeared after the first
        # failed attempt (some GovQA tenants only show CAPTCHA after one miss).
        captcha_count = page.locator(
            "[id*='captcha' i], [name*='captcha' i], img[src*='captcha' i]"
        ).count()

        # Dump a screenshot so the user can eyeball what happened.
        shot = Path("/tmp") / "pra_login_failure.png"
        try:
            page.screenshot(path=str(shot), full_page=True)
        except Exception:
            shot = None

        if err_text:
            err(f"   login failed: {err_text}")
        elif captcha_count > 0:
            err("   login failed and a CAPTCHA element is now on the page — "
                "auto-login may need a CAPTCHA-solve step")
        else:
            body = ""
            try:
                body = page.evaluate("() => (document.body && document.body.innerText) || ''")
            except Exception:
                pass
            snippet = " | ".join(line.strip() for line in body.splitlines() if line.strip())[:300]
            err(f"   login failed — still on a password form. body: {snippet}")

        if shot is not None:
            err(f"   screenshot: {shot}")
        return False

    return True


def auto_login() -> int:
    creds = load_credentials()
    if creds is None:
        print(f"No credentials at {CREDENTIALS_PATH}.\n"
              f"Create it with:\n"
              f"  {{\"username\": \"...\", \"password\": \"...\"}}\n"
              f"and chmod 600.", file=sys.stderr)
        return 1

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = load_config()
    portal_base = config.get("portal_base") or DEFAULT_PORTAL_BASE

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        ok = perform_auto_login(page, portal_base, creds)
        if not ok:
            browser.close()
            return 1
        # Navigate to the requests list so the captured URL is the list
        # itself (matches the interactive --login flow's expectation).
        list_url = portal_base.rstrip("/") + "/" + REQUESTS_LIST_PATH
        page.goto(list_url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
        except PWTimeout:
            pass
        if not _looks_like_requests_list(page):
            err("logged in, but couldn't render the My Requests list "
                "at " + list_url)
            _dump_page_diag(page)
            browser.close()
            return 1
        home_url = strip_session_token(page.url)
        config["portal_base"] = portal_base
        config["support_home_url"] = home_url
        save_config(config)
        print(f"Captured home URL: {home_url}")
        context.storage_state(path=str(AUTH_STATE))
        print(f"Saved auth → {AUTH_STATE}")
        browser.close()
    return 0


def login() -> int:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = load_config()
    start = config.get("portal_base") or input(
        "Portal base URL (e.g. https://sanmateoca.mycusthelp.com/): "
    ).strip()
    if not start:
        print("No base URL provided.", file=sys.stderr)
        return 1

    print()
    print("Opening a Chromium browser. In the browser:")
    print("  1. Log in to the portal.")
    print("  2. Land on the support home page (list of your requests).")
    print("  3. Return here and press Enter to capture session + home URL.")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.goto(start, wait_until="domcontentloaded")
        input("Press Enter when you're on the support home page… ")
        home_url = strip_session_token(page.url)
        print(f"Captured home URL: {home_url}")
        config["portal_base"] = start
        config["support_home_url"] = home_url
        save_config(config)
        context.storage_state(path=str(AUTH_STATE))
        print(f"Saved auth   → {AUTH_STATE}")
        print(f"Saved config → {CONFIG_PATH}")
        browser.close()
    return 0


def discover_requests() -> list[str]:
    if not PRA_ROOT.exists():
        return []
    return sorted(
        d.name for d in PRA_ROOT.iterdir()
        if d.is_dir() and REQUEST_ID_RE.match(d.name)
    )


def discover_portal_ids(page: Page, home_url: str,
                        max_pages: int = 30) -> set[str]:
    """Walk the My Requests list, paging forward to enumerate every visible
    W-request id. Best-effort: returns whatever we collected if list nav fails."""
    page.goto(home_url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
    except PWTimeout:
        pass
    if not ensure_on_requests_list(page):
        err("   could not reach requests list for discovery")
        return set()

    found: set[str] = set()
    page_num = 1
    while True:
        try:
            text = page.evaluate(
                "() => (document.body && document.body.innerText) || ''"
            )
        except Exception:
            text = ""
        page_ids = set(W_REQUEST_ID_RE.findall(text))
        new_count = len(page_ids - found)
        found |= page_ids
        diag(f"   discover page {page_num}: {len(page_ids)} ids "
             f"({new_count} new), running total {len(found)}")
        if page_num >= max_pages:
            warn(f"   discovery hit page limit ({max_pages}); "
                 f"some requests may be missed")
            break
        if not click_page_number(page, page_num + 1):
            break
        page_num += 1
    return found


def discover_and_stub_new_requests(page: Page, home_url: str) -> list[str]:
    """Enumerate portal request ids and mkdir an empty folder for any that
    don't yet exist on disk. Returns the sorted list of newly-stubbed ids."""
    print(_c("Discovering portal request ids...", "bold"))
    portal_ids = discover_portal_ids(page, home_url)
    existing = set(discover_requests())
    new_ids = sorted(portal_ids - existing)
    for rid in new_ids:
        (PRA_ROOT / rid).mkdir(parents=True, exist_ok=True)
    return new_ids


def local_pdf_names(folder: Path) -> set[str]:
    return {p.name for p in folder.glob("*.pdf")}


DOCLIKE_EXTS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".csv",
                ".png", ".jpg", ".jpeg", ".txt", ".eml")


# Scoped selectors for attachment lists on GovQA/DevExpress RequestEdit pages.
# Each selector is tried in order; the first non-empty match is used.
ATTACHMENT_SCOPE_SELECTORS = [
    "#trAttachments a",
    "#dvAttachments a",
    "[id$='Attachments'] a",
    "[id*='ttachment'] a",
]


def _get_attachment_anchors(page: Page) -> list:
    """Return raw anchor element handles from the attachments section of
    RequestEdit.aspx. These usually have javascript:__doPostBack hrefs, so
    they need to be clicked rather than fetched directly."""
    for sel in ATTACHMENT_SCOPE_SELECTORS:
        try:
            found = page.query_selector_all(sel)
        except Exception:
            continue
        if found:
            return found
    return []


def find_download_links(page: Page) -> list[tuple[str, str]]:
    """Return (label, full_url) for attachment download links.

    Scopes to the attachments section of the DOM first (e.g. #trAttachments
    on RequestEdit.aspx), falling back to the full page. Skips anchors whose
    href is javascript:/# (they need click-based fallback — callers can
    detect these by seeing no matches here and falling back to clicking)."""
    anchors = []
    for sel in ATTACHMENT_SCOPE_SELECTORS:
        try:
            found = page.query_selector_all(sel)
        except Exception:
            continue
        if found:
            diag(f"   attachment scope: {sel} ({len(found)} anchors)")
            if VERBOSE:
                for i, a in enumerate(found[:6]):
                    txt = (a.inner_text() or "").strip()[:60]
                    href = (a.get_attribute("href") or "")[:100]
                    onclick = (a.get_attribute("onclick") or "")[:100]
                    diag(f"     sample[{i}] text={txt!r} href={href!r} onclick={onclick!r}")
            anchors = found
            break
    if not anchors:
        anchors = page.query_selector_all("a")

    out: list[tuple[str, str]] = []
    for a in anchors:
        href = a.get_attribute("href") or ""
        text = (a.inner_text() or "").strip()
        if not text and not href:
            continue
        if not href or href.startswith(("javascript:", "#")):
            continue
        lower_txt = text.lower()
        lower_href = href.lower()
        matches = (
            any(ext in lower_txt for ext in DOCLIKE_EXTS)
            or "downloadexternalfile" in lower_href
            or "documents.aspx" in lower_href
            or "getfile" in lower_href
            or "download" in lower_href
            or "attachment" in lower_href
        )
        if matches:
            out.append((text or href, urljoin(page.url, href)))
    return out


DETAILS_BUTTON_RE = re.compile(r"\bDetails\b", re.IGNORECASE)
VIEW_FILES_BUTTON_RE = re.compile(r"View File", re.IGNORECASE)
PRINT_MESSAGES_BUTTON_RE = re.compile(r"Print.*Message|Message.*History", re.IGNORECASE)


def _card_scoped_button_xpath(request_id: str, button_text: str) -> str:
    """Select a <button> labeled `button_text` inside the card containing `request_id`."""
    return (
        f"xpath=//*[contains(text(), '{request_id}')]"
        f"/ancestor::*[.//button[contains(., '{button_text}')]][1]"
        f"//button[contains(., '{button_text}')]"
    )


def locate_request_on_page(page: Page, request_id: str,
                           button_label: str = "View File"):
    """Find the named button on the card containing `request_id`.

    GovQA renders each request as a card with the id as plain text, a
    "View File(s)" button, and a "Details" button. Scoping by XPath
    ancestor keeps us from matching buttons in other cards.
    """
    xpath_sel = _card_scoped_button_xpath(request_id, button_label)
    candidate = page.locator(xpath_sel).first
    try:
        candidate.wait_for(state="visible", timeout=3_000)
        return candidate, f"card-button({button_label})"
    except PWTimeout:
        pass

    # Fallback: older list UIs may use anchors instead of buttons.
    fallbacks = [
        f"tr:has-text('{request_id}') a",
        f"a:has-text('{request_id}')",
    ]
    for sel in fallbacks:
        candidate = page.locator(sel).first
        try:
            candidate.wait_for(state="visible", timeout=1_500)
            return candidate, sel
        except PWTimeout:
            continue
    return None, None


def click_page_number(page: Page, n: int) -> bool:
    """Click a numbered pagination button (1, 2, 3, …)."""
    selectors = [
        f"a:text-is('{n}')",
        f"button:text-is('{n}')",
        f"[role=button]:text-is('{n}')",
        f"[aria-label='Page {n}']",
        f"[aria-label='Go to page {n}']",
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=1_500)
        except PWTimeout:
            continue
        try:
            with page.expect_navigation(timeout=NAV_TIMEOUT_MS):
                loc.click()
        except PWTimeout:
            # AJAX paginators don't trigger nav; wait for network idle instead.
            try:
                page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
            except PWTimeout:
                pass
        return True
    return False


MY_REQUESTS_LINK_SELECTORS = [
    "a:has-text('My Request Center')",
    "a:has-text('My Requests')",
    "a:has-text('My Public Records Center')",
]


# Drive the DevExpress search form on CustomerIssues.aspx. The controls are
# exposed as globals: lstSelect (criteria), lstCondition (operator),
# txtCriteria (value). Setting via their JS API is far more reliable than
# driving the rendered combo UI.
SEARCH_JS = r"""
(rid) => {
    if (typeof lstSelect !== 'undefined' && lstSelect && lstSelect.SetText) {
        lstSelect.SetText('Reference No');
    }
    if (typeof lstCondition !== 'undefined' && lstCondition && lstCondition.SetText) {
        lstCondition.SetText('Equals');
    }
    if (typeof txtCriteria !== 'undefined' && txtCriteria && txtCriteria.SetText) {
        txtCriteria.SetText(rid);
    }
}
"""


GO_BUTTON_SELECTORS = [
    "input[type=submit][value='Go']",
    "input[type=button][value='Go']",
    "button:has-text('Go')",
    "div[role='button']:has-text('Go')",
    "a[role='button']:has-text('Go')",
    "[role='button']:has-text('Go')",
    "a:has-text('Go')",
    "span:text-is('Go')",
]


# DevExpress fallback: find the client-side button object whose caption is 'Go'
# and invoke its DoClick() method. This bypasses DOM rendering quirks.
CLICK_GO_DEVEX_JS = r"""
() => {
    for (const key in window) {
        try {
            const obj = window[key];
            if (!obj) continue;
            const textFn = obj.GetText || obj.GetValue;
            if (typeof textFn !== 'function') continue;
            const caption = textFn.call(obj);
            if (caption !== 'Go') continue;
            if (typeof obj.DoClick === 'function') {
                obj.DoClick();
                return key;
            }
        } catch (e) { /* skip non-DX globals */ }
    }
    return null;
}
"""


def search_for_request(page: Page, request_id: str) -> bool:
    """Narrow the My Requests list to a single card via the Search Criteria form.
    Returns True if the resulting page contains the request id."""
    try:
        page.evaluate(SEARCH_JS, request_id)
    except Exception as exc:
        err(f"   search form setup failed: {exc}")
        return False

    clicked = False
    for sel in GO_BUTTON_SELECTORS:
        loc = page.locator(sel).first
        if loc.count() == 0:
            continue
        diag(f"   clicking Go via {sel}")
        try:
            with page.expect_navigation(timeout=NAV_TIMEOUT_MS):
                loc.click(force=True)
            clicked = True
            break
        except PWTimeout:
            # AJAX postback (no full nav); wait for network and continue.
            try:
                page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
            except PWTimeout:
                pass
            clicked = True
            break
        except Exception as exc:
            diag(f"   click via {sel} failed: {exc}")
            continue

    if not clicked:
        # DevExpress client-side fallback.
        try:
            key = page.evaluate(CLICK_GO_DEVEX_JS)
        except Exception as exc:
            err(f"   DevExpress Go fallback failed: {exc}")
            key = None
        if key:
            diag(f"   clicked Go via DevExpress client object: {key}")
            try:
                page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
            except PWTimeout:
                pass
            clicked = True

    if not clicked:
        err("   could not trigger 'Go' button")
        return False

    # Verify the card for `request_id` is present after search.
    return page.locator(f"*:has-text('{request_id}')").count() > 0


LIST_INDICATORS = [
    "button:has-text('Details')",
    "button:has-text('View File')",
    "input[value='Details']",
    "input[value*='View File']",
    "a:has-text('Details')",
    "a:has-text('View File')",
    # DevExpress ASPxButton often wraps text in a <span>.
    "span:text-is('Details')",
    "span:text-is('View File(s)')",
]


def _looks_like_requests_list(page: Page) -> bool:
    for sel in LIST_INDICATORS:
        if page.locator(sel).count() > 0:
            return True
    return False


def _dump_page_diag(page: Page, limit: int = 40) -> None:
    """Print a compact snapshot of the page so we can see why list detection failed."""
    if not VERBOSE:
        return
    diag(f"   [diag] url={page.url}")
    try:
        title = page.title()
        diag(f"   [diag] title={title!r}")
    except Exception:
        pass
    try:
        text = page.evaluate("() => (document.body && document.body.innerText) || ''")
        snippet = " | ".join(
            line.strip() for line in text.splitlines() if line.strip()
        )[:500]
        diag(f"   [diag] body text: {snippet}")
    except Exception:
        pass


def ensure_on_requests_list(page: Page) -> bool:
    """Make sure the current page is the 'My Requests' list (has Details /
    View File(s) buttons per card). If it's the generic support home, read the
    nav link's href and goto() it directly — avoids JS / visibility issues."""
    if _looks_like_requests_list(page):
        return True
    for sel in MY_REQUESTS_LINK_SELECTORS:
        link = page.locator(sel).first
        if link.count() == 0:
            continue
        # Prefer goto(href) — the server renders hrefs with live session tokens
        # on already-authed pages, and some nav items live in a hidden mobile
        # nav clone that breaks .click() with "Element is not visible".
        href = None
        try:
            href = link.get_attribute("href")
        except Exception:
            pass
        if href:
            target = urljoin(page.url, href)
            diag(f"   navigating to: {target}")
            page.goto(target, wait_until="domcontentloaded")
        else:
            try:
                with page.expect_navigation(timeout=NAV_TIMEOUT_MS):
                    link.click(force=True)
            except PWTimeout:
                pass
        try:
            page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
        except PWTimeout:
            pass
        if _looks_like_requests_list(page):
            return True
        _dump_page_diag(page)
    return False


def open_request_from_home(page: Page, home_url: str, request_id: str,
                           button_label: str = "View File",
                           max_pages: int = 20) -> bool:
    """Walk the My Requests list (paginating as needed), click the named button
    on the card for `request_id`. `button_label` is 'View File' (attachments)
    or 'Details' (request detail / message history)."""
    page.goto(home_url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
    except PWTimeout:
        pass

    if not ensure_on_requests_list(page):
        err(f"   could not navigate to My Requests list from {home_url}")
        return False

    page_num = 1
    while page_num <= max_pages:
        locator, sel = locate_request_on_page(page, request_id, button_label)
        if locator is not None:
            if page_num > 1:
                diag(f"   found on page {page_num} via {sel}")
            else:
                diag(f"   matched selector: {sel}")
            try:
                with page.expect_navigation(timeout=NAV_TIMEOUT_MS):
                    locator.click()
            except PWTimeout:
                pass
            try:
                page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
            except PWTimeout:
                pass
            return True

        # Try to advance to the next numbered page.
        next_num = page_num + 1
        if not click_page_number(page, next_num):
            break
        page_num = next_num

    err(f"   could not locate '{request_id}' after {page_num} page(s)")
    if VERBOSE:
        anchors = page.query_selector_all("a")
        diag(f"   last page has {len(anchors)} anchors; first 30:")
        for a in anchors[:30]:
            text = (a.inner_text() or "").strip()
            href = a.get_attribute("href") or ""
            if text or href:
                diag(f"     a[{text[:60]!r}] href={href[:100]}")
    return False


def sanitize(name: str) -> str:
    return re.sub(r"[^\w\-\. ]", "_", name.strip()).strip() or "unnamed.pdf"


PRINT_MESSAGES_SELECTORS = [
    "a:has-text('Print Messages (PDF)')",
    "button:has-text('Print Messages (PDF)')",
    "a:has-text('Print Messages')",
    "button:has-text('Print Messages')",
    "a:has-text('Print All Messages')",
    "button:has-text('Print All Messages')",
    "a:has-text('Message History')",
]


def _goto_request_section(page: Page, home_url: str, request_id: str,
                          section: str) -> bool:
    """Use the search form to narrow to `request_id`, then click its
    View File(s) or Details button. Falls back to paginating + clicking if
    the search form isn't available."""
    page.goto(home_url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
    except PWTimeout:
        pass
    if not ensure_on_requests_list(page):
        err(f"   could not reach requests list")
        return False

    button_label = "View File" if section == "view_files" else "Details"

    if search_for_request(page, request_id):
        # After search there's exactly one card on the page, so we can grab
        # the only matching button directly — no card-scoping needed.
        # get_by_role matches <button>, <input type="submit">, and anything
        # with role='button', which covers DevExpress ASPxButton variants.
        name_re = re.compile(rf"^\s*{re.escape(button_label)}",
                             re.IGNORECASE)
        btn = page.get_by_role("button", name=name_re).first
        try:
            btn.wait_for(state="attached", timeout=5_000)
        except PWTimeout:
            err(f"   {button_label} button not attached after search")
            return False
        # Invoke the native HTMLElement.click() via JS: fires the click event
        # AND triggers default actions (anchor nav, form submit, onclick),
        # without Playwright's scroll-into-view dance. The page's own JS has
        # a scroll-restore handler (PortalCustomerIssuesScroll cookie) that
        # fights with Playwright's auto-scroll, producing the oscillation.
        nav_happened = True
        try:
            with page.expect_navigation(timeout=NAV_TIMEOUT_MS):
                btn.evaluate("el => el.click()")
        except PWTimeout:
            nav_happened = False
        try:
            page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
        except PWTimeout:
            pass
        if not nav_happened:
            diag(f"   [diag] no nav after {button_label} click; "
                 f"current url={page.url}")
        return True

    # Fallback: old paginate-and-click.
    return open_request_from_home(page, home_url, request_id,
                                  button_label=button_label)


def process_request(page: Page, home_url: str, request_id: str,
                    do_messages: bool, do_files: bool) -> None:
    """Visit a request's detail page ONCE and harvest both the Message History
    PDF URL and all attachment URLs. Then fetch everything via the context's
    request API (shares cookies) — no further navigation needed."""
    folder = PRA_ROOT / request_id
    folder.mkdir(parents=True, exist_ok=True)
    existing = local_pdf_names(folder)
    print(f"{_c('→', 'bold')} {_c(request_id, 'bold')}: "
          f"{len(existing)} existing PDF(s)")

    if not _goto_request_section(page, home_url, request_id, "details"):
        err(f"   could not open Details for {request_id} — skipping")
        return

    # Message History
    if do_messages:
        mh_target = folder / f"{request_id}_Message_History.pdf"
        mh_link = None
        for sel in PRINT_MESSAGES_SELECTORS:
            link = page.locator(sel).first
            try:
                link.wait_for(state="attached", timeout=3_000)
            except PWTimeout:
                continue
            mh_link = link
            break
        if mh_link is None:
            warn(f"   WARN: no Print Messages (PDF) link found")
        else:
            result = save_pdf_via_click(page, mh_link, target_path=mh_target)
            if result:
                _, state = result
                file_status(state, mh_target.name)
            else:
                err(f"   ERR: Print Messages fetch failed")

    # Attachments
    if do_files:
        anchors = _get_attachment_anchors(page)
        if not anchors:
            print(f"   0 attachment anchors")
            _dump_page_diag(page)
            return
        print(f"   {len(anchors)} attachment anchor(s)")
        counts = {"new": 0, "updated": 0, "unchanged": 0,
                  "skipped": 0, "failed": 0}
        # Track by (label, onclick, href) to survive DOM re-render between
        # clicks while still distinguishing two anchors that share a label
        # (e.g. two attachments uploaded with the same filename).
        processed: set[tuple[str, str, str]] = set()
        skip_labels = SKIP_ATTACHMENTS.get(request_id, frozenset())
        while True:
            current = _get_attachment_anchors(page)
            target_el = None
            target_label = ""
            target_key: tuple[str, str, str] | None = None
            for a in current:
                lbl = (a.inner_text() or "").strip()
                if not lbl:
                    continue
                key = (lbl,
                       a.get_attribute("onclick") or "",
                       a.get_attribute("href") or "")
                if key in processed:
                    continue
                target_el = a
                target_label = lbl
                target_key = key
                break
            if target_el is None or target_key is None:
                break
            processed.add(target_key)

            if target_label in skip_labels:
                counts["skipped"] += 1
                file_status("skipped", f"{target_label} (known broken)")
                continue

            if target_label in existing or sanitize(target_label) in existing:
                counts["skipped"] += 1
                file_status("skipped", target_label)
                continue

            result = save_pdf_via_click(page, target_el, folder=folder,
                                        fallback_name=target_label)
            if result is None:
                counts["failed"] += 1
                file_status("failed", target_label)
                continue
            saved, state = result
            counts[state] += 1
            file_status(state, saved.name)

        _print_summary(counts)


def _print_summary(counts: dict[str, int]) -> None:
    """One-line colorized summary. Zero-valued buckets are dimmed."""
    parts = []
    order = [
        ("new",       "new",       "green"),
        ("updated",   "updated",   "cyan"),
        ("unchanged", "unchanged", "dim"),
        ("skipped",   "skipped",   "dim"),
        ("failed",    "failed",    "red"),
    ]
    for key, label, color in order:
        n = counts.get(key, 0)
        if n == 0:
            parts.append(_c(f"{n} {label}", "dim"))
        else:
            parts.append(_c(f"{n} {label}", color, "bold"))
    print("   " + ", ".join(parts))


def _session_appears_expired(page: Page) -> bool:
    body = (page.content() or "").lower()
    return "login" in page.url.lower() or "sign in" in body[:5000]


def run(args, config: dict) -> None:
    home_url = config.get("support_home_url") or DEFAULT_SUPPORT_HOME

    do_files = not args.messages_only
    do_messages = not args.files_only

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context_kwargs = dict(accept_downloads=True,
                              viewport={"width": 1280, "height": 1600})
        if AUTH_STATE.exists():
            context_kwargs["storage_state"] = str(AUTH_STATE)
        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        # Prime: visit home, check we're actually logged in.
        page.goto(home_url, wait_until="domcontentloaded")
        if _session_appears_expired(page):
            creds = load_credentials()
            if creds is None:
                print("session appears expired and no credentials available — "
                      "run --login (or set PRA_USERNAME/PRA_PASSWORD)",
                      file=sys.stderr)
                browser.close()
                return
            print("session expired; attempting auto-login...")
            # Open a fresh context for auto-login so we don't carry stale cookies.
            try:
                context.close()
            except Exception:
                pass
            context = browser.new_context(accept_downloads=True,
                                          viewport={"width": 1280, "height": 1600})
            page = context.new_page()
            portal_base = config.get("portal_base") or DEFAULT_PORTAL_BASE
            if not perform_auto_login(page, portal_base, creds):
                err("auto-login failed; run --login")
                browser.close()
                return
            list_url = portal_base.rstrip("/") + "/" + REQUESTS_LIST_PATH
            page.goto(list_url, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
            except PWTimeout:
                pass
            if not _looks_like_requests_list(page):
                err("auto-login succeeded but couldn't reach requests list; "
                    "run --login")
                browser.close()
                return
            captured = strip_session_token(page.url)
            config["portal_base"] = portal_base
            config["support_home_url"] = captured
            save_config(config)
            home_url = captured
            context.storage_state(path=str(AUTH_STATE))

        if args.discover:
            new_ids = discover_and_stub_new_requests(page, home_url)
            if new_ids:
                print(f"{_c('+', 'green', 'bold')} "
                      f"discovered {len(new_ids)} new request(s):")
                for rid in new_ids:
                    print(f"     {_c('+', 'green', 'bold')} {rid}")
            else:
                print("no new requests on portal")

        targets = list(args.requests)
        if args.all or args.active or not targets:
            targets = discover_requests()
        if args.active and not args.all:
            # A closed PRA won't gain new messages, so skip it. --all overrides
            # (re-checks everything in case a closed request changed).
            from build_pra_registry import closed_ids
            closed = closed_ids()
            skipped = sorted(t for t in targets if t in closed)
            targets = [t for t in targets if t not in closed]
            if skipped:
                print(f"{_c('—', 'yellow', 'bold')} --active: skipping "
                      f"{len(skipped)} closed PRA(s): {', '.join(skipped)}")
        if not targets:
            print("No target requests.", file=sys.stderr)
            browser.close()
            return

        for rid in targets:
            if not REQUEST_ID_RE.match(rid):
                print(f"skipping '{rid}': not a W-request id", file=sys.stderr)
                continue
            process_request(page, home_url, rid,
                            do_messages=do_messages, do_files=do_files)

        browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--login", action="store_true",
                        help="Interactive login / refresh saved session")
    parser.add_argument("--auto-login", action="store_true",
                        help="Headless login using stored credentials "
                             "(file at ~/.config/sm-alpr/pra_credentials.json "
                             "or PRA_USERNAME/PRA_PASSWORD env vars)")
    parser.add_argument("--all", action="store_true",
                        help="Iterate every W* folder (default when no ids given)")
    parser.add_argument("--active", action="store_true",
                        help="Like --all but skip PRAs already closed (a closed "
                             "request won't gain new messages). Use --all to "
                             "re-check closed ones in case something changed.")
    parser.add_argument("--discover", action="store_true",
                        help="Walk the portal list and create stub folders for "
                             "any new request ids before scraping")
    parser.add_argument("--headed", action="store_true",
                        help="Show the browser window (default: headless)")
    parser.add_argument("--files-only", action="store_true",
                        help="Only download attachments, skip message history")
    parser.add_argument("--messages-only", action="store_true",
                        help="Only refresh message-history PDFs, skip attachments")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show diagnostic output (selector matches, page dumps, etc.)")
    parser.add_argument("requests", nargs="*",
                        help="Specific request ids (e.g. W012297-030826)")
    args = parser.parse_args()

    global VERBOSE
    VERBOSE = args.verbose

    if args.login:
        return login()

    if args.auto_login:
        return auto_login()

    if not AUTH_STATE.exists() and load_credentials() is None:
        print("No saved auth and no credentials.\n"
              "Run one of:\n"
              "  uv run python scripts/pra_download.py --login\n"
              "  uv run python scripts/pra_download.py --auto-login   "
              "(after writing ~/.config/sm-alpr/pra_credentials.json "
              "or setting PRA_USERNAME/PRA_PASSWORD)",
              file=sys.stderr)
        return 1

    config = load_config()
    run(args, config)

    print()
    print("Done. To OCR newly downloaded files:")
    print("  uv run python scripts/ocr_sidecar.py --staged")
    return 0


if __name__ == "__main__":
    sys.exit(main())

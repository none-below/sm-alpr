#!/usr/bin/env python3
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

After downloading, run OCR to refresh sidecars:
  uv run python scripts/ocr_sidecar.py --staged

Config lives at ~/.config/sm-alpr/pra_config.json.
Auth state lives at ~/.config/sm-alpr/pra_auth.json (not in repo).
"""

import functools
print = functools.partial(print, flush=True)

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import fitz  # pymupdf, for PDF text fingerprinting

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page


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
                      fallback_name: str | None = None) -> Path | None:
    """Click `element`; handle whichever of these the portal returns:
       (a) Content-Disposition: attachment -> Chromium download event
       (b) inline PDF in a new tab -> popup with URL we fetch via request API
       (c) direct href on the anchor -> fetch URL straight via request API.

    Returns the saved path on success, else None.
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
            print(f"   click error: {exc}")
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
        out = target_path if target_path else (folder / sanitize(name))
        try:
            dl.save_as(str(out))
        except Exception as exc:
            print(f"   download.save_as failed: {exc}")
            out = None
        # Close any popup we opened on the way.
        for p in extra_popups:
            try:
                p.close()
            except Exception:
                pass
        return out

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


def _write_pdf(resp, url: str,
               target_path: Path | None,
               folder: Path | None,
               fallback_name: str | None) -> Path:
    new_bytes = resp.body()
    if target_path is not None:
        out = target_path
    else:
        name = filename_from_response(resp, url) or fallback_name or "unnamed.pdf"
        assert folder is not None, "folder required when target_path is None"
        out = folder / sanitize(name)

    # Skip rewriting if the existing file has byte-identical content OR
    # text-content equivalent (same semantic text, only PDF metadata differs).
    # Lets --all runs refresh MH PDFs without spamming git with metadata churn.
    if out.exists():
        existing_bytes = out.read_bytes()
        if existing_bytes == new_bytes:
            return out
        new_fp = pdf_text_fingerprint(new_bytes)
        old_fp = pdf_text_fingerprint(existing_bytes)
        if new_fp is not None and new_fp == old_fp:
            return out  # same text content; leave the on-disk file untouched

    out.write_bytes(new_bytes)
    return out


REPO = Path(__file__).resolve().parent.parent
PRA_ROOT = REPO / "assets" / "san-mateo-public-records"
CONFIG_DIR = Path.home() / ".config" / "sm-alpr"
AUTH_STATE = CONFIG_DIR / "pra_auth.json"
CONFIG_PATH = CONFIG_DIR / "pra_config.json"

REQUEST_ID_RE = re.compile(r"^W\d{6}-\d{6}$")
SESSION_TOKEN_RE = re.compile(r"/\(S\([^)]+\)\)/")
SSESSIONID_RE = re.compile(r"[?&]sSessionID=[^&]*")
DOWNLOAD_TIMEOUT_MS = 60_000  # portal pre-signs S3 URLs; can take several seconds
NAV_TIMEOUT_MS = 20_000


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
            print(f"   attachment scope: {sel} ({len(found)} anchors)")
            # Dump a sample so we can see the DOM pattern (text / href / onclick).
            for i, a in enumerate(found[:6]):
                txt = (a.inner_text() or "").strip()[:60]
                href = (a.get_attribute("href") or "")[:100]
                onclick = (a.get_attribute("onclick") or "")[:100]
                print(f"     sample[{i}] text={txt!r} href={href!r} onclick={onclick!r}")
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
        print(f"   search form setup failed: {exc}")
        return False

    clicked = False
    for sel in GO_BUTTON_SELECTORS:
        loc = page.locator(sel).first
        if loc.count() == 0:
            continue
        print(f"   clicking Go via {sel}")
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
            print(f"   click via {sel} failed: {exc}")
            continue

    if not clicked:
        # DevExpress client-side fallback.
        try:
            key = page.evaluate(CLICK_GO_DEVEX_JS)
        except Exception as exc:
            print(f"   DevExpress Go fallback failed: {exc}")
            key = None
        if key:
            print(f"   clicked Go via DevExpress client object: {key}")
            try:
                page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
            except PWTimeout:
                pass
            clicked = True

    if not clicked:
        print("   could not trigger 'Go' button")
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
    print(f"   [diag] url={page.url}")
    try:
        title = page.title()
        print(f"   [diag] title={title!r}")
    except Exception:
        pass
    try:
        text = page.evaluate("() => (document.body && document.body.innerText) || ''")
        snippet = " | ".join(
            line.strip() for line in text.splitlines() if line.strip()
        )[:500]
        print(f"   [diag] body text: {snippet}")
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
            print(f"   navigating to: {target}")
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
        print(f"   could not navigate to My Requests list from {home_url}")
        return False

    page_num = 1
    while page_num <= max_pages:
        locator, sel = locate_request_on_page(page, request_id, button_label)
        if locator is not None:
            if page_num > 1:
                print(f"   found on page {page_num} via {sel}")
            else:
                print(f"   matched selector: {sel}")
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

    # Diagnostic dump — help us iterate on selectors.
    anchors = page.query_selector_all("a")
    print(f"   could not locate '{request_id}' after {page_num} page(s). "
          f"last page has {len(anchors)} anchors; first 30:")
    for a in anchors[:30]:
        text = (a.inner_text() or "").strip()
        href = a.get_attribute("href") or ""
        if text or href:
            print(f"     a[{text[:60]!r}] href={href[:100]}")
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
        print(f"   could not reach requests list")
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
            print(f"   {button_label} button not attached after search")
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
            print(f"   [diag] no nav after {button_label} click; "
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
    print(f"→ {request_id}: {len(existing)} existing PDF(s)")

    if not _goto_request_section(page, home_url, request_id, "details"):
        print(f"   could not open Details for {request_id} — skipping")
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
            print(f"   WARN: no Print Messages (PDF) link found")
        else:
            saved = save_pdf_via_click(page, mh_link, target_path=mh_target)
            if saved:
                print(f"     + {mh_target.name}")
            else:
                print(f"   ERR: Print Messages fetch failed")

    # Attachments
    if do_files:
        anchors = _get_attachment_anchors(page)
        if not anchors:
            print(f"   0 attachment anchors")
            _dump_page_diag(page)
            return
        print(f"   {len(anchors)} attachment anchor(s)")
        downloaded = skipped = failed = 0
        # Track by label to survive DOM re-render between clicks.
        processed: set[str] = set()
        while True:
            current = _get_attachment_anchors(page)
            target_el = None
            target_label = ""
            for a in current:
                lbl = (a.inner_text() or "").strip()
                if lbl in processed or not lbl:
                    continue
                target_el = a
                target_label = lbl
                break
            if target_el is None:
                break
            processed.add(target_label)

            if target_label in existing or sanitize(target_label) in existing:
                skipped += 1
                continue

            saved = save_pdf_via_click(page, target_el, folder=folder,
                                       fallback_name=target_label)
            if saved is None:
                failed += 1
                continue
            existing.add(saved.name)
            print(f"     + {saved.name}")
            downloaded += 1

        print(f"   downloaded {downloaded}, skipped {skipped}, {failed} failed")


def run(targets: list[str], config: dict, headed: bool,
        do_files: bool, do_messages: bool) -> None:
    home_url = config.get("support_home_url")
    if not home_url:
        print("No support home URL configured. Run --login first.", file=sys.stderr)
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            storage_state=str(AUTH_STATE), accept_downloads=True,
            viewport={"width": 1280, "height": 1600},
        )
        page = context.new_page()

        # Prime: visit home, check we're actually logged in.
        page.goto(home_url, wait_until="domcontentloaded")
        body = (page.content() or "").lower()
        if "login" in page.url.lower() or "sign in" in body[:5000]:
            print("session appears expired — re-run with --login", file=sys.stderr)
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
    parser.add_argument("--all", action="store_true",
                        help="Iterate every W* folder (default when no ids given)")
    parser.add_argument("--headed", action="store_true",
                        help="Show the browser window (default: headless)")
    parser.add_argument("--files-only", action="store_true",
                        help="Only download attachments, skip message history")
    parser.add_argument("--messages-only", action="store_true",
                        help="Only refresh message-history PDFs, skip attachments")
    parser.add_argument("requests", nargs="*",
                        help="Specific request ids (e.g. W012297-030826)")
    args = parser.parse_args()

    if args.login:
        return login()

    if not AUTH_STATE.exists():
        print("No saved auth. Run: uv run python scripts/pra_download.py --login",
              file=sys.stderr)
        return 1

    config = load_config()
    targets = list(args.requests)
    if args.all or not targets:
        targets = discover_requests()
    if not targets:
        print("No target requests.", file=sys.stderr)
        return 1

    do_files = not args.messages_only
    do_messages = not args.files_only
    run(targets, config, headed=args.headed,
        do_files=do_files, do_messages=do_messages)

    print()
    print("Done. To OCR newly downloaded files:")
    print("  uv run python scripts/ocr_sidecar.py --staged")
    return 0


if __name__ == "__main__":
    sys.exit(main())

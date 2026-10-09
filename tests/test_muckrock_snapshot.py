"""Tests for scripts/muckrock_snapshot.py: parsing, Cloudflare email decoding,
redaction, and change detection, on a synthetic request page (no network)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import muckrock_snapshot as ms  # noqa: E402


def cfemail(addr: str, key: int = 0x5A) -> str:
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in addr)


def comm(cid, sender, when, subject, channel, body, files=""):
    return f"""
<section class=" collapsable communication textbox  " id="comm-{cid}">
  <header class="textbox__header communication-header">
    <p class="from">From: {sender}</p>
    <div class="actionables nocollapse"><a href="#comm-{cid}" class="permalink">
      <time datetime="{when}" class="date">x</time></a></div>
  </header>
  <section class="textbox__section communication-metadata">
    <p class="small subject">Subject: {subject}</p>
    <span><span class="small badge">
      {channel}
    </span></span>
  </section>
  <section class="textbox__section communication-body">{body}</section>
  {files}
</section>"""


AGENCY_FILE = """<ul class="files"><li>
<div class="file" data-title="Response Letter.pdf"><div class="file-info">
<a href="https://cdn.muckrock.com/foia_files/2026/10/08/Response_Letter.pdf" class="action">Download</a>
</div></div></li></ul>"""

PAGE = f"""<html><body>
<h1 class="state">Test request title</h1>
<a href="/agency/some-city-1/some-police-department-2/">Some Police Department</a>
<table class="numbers"><tr class="tracking-number"><td>Tracking #</td><td><p>P000001-010126</p></td></tr></table>
<table class="dates">
  <tr class="submitted"><td class="label">Submitted</td><td class="date">Sept. 25, 2026</td></tr>
  <tr class="due"><td class="label">Due</td><td class="date failure">Oct. 5, 2026</td></tr>
  <tr class="estimated-completion"><td class="label">Est. Completion</td><td class="date"> None </td></tr>
</table>
<section class="status manager" id="request-status">Status <span>Awaiting Response</span></section>
<div class="communications-list">
{comm(1, "Pat Requesterson", "2026-09-25T10:00:00-04:00", "CPRA request", "Portal",
      "<p>To Whom It May Concern:</p><p>Please send records.<br>Sincerely,<br>Pat Requesterson</p>")}
{comm(2, "Some Police Department", "2026-10-07T17:00:00-04:00", "RE: CPRA request", "Email",
      "<p>Dear Pat Requesterson:</p><p>Mr. Requesterson, contact "
      f'<a href="/cdn-cgi/l/email-protection" class="__cf_email__" data-cfemail="{cfemail("records@city.gov")}">[email&#160;protected]</a>'
      ".<br>Upload documents directly: https://u1.ct.sendgrid.net/ls/click?upn=SECRET<br>"
      "Reply to 99999-12345678@requests.muckrock.com</p>",
      files=AGENCY_FILE)}
</div>
<section role="tabpanel" class="tab-panel files" id="files">
  <div class="file" data-title="should-not-be-a-communication.pdf"></div>
</section>
</body></html>"""

URL = "https://www.muckrock.com/foi/some-city-1/test-request-123456/#"


def parsed():
    return ms.redact(ms.parse_request(PAGE, URL))


def test_request_fields():
    r = parsed()
    assert r["muckrock_id"] == 123456
    assert r["url"] == "https://www.muckrock.com/foi/some-city-1/test-request-123456/"
    assert r["title"] == "Test request title"
    assert r["agency"] == "Some Police Department"
    assert r["agency_tracking_number"] == "P000001-010126"
    assert r["status"] == "Awaiting Response"


def test_dates_and_overdue_flag():
    r = parsed()
    assert r["dates"] == {
        "submitted": "Sept. 25, 2026",
        "due": "Oct. 5, 2026",
        "estimated-completion": "None",
    }
    assert r["overdue"] == ["due"]


def test_communications_and_attachments():
    c = parsed()["communications"]
    assert [x["id"] for x in c] == ["comm-1", "comm-2"]
    assert c[1]["from"] == "Some Police Department"
    assert c[1]["channel"] == "Email"
    assert c[1]["datetime"] == "2026-10-07T17:00:00-04:00"
    assert c[1]["attachments"] == [{
        "title": "Response Letter.pdf",
        "url": "https://cdn.muckrock.com/foia_files/2026/10/08/Response_Letter.pdf",
    }]


def test_cloudflare_email_decoded():
    assert "records@city.gov" in parsed()["communications"][1]["body"]


def test_redactions():
    r = parsed()
    text = ms.thread_text(r)
    assert "Requesterson" not in text
    assert r["communications"][0]["from"] == "Pat"
    assert "Dear Pat:" in r["communications"][1]["body"]
    assert "Mr. [surname redacted]" in r["communications"][1]["body"]
    assert "SECRET" not in text and "sendgrid" not in text
    assert "[MuckRock upload link removed]" in text
    assert "requests.muckrock.com" not in text
    assert "[MuckRock request address]" in text


def test_thread_hash_stable():
    assert ms.thread_text(parsed()) == ms.thread_text(parsed())

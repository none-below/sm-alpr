# Daly City, CA — reference documents

Daly City City Council, June 8, 2026, consent item: "Authorize the City Manager to Execute an Amendment to the Police Department's Axon Agreement to Include their Automated License Plate Reader Cameras" (Resolution 26-108).

| File | Source URL | Filename date basis | Fetched (UTC) | sha256 |
|---|---|---|---|---|
| `Axon_ALPR_Amendment_Agenda_Report_2026-06-08.pdf` | **Excerpt, PDF pages 106–107** of the agenda packet https://dalycityca.api.civicclerk.com/v1/Meetings/GetMeetingFileStream(fileId=5379,plainText=false) (192 pp.) | Meeting date printed on the agenda report | 2026-10-16 | `fae80f05216be3fbf3c8ad96491258f493ce85c240d1a1271dc159f0f6c5c73d` |
| `Council_Minutes_Excerpt_Resolution_26-108_2026-06-08.pdf` | **Excerpt, PDF page 3** of the minutes https://dalycityca.api.civicclerk.com/v1/Meetings/GetMeetingFileStream(fileId=5415,plainText=false) (7 pp., image-only) | Meeting date | 2026-10-16 | `ebd4c9c23449e12a97c892c48f91c4b88ad571dfa1c0ae9b016b0a70b3ac9fa5` |

What each file is cited for:

- `Axon_ALPR_Amendment_Agenda_Report_2026-06-08.pdf`: p. 1–2: "no-cost pilot of Axon's Fixed ALPR solutions through June of 2027"; first payment of $120,000 due June 2027; "9 donor Outpost cameras"; "In total we are receiving 99 Outpost cameras and 6 Lightpost PTZ"
- `Council_Minutes_Excerpt_Resolution_26-108_2026-06-08.pdf`: Resolution 26-108 adopted on a voice vote (moved DiGiovanni, seconded Sylvester); Mayor asks for an ORT program report

Full sources (not committed): packet sha256 `637f763c9c8b577e1541c2d4e721793eb06eb94242cca5b31824c8e9455a115e` (24,526,028 bytes); minutes sha256 `e9abf976884b2002bef5b297fa1d3badd1629a5404d115a691592ad655defa61` (6,969,785 bytes). Earlier notes cited "packet pp. 105–107"; PDF p. 105 is the end of an unrelated item (Teleconference Disruption Policy), so only pp. 106–107 are the Axon agenda report.

Excerpts were cut with `qpdf --empty --pages <packet> <range> --`; page numbers above are PDF page numbers. Files fetched 2026-10-22 were downloaded with `curl_cffi` (`impersonate="chrome"`); files fetched 2026-10-16 come from a local evidence snapshot taken that day, and rows marked re-fetched were downloaded again on 2026-10-22 and matched byte-for-byte. Text sidecars (`*.pdf.<hash>.txt`) are generated separately with `scripts/ocr_sidecar.py`.

# Kennewick, WA — reference documents

Kennewick City Council, December 2, 2025: Axon agreement amendment with Axon quote Q-753565-45981CN.

| File | Source URL | Filename date basis | Fetched (UTC) | sha256 |
|---|---|---|---|---|
| `Axon_Quote_Q-753565_2025-11-20.pdf` | **Excerpt, PDF pages 188–207** of the council packet https://kennewickwa.api.civicclerk.com/v1/Meetings/GetMeetingFileStream(fileId=3090,plainText=false) (431 pp.). The full 20-page quote | Quote issue date ("Issued: 11/20/2025") | 2026-10-16 | `40970d3a3344a0e0334824f8377681aa5b5245695e1677f27fc781ef65946f51` |

What each file is cited for:

- `Axon_Quote_Q-753565_2025-11-20.pdf`: excerpt p. 3 (packet p. 190): B00063 BUNDLE - OUTPOST WITH TAP 10YR, 40 × 114 mo, list $265.74, net $0.00; 102182 OUTPOST BUNDLE TRUE-UP × 40, net $0.00

Full source packet (not committed): sha256 `9e5e3603da0866502178b6d356ebf617220849fee8985aa65972c4729913550d`.

Excerpts were cut with `qpdf --empty --pages <packet> <range> --`; page numbers above are PDF page numbers. Files fetched 2026-10-22 were downloaded with `curl_cffi` (`impersonate="chrome"`); files fetched 2026-10-16 come from a local evidence snapshot taken that day, and rows marked re-fetched were downloaded again on 2026-10-22 and matched byte-for-byte. Text sidecars (`*.pdf.<hash>.txt`) are generated separately with `scripts/ocr_sidecar.py`.

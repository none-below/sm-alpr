# Ferndale, MI — reference documents

Ferndale City Council, December 15, 2025: Request for Council Action "Agreement with Axon for Automated License Plate Reader Cameras", and the Axon quote attached to the item.

| File | Source URL | Filename date basis | Fetched (UTC) | sha256 |
|---|---|---|---|---|
| `Axon_Quote_Q-775929_2025-11-13.pdf` | Whole file. https://ferndalemi.granicus.com/services/legistar/download/pdf/3746370/Outpost_with_Tap__Ferndale_.pdf (redirects to a time-limited S3 link) | Quote issue date ("Issued: 11/13/2025") | 2026-10-16 (re-fetched 2026-10-22; identical sha256) | `1d5ad6f82b3a864576ca6f04fd222b52f210722c4d87fc47690b911214ccd162` |
| `Axon_ALPR_Request_for_Council_Action_2025-12-15.pdf` | Whole file. https://d2kbkoa27fdvtw.cloudfront.net/ferndalemi/3a61c77138b998593c5f6813afa039df0.pdf | Date printed on the Request for Council Action | 2026-10-16 (re-fetched 2026-10-22; identical sha256) | `e00d41631815f29b18e65076d2865a23c9c2b3abb63e341d8e354b726460b554` |

What each file is cited for:

- `Axon_Quote_Q-775929_2025-11-13.pdf`: p. 3: B00061 BUNDLE - OUTPOST WITH TAP, 9 × 60 mo, list $249.92, net $199.94
- `Axon_ALPR_Request_for_Council_Action_2025-12-15.pdf`: p. 2: "The first payment would be deferred to April of 2027 and consist of four payments of $27,741.90 per year."

Excerpts were cut with `qpdf --empty --pages <packet> <range> --`; page numbers above are PDF page numbers. Files fetched 2026-10-22 were downloaded with `curl_cffi` (`impersonate="chrome"`); files fetched 2026-10-16 come from a local evidence snapshot taken that day, and rows marked re-fetched were downloaded again on 2026-10-22 and matched byte-for-byte. Text sidecars (`*.pdf.<hash>.txt`) are generated separately with `scripts/ocr_sidecar.py`.

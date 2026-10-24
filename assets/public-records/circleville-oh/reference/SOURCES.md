# Circleville, OH — reference documents

City of Circleville Ordinance 10-76-2025 authorizing a renewal contract with Axon, with Exhibit A, Axon non-binding budgetary estimate Q-749055-45927CS; and the October 7, 2025 Safety Committee minutes discussing it.

| File | Source URL | Filename date basis | Fetched (UTC) | sha256 |
|---|---|---|---|---|
| `Ordinance_10-76-2025_with_Axon_Quote_Q-749055_2025-09-27.pdf` | **Excerpt, PDF pages 1–5** of https://circlevilleoh.gov/wp-content/uploads/2025/10/10.76.2025.pdf (23 pp., scanned). pp. 1–2 ordinance; pp. 3–5 first three pages of the 21-page estimate | Estimate issue date ("Issued 09/27/2025"); the ordinance's passage date is handwritten and not legible in the text layer | 2026-10-16 (re-fetched 2026-10-22; identical sha256) | `176c3e3640a6f3734500b3c5eb82259a6b641a607d68712395e6fafaf64d9272` |
| `Safety_Committee_Minutes_2025-10-07.pdf` | Whole file. https://circlevilleoh.gov/wp-content/uploads/2025/10/10.7.2025-Safety-Minutes.pdf | Meeting date | 2026-10-16 (re-fetched 2026-10-22; identical sha256) | `a2991de8b688e2fb037074616ba2e74b30fcc513b8a26da655fd73cea5dc9700` |

What each file is cited for:

- `Ordinance_10-76-2025_with_Axon_Quote_Q-749055_2025-09-27.pdf`: p. 5: B00063 BUNDLE - OUTPOST WITH TAP 10YR, 2 × 120 mo, list $265.74, net $0.00 (the scan's text layer reads the SKU as "800063")
- `Safety_Committee_Minutes_2025-10-07.pdf`: p. 1: "... apart from the drone, outpost cameras, and the VR headsets that they would be receiving at no additional cost, and Fisher confirmed that is correct."

Full ordinance PDF (not committed): sha256 `e90039cffefcc56ce9aefabcd0544aca17f8476f5316487b2bd32ed71614d4d4` (6,494,773 bytes).

Excerpts were cut with `qpdf --empty --pages <packet> <range> --`; page numbers above are PDF page numbers. Files fetched 2026-10-22 were downloaded with `curl_cffi` (`impersonate="chrome"`); files fetched 2026-10-16 come from a local evidence snapshot taken that day, and rows marked re-fetched were downloaded again on 2026-10-22 and matched byte-for-byte. Text sidecars (`*.pdf.<hash>.txt`) are generated separately with `scripts/ocr_sidecar.py`.

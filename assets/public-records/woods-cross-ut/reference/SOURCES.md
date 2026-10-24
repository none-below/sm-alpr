# Woods Cross, UT — reference documents

Woods Cross City Council, July 21, 2026: Resolution 2026-1008 approving purchase of Axon license plate readers, TASER 10s and contract consolidation. The packet carries two versions of Axon quote Q-835693: the April 29, 2026 "Original Contract" and the July 15, 2026 "Updated Contract".

| File | Source URL | Filename date basis | Fetched (UTC) | sha256 |
|---|---|---|---|---|
| `Resolution_2026-1008_Axon_Memo_and_Quotes_Q-835693_2026-07-21.pdf` | **Excerpt, PDF pages 46–75** of the council packet https://www.utah.gov/pmn/files/1463133.pdf (197 pp.; extracted with `qpdf --pages`). Packet p. 46 chief's memo (07/14/2026); p. 47 Resolution 2026-1008; pp. 48–61 quote Q-835693-46141AM issued 04/29/2026; pp. 62–75 quote Q-835693-46218AM issued 07/15/2026 | Council meeting date | 2026-10-16 | `c3f8f83e7347e69e74c1e0e760f0a945fe2ae77a074090ddb89ced5218b19e86` |

What each file is cited for:

- `Resolution_2026-1008_Axon_Memo_and_Quotes_Q-835693_2026-07-21.pdf`: excerpt p. 6 (packet p. 51), April quote: B00085 LIGHTPOST TAP PLAN 1 × 120 mo net $0.00; B00076 OUTPOST TAP PLAN 1 × 120 mo net $0.00. Excerpt p. 20 (packet p. 65), July quote: B00085 LIGHTPOST TAP PLAN 1 × 120 mo net $0.00 (list $500.65); B00076 OUTPOST TAP PLAN 1 × 120 mo, list $264.97, net $132.49

Full source packet (not committed): sha256 `8d1002785632b3eeb56dc7872c7b35b114e5c875e66d51ca4786e6cb7b15e9c0`.

Excerpts were cut with `qpdf --empty --pages <packet> <range> --`; page numbers above are PDF page numbers. Files fetched 2026-10-22 were downloaded with `curl_cffi` (`impersonate="chrome"`); files fetched 2026-10-16 come from a local evidence snapshot taken that day, and rows marked re-fetched were downloaded again on 2026-10-22 and matched byte-for-byte. Text sidecars (`*.pdf.<hash>.txt`) are generated separately with `scripts/ocr_sidecar.py`.

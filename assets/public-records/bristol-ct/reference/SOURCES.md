# Bristol, CT — reference documents

Bristol Board of Finance regular meeting, September 22, 2026, item "Approval of a bid waiver to Axon": the Police Chief's sole-source request letter (August 20, 2026) for 14 LPR cameras and Axon quote Q-865784-46253JK.

| File | Source URL | Filename date basis | Fetched (UTC) | sha256 |
|---|---|---|---|---|
| `PD_Sole_Source_Letter_and_Axon_Quote_Q-865784_2026-08-20.pdf` | **Excerpt, PDF pages 73–80** of the Board of Finance packet https://bristolct.api.civicclerk.com/v1/Meetings/GetMeetingFileStream(fileId=13528,plainText=false) (231 pp.). Packet p. 73 chief's letter (scanned); pp. 74–80 quote, issued 08/19/2026 | Date of the chief's letter | 2026-10-16 (re-fetched 2026-10-22; identical sha256) | `a302f3802dcc86a32dc69c64ba6603bfeafa54c1a384a4ac243f2d988fc28bc6` |

What each file is cited for:

- `PD_Sole_Source_Letter_and_Axon_Quote_Q-865784_2026-08-20.pdf`: excerpt p. 4 (packet p. 76): B00076 Outpost TAP Plan, 14 × 54 mo, list $249.20, net $221.43; 103070 Outpost TAP Bundle True-Up, 14 × 6 mo, net $57.12

Full source packet (not committed): sha256 `f97e86adb5c935960b428e71dc46a53e18fda99da0c80f30db6bb59217dda597` (20,006,561 bytes).

Excerpts were cut with `qpdf --empty --pages <packet> <range> --`; page numbers above are PDF page numbers. Files fetched 2026-10-22 were downloaded with `curl_cffi` (`impersonate="chrome"`); files fetched 2026-10-16 come from a local evidence snapshot taken that day, and rows marked re-fetched were downloaded again on 2026-10-22 and matched byte-for-byte. Text sidecars (`*.pdf.<hash>.txt`) are generated separately with `scripts/ocr_sidecar.py`.

# Duncanville, TX — reference documents

Duncanville City Council regular meeting, September 1, 2026: acceptance of the MVCPA FY2027 grant, with Axon quote Q-895527-002 ("Axon- 10 Outpost + 2 Docks + 1 extra parachute plan").

| File | Source URL | Filename date basis | Fetched (UTC) | sha256 |
|---|---|---|---|---|
| `Axon_Quote_Q-895527-002_2026-08-24.pdf` | **Excerpt, PDF pages 133–137** of the council packet https://duncanvilletx.api.civicclerk.com/v1/Meetings/GetMeetingFileStream(fileId=7086,plainText=false) (390 pp.) | Quote issue date ("Issued Aug 24, 2026") | 2026-10-22 | `ddff752c0d887e2de09cf758e06597c9e55b7a7a9649eaca455803cbe00bfde8` |

What each file is cited for:

- `Axon_Quote_Q-895527-002_2026-08-24.pdf`: excerpt p. 2: B00076 Outpost TAP Plan, 10 × 60 mo, unbundled $366.09, list column $366.07, net $199.36

Full source packet (not committed): sha256 `c769e53cc42d5352fbd6c32747861fc36bb21d68af58528a7958d359fdae7e99` (43,519,730 bytes). The staff report for the item is at packet pp. 130–132 (not committed).

Excerpts were cut with `qpdf --empty --pages <packet> <range> --`; page numbers above are PDF page numbers. Files fetched 2026-10-22 were downloaded with `curl_cffi` (`impersonate="chrome"`); files fetched 2026-10-16 come from a local evidence snapshot taken that day, and rows marked re-fetched were downloaded again on 2026-10-22 and matched byte-for-byte. Text sidecars (`*.pdf.<hash>.txt`) are generated separately with `scripts/ocr_sidecar.py`.

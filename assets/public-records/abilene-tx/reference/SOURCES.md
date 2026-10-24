# Abilene, TX — reference documents

Abilene City Council, October 8, 2026, item 9 (motion to reconsider the earlier resolution): contract with Axon for body-worn, in-car and interview-room cameras, TASERs and related equipment. Axon quote Q-863523-016.

| File | Source URL | Filename date basis | Fetched (UTC) | sha256 |
|---|---|---|---|---|
| `Axon_Quote_Q-863523-016_2026-09-30.pdf` | **Excerpt, PDF pages 101–109** of the council packet https://abilenetx.api.civicclerk.com/v1/Meetings/GetMeetingFileStream(fileId=7267,plainText=false) (239 pp.). The full 9-page quote | Quote issue date ("Issued Sep 30, 2026") | 2026-10-16 | `390d70645d04121f1d980c9661e4a7874ac31f56e2b43a23eb975d9e7f5047b7` |

What each file is cited for:

- `Axon_Quote_Q-863523-016_2026-09-30.pdf`: excerpt p. 4 (packet p. 104): 102032 Axon Outpost - Camera × 28. Excerpt p. 6 (packet p. 106): under B00089 OSP-Unlimited Plus (No VR) × 229, B00076 Outpost TAP Plan × 28, with no Outpost price row. Excerpt p. 7: 102142 ALPR license × 28

Full source packet (not committed): sha256 `f6d5475b497f34fa1eeaa1b005d730a47cd8a921a0d8b0e8fafb79683b3ce34e` (38,348,463 bytes). Earlier notes cited pp. 101–107; the quote runs to p. 109 ("Page 1 of 9").

Excerpts were cut with `qpdf --empty --pages <packet> <range> --`; page numbers above are PDF page numbers. Files fetched 2026-10-22 were downloaded with `curl_cffi` (`impersonate="chrome"`); files fetched 2026-10-16 come from a local evidence snapshot taken that day, and rows marked re-fetched were downloaded again on 2026-10-22 and matched byte-for-byte. Text sidecars (`*.pdf.<hash>.txt`) are generated separately with `scripts/ocr_sidecar.py`.

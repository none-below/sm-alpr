# Glenwood Springs, CO — reference documents

Glenwood Springs City Council regular meeting, June 4, 2026: Axon agreement with Axon quote Q-840238-46154AP.

| File | Source URL | Filename date basis | Fetched (UTC) | sha256 |
|---|---|---|---|---|
| `Axon_Quote_Q-840238_2026-05-12.pdf` | **Excerpt, PDF pages 308–324** of the council packet https://glenwoodspringsco.api.civicclerk.com/v1/Meetings/GetMeetingFileStream(fileId=8156,plainText=false) (328 pp.). The full 17-page quote | Quote issue date ("Issued: 05/12/2026") | 2026-10-22 | `440a0fbfd1f71541a4ab3fc619107029340e3763e65fbce2c3dbbbe0f42111e3` |

What each file is cited for:

- `Axon_Quote_Q-840238_2026-05-12.pdf`: excerpt p. 3 (packet p. 310): B00076 OUTPOST TAP PLAN, 4 × 120 mo, list $264.97, net $0.00

Full source packet (not committed): sha256 `aff92522673dd17073030e06c1fe96521fbb59017ba3ed783de699ccb4d86c30` (22,997,939 bytes). Axon's quote-overview slides precede the quote at packet pp. 299–307 (not committed).

Excerpts were cut with `qpdf --empty --pages <packet> <range> --`; page numbers above are PDF page numbers. Files fetched 2026-10-22 were downloaded with `curl_cffi` (`impersonate="chrome"`); files fetched 2026-10-16 come from a local evidence snapshot taken that day, and rows marked re-fetched were downloaded again on 2026-10-22 and matched byte-for-byte. Text sidecars (`*.pdf.<hash>.txt`) are generated separately with `scripts/ocr_sidecar.py`.

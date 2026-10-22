# San Bernardino, CA — reference documents

San Bernardino Mayor and City Council, May 6, 2026, consent calendar: Request for Council Action to authorize a Professional Services Agreement with Axon Enterprise (not to exceed $4,150,000). Exhibit A to the agreement is Axon quote Q-800742-46086LG.

| File | Source URL | Filename date basis | Fetched (UTC) | sha256 |
|---|---|---|---|---|
| `Axon_Quote_Q-800742_2026-03-05.pdf` | **Excerpt, PDF pages 1523–1537** of the compiled packet https://sanbernardino.primegov.com/Public/CompiledDocument?meetingTemplateId=7176&compileOutputType=1 (2,129 pp.; redirects to a time-limited pgwest.blob.core.windows.net link for `Packet_20260429095326326.pdf`). Packet p. 1523 Exhibit A cover; pp. 1524–1537 quote | Quote issue date ("Issued: 03/05/2026") | 2026-10-22 | `74965291b316aad1565ae8c5646207dea96d36b1f5a27e1a5be040a3d80fb2ad` |

What each file is cited for:

- `Axon_Quote_Q-800742_2026-03-05.pdf`: excerpt p. 4 (packet p. 1526): B00076 OUTPOST TAP PLAN, 28 × 60 mo, list $249.20, net $199.36

Full source packet (not committed): sha256 `57473809512da98d4b1911694dfae50757edeac33778c4e6f90ddefbf5be67e9` (230,208,859 bytes). The Request for Council Action itself starts at packet p. 1501 (not committed).

Excerpts were cut with `qpdf --empty --pages <packet> <range> --`; page numbers above are PDF page numbers. Files fetched 2026-10-22 were downloaded with `curl_cffi` (`impersonate="chrome"`); files fetched 2026-10-16 come from a local evidence snapshot taken that day, and rows marked re-fetched were downloaded again on 2026-10-22 and matched byte-for-byte. Text sidecars (`*.pdf.<hash>.txt`) are generated separately with `scripts/ocr_sidecar.py`.

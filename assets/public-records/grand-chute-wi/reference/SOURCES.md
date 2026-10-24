# Grand Chute, WI — reference documents

Town of Grand Chute Town Board, June 2, 2026: early termination of the Flock Safety agreements, and an agreement with Axon for 23 fixed ALPR cameras (Axon quote Q-853193-46161JT).

| File | Source URL | Filename date basis | Fetched (UTC) | sha256 |
|---|---|---|---|---|
| `Axon_Agreement_Memo_and_Quote_Q-853193_2026-06-02.pdf` | **Excerpt, PDF pages 144–154** of the Town Board packet https://www.grandchutewi.gov/i/agendas/town-board/pkt_tb_6-2-26.pdf (176 pp.). Packet pp. 144–146 Police Chief's memo; pp. 147–154 quote, issued 05/19/2026 | Meeting date | 2026-10-16 | `eead2375a661bba4ba30edd3732f70d2768941299201db3655e5936188377a7b` |
| `Flock_Early_Termination_Memo_2026-06-02.pdf` | **Excerpt, PDF pages 115–118** of the same packet | Meeting date | 2026-10-16 | `5397e338bb24e782a9e4ef2b41eb080ba3f576b962d4f2d81ce471b9d247694f` |

What each file is cited for:

- `Axon_Agreement_Memo_and_Quote_Q-853193_2026-06-02.pdf`: excerpt p. 3 (packet p. 146): "they have agreed to absorb the early termination fees ($11,500) from Flock incurred by the Town". Excerpt p. 6 (packet p. 149): B00076 OUTPOST TAP PLAN, 23 × 60 mo, list $249.20, net $199.12
- `Flock_Early_Termination_Memo_2026-06-02.pdf`: Police Chief's memo recommending early termination of the Flock agreements

Full source packet (not committed): sha256 `ec86f1d1eedcff4702939ca323eba84ee36a6bf45a57e8ecc0a3b5060ada2c84` (25,760,549 bytes).

Excerpts were cut with `qpdf --empty --pages <packet> <range> --`; page numbers above are PDF page numbers. Files fetched 2026-10-22 were downloaded with `curl_cffi` (`impersonate="chrome"`); files fetched 2026-10-16 come from a local evidence snapshot taken that day, and rows marked re-fetched were downloaded again on 2026-10-22 and matched byte-for-byte. Text sidecars (`*.pdf.<hash>.txt`) are generated separately with `scripts/ocr_sidecar.py`.

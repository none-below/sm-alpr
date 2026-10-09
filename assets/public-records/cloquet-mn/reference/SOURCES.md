# Cloquet, MN — reference documents

City of Cloquet, Minnesota, City Council regular meeting of **February 17, 2026**, item "Authorize Purchase of
Axon Draft One AI Assisted Report Writing Software" (Cloquet Police Department). Includes Axon quote
Q-801219-46064KP.

| File | Source URL | Filename date basis | Fetched (UTC) | sha256 |
|---|---|---|---|---|
| `Axon_Draft_One_Request_for_Council_Action_2026-02-17.pdf` | **Excerpt, PDF pages 69–85** of the full council packet at https://www.cloquetmn.gov/home/showpublisheddocument/7362/639065931669000000 (103 pp.; extracted with `qpdf --pages`) | Date printed on the Request for Council Action | 2026-10-09T04:53Z | `406b40f7d2b788d901bca94c689da07ebb1b56913b0f8faa065edb43bf1338ca` |
| `Council_Minutes_2026-02-17.pdf` | https://www.cloquetmn.gov/home/showpublisheddocument/7402/639082366642730000 | Meeting date (minutes) | 2026-10-09T04:53Z | `6c31bf7af07fe8520e23885d527fbef00ccfc39e9f96f761faec2817f82c1fc6` |

Full source packet (not committed; 22 MB, mostly unrelated items): sha256
`7dc86e3dd090298fc757cd80b845c487b0aebbd74ef3aab43e79f39e2f2225c7`. The meeting agenda is also posted at
https://swagit-attachments.granicus.com/uploads/video/agenda_file/375380/2-17_CloquetCC.pdf (sha256
`52dbc390cca784fafb3aa1f4615588c5ce6c1e8883902a0e70fa99d0ab669232`, not committed).

cloquetmn.gov returns 403 to plain fetchers; the files were fetched with the repo `.venv` `curl_cffi`
(`impersonate="chrome"`). The packet pages are scanned images; text sidecars (`*.pdf.<hash>.txt`) were
generated with `scripts/ocr_sidecar.py`.

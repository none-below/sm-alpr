# San Jose PD — ALPR reference documents

Public City of San José documents, fetched 2026-10-08.
- sanjoseca.gov returns 403 to plain curl and WebFetch. Fetch it with curl_cffi `impersonate="chrome"`.
- Legistar attachments download with plain curl.

The date in each filename is the date named in the last column.

| File | Document | Source | Filename date | SHA-256 |
|---|---|---|---|---|
| `ALPR_Data_Usage_Protocol_2022-08-22.pdf` | ALPR Data Usage Protocol (DUP), "UPDATED as of August 22, 2022" (council-approved Aug 2022; superseded Duty Manual L 4207 per the 2026 memo) | https://www.sanjoseca.gov/home/showpublisheddocument/90909/638022325861770000 | document date on its cover | `cf846b6a9b934a1c174f31d40fc88debfb7fd0938d4197665265fd2d0317a50c` |
| `ALPR_DUP_Update_Memo_File_26-215_2026-02-09.pdf` | Chief Paul Joseph to Mayor and Council, "Automated License Plate Readers Data Usage Protocol Update", File 26-215, Item 4.1, agenda 3/10/26; includes the revised DUP as an attachment | https://legistar.granicus.com/sanjose/attachments/1e311deb-3542-4cd1-8212-d3452782c28f.pdf (Legistar matter 15601) | memo date | `9aa7953f1588238f6c2e5b9cf6f094dac190516d9bd1ff9df467170ca4de9c69` |
| `ALPR_Annual_Usage_Report_2023_2024-05-31.pdf` | Annual Data Usage Report, ALPRs, covering Jan–Dec 2023 (cover: May 2024) | https://www.sanjoseca.gov/home/showpublisheddocument/112472/638527656046970000 | city posting date (URL ticks + PDF CreationDate) | `d1973ffa24d9e1259b4c57112ca720ab8a2a62747951f7edb82c8694bbc7dac6` |
| `ALPR_Annual_Usage_Report_Aug-Dec_2022_2023-05-15.pdf` | Annual Usage Report, ALPRs, covering Aug–Dec 2022 | https://www.sanjoseca.gov/home/showpublisheddocument/97700/638197468521000000 | city posting date (URL ticks + PDF CreationDate) | `92f27d81baf871909699d064678467824ca3f1c8f882367869adc9dee8a505cd` |

As of 2026-10-08, no report for calendar 2024 or 2025 was found online. DUP §14 requires each report to reach the Digital Privacy Officer by March 1, and the officer to publish it within 90 days.

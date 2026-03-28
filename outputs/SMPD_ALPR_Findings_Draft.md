# SMPD ALPR Investigation — Findings

<!-- PDF GENERATOR NOTE: All [N] references in table cells should be converted to hyperlinks pointing to the corresponding entry in the Source Documents section. Display the cell text without bracket notation. Each cell contains at most one citation. Section-specific column headers (SOP 205, Policy 463, etc.) cite their own source implicitly and do not need links. -->
*Prepared March 16, 2026*

Every factual claim below is traceable to a specific document, email, or public record. Bracketed numbers (e.g., [1]) refer to the numbered Source Documents table at the end. In the digital version, source links are clickable. In print, QR codes are provided for key sources.

---

## Executive Summary

A review of public records and the department's own statements identified significant gaps between SMPD's ALPR program and its representations and obligations.

Council approved 55 Flock Safety devices across two actions (August 2023 and March 2024). Twelve of those devices had already been deployed and invoiced nine months before Council was asked to approve them. Two and a half years later, the department’s Flock Camera Manager reports 80. For over a decade, the department represented its ALPR program as regularly audited and compliant with policy. No record of any audit or compliance activity exists prior to December 2025. The program remains out of compliance with state law and the department’s own policies — despite explicit warnings from the Attorney General over two years ago.

This document presents eight findings across seven sections, grounded in the department's own records and statements. The findings do not argue for or against the ALPR program. They document the gap between what the department committed to and what it did.

### Key Findings

1. **283 agencies listed as having access — scope inconsistent with policy and statute.** The 2023 staff report told Council that SMPD shares with NCRIC and "allied County law enforcement agencies." As of February 2026, the Flock transparency page listed 283 organizations statewide, including University of the Pacific — a private university whose own Chief of Police confirmed it is not a public agency. UOP was removed from SMPD’s list after a resident raised the issue. [§6]

2. **No documented audits for 13+ years.** Policy 462/463 required audits "as described in SOP 205." SOP 205 specified quarterly audits — including review of search justifications and case number entries. O’Keefe confirmed no records exist before December 2025. She stated audits occurred through "roundtable discussions," but a PRA for meeting records returned no responsive documents. The audits that began in December 2025 review sharing configuration, not search activity compliance. A subsequent PRA specifically requesting search activity records confirmed these are the only audits that exist [§1] [32].

3. **Vendor contract authorizes sharing beyond what SMPD policy permits.** Contract § 5.3 grants Flock independent authority to disclose SMPD footage to law enforcement, government officials, and third parties — without SMPD approval or notification. No policy or audit process would surface a disclosure made under this provision. [§5]

4. **Statutory compliance: 1 of 11 required elements met.** Comparison of Policy 463 and SOP 205 against § 1798.90.51 (operator) and § 1798.90.53 (end-user) obligations found 1 fully addressed, 4 partially addressed, and 6 non-compliant or in structural conflict. (See Appendix B.) [§1–7]

5. **AG Bulletin 2023-DLE-06 guidance not followed.** In October 2023, the Attorney General issued guidance for all California agencies to review vendor contracts for SB 34 compliance, conspicuously post ALPR policies, and address audit deficiencies. SMPD’s first documented compliance activity occurred over two years later. Three of six items were not followed; three were partially addressed. [§1–7]

6. **All compliance activity occurred within six weeks — and remains incomplete.** SOP update, first substantive policy revision in nearly five years, and first audits in thirteen years all occurred within approximately six weeks (November–December 2025). Audits remain narrowly scoped. SOP 205 — required by both its own text and state law to be conspicuously posted — remains unchanged since February 2021 and unposted. The city acknowledged the oversight on March 3, 2026 and stated it was "actively working" to publish it. As of March 16, it remains unavailable [33]. [§7] [§2]

7. **Non-ALPR surveillance operates with no policy framework.** Of the department’s 68 Flock devices, 14 lack ALPR capability — including 10 privately-owned community cameras. At multiple locations, edge computing hardware paired with ALPR cameras converts the same feed into live video — producing ungoverned surveillance data alongside governed ALPR data. These devices are absent from all contracts. The department also operates Verkada cameras under a separate contract with no identified surveillance policy. The City does not have the surveillance technology ordinance recommended by the 2016–2017 Civil Grand Jury. [§4] [20] [24]

8. **No public record of who accesses SMPD’s surveillance data.** City policies require documented approval processes and audit logs for external agency access. In practice, all sharing decisions are made within the Flock platform — a private system not subject to the California Public Records Act. When the platform does maintain logs, the city denies their release under blanket ALPR exemptions. When it doesn’t, no record exists at all. No entity in the chain maintains a retrievable public record of who has accessed public surveillance data, or when access was granted or revoked. [§6] [§2] [32] [26]

---

## 1. Audit Compliance

| Requirement | CA Law | AG Bulletin | SOP 205 | Policy 463 | Compliance | City Audits |
|---|---|---|---|---|---|---|
| Audit frequency | "Periodic" | Address State Auditor findings | ✅ Quarterly [§205.5.1] | ✅ Monthly internal, quarterly external [§463.10] | ⚠️ Monthly since Dec 2025; sharing configuration only — search activity not reviewed [11] [17] | ⚠️ Matches 463 frequency; scope limited to sharing settings, not SOP 205 search compliance criteria |
| Audit reporting chain | — | — | Support Services Captain [§205.5.1] | Chief of Police [§463.10] | ⚠️ Chief of Police [17] | ⚠️ Matches 463; not submitted to Support Services Captain per SOP 205 |
| Audit scope | All ALPR systems | — | ⚠️ Operator audits: all platforms. End-user audits: NCRIC only — no end-user audit requirement for Flock [§205.5.1] | ✅ Flock only [§463.10] | ⚠️ Flock sharing configuration only; search activity, access logs, and case number compliance not reviewed [17] | ⚠️ No end-user audit conducted for any platform |
| Committee structure | — | — | Steering Committee + Subcommittee [§205.7] | ALPR Committee Captain [§463.2.1] | ⚠️ "Flock Committee" — not in SOP [11] | ❌ |
| Annual SOP review | — | — | Required [§205.6] | — | ❌ No review documented [15] | ❌ |
| User offboarding | Required [§.51(a)] | — | ❌ Not addressed | ❌ Not addressed | ❌ No process described [3] | ❌ |
| Community cameras | End-user obligations [§.53] | Determine status | ❌ Not addressed | ❌ Not addressed | ❌ 10 cameras accessed, no policy [20] | ❌ |

- Policy 462/463 required ALPR audits "on a regular basis, as described in SOP 205" [1, §462.6(c)] [3, §463.6(c)]. SOP 205.5.1 specified quarterly end-user audits for the NCRIC platform and quarterly operator audits across all platforms including Flock [4, §205.5.1]. The end-user audit criteria — case number entry, connection between searched data and justification — apply only to NCRIC, which the department confirmed it no longer uses [21]. No equivalent audit requirement exists for Flock search activity, despite the SOP describing that Flock stores this data.

- The annual SOP reviews required by §205.6 — none of which were conducted [15] — should have identified that the end-user audit requirement still references NCRIC, a platform the department abandoned when it adopted Flock. The SOP was updated in February 2021 to add Flock-specific platform descriptions but did not extend the end-user audit requirement to cover it [4] [21].

- SMPD presented to Council on September 1, 2020 (File ID: 20-3547) that "SMPD conducts regular audits to ensure access to the ALPR Databases are within policy" [7]. No audits were conducted [11].

- Kelly O'Keefe confirmed February 10, 2026 — five years after that representation — that "We do not have any records of audits prior to December [2025]." [11]

- O’Keefe stated February 12 that prior audits consisted of "roundtable discussions with the Flock Committee" that "were not memorialized in standalone records" [11]. The department’s own Flock Questions response uses different language: audits were "conducted informally" [17]. Neither characterization is consistent with SOP 205.5’s requirement — in place since at least October 2019 [4, §205.5] — that quarterly audit results be submitted to the Support Services Captain.

- A PRA for records of these roundtable discussions produced three calendar entries across five years [14]. “No responsive records” for minutes, agendas, notes, or summaries. Of the two meetings with attendee lists, none included the Support Services Captain — the person responsible under SOP 205 [4] for receiving audit results [15].

  - **April 23, 2024** — “ALPR Committee Meeting.” Attendees: Trujillo, Leung, Venikov, Lethin, Goshin.
  - **February 25, 2025** — “LPR meeting.” Organizer: Trujillo. SMPD Classroom. No attendee list.
  - **March 6, 2025** — “ALPR/RTIC Meeting for S. Leung.” Attendees: Leung, Goshin. Chief’s conference room.

- First formal audit records: December 2025 [11]. Thirteen years after the department's first ALPR policy. Over three years after the first Flock cameras were invoiced [20]. Twenty-eight months after Council approved the program and the contract was executed.

- Policy 463.10, added December 2025, requires monthly internal audits and quarterly external audits of Flock, documented via memorandum to the Chief of Police. [3, §463.10]

- The monthly audit memos follow the 463.10 framework [17]: they cover Flock only and are addressed to the Chief. No audits have been submitted to the Support Services Captain under SOP 205.5.1 [4, §205.6] — not before December 2025, and not after [15]. The two frameworks have not been reconciled.

- The first audit memo under 463.10 (dated December 19, 2025) retroactively reviewed November sharing configuration [17]. No retroactive review of search activity or external agency access was conducted for any prior period [3, §463.10].

- The audit memos confirm the department reviews sharing settings — the January 2026 memo verifies "Revoke Out-of-State Sharing" is selected. [17]

- A PRA specifically requesting search activity compliance records — distinguished from sharing configuration audits — returned "no records responsive." The department identified the sharing configuration memos as "all audit records maintained by the Department" [32]. No search activity compliance audit has ever been conducted.

- The Flock transparency page listed University of the Pacific — a private institution — among the 283 entities with access to SMPD data [10]. California Civil Code § 1798.90.55(b) permits ALPR sharing only with public agencies [12]. See §6 for entity-type analysis.

- To determine whether UOP qualifies as a "public agency," records requests were sent to three entities. SMPD’s PRA produced no records of any entity-type review [16]. Stockton PD — UOP’s local law enforcement agency — confirmed it performed no legal review; authorization consists of clicking "agree" in the Flock platform [27]. UOP’s Chief of Police responded to a CPRA by confirming that "University of the Pacific is a private institution and therefore not subject to the CPRA" [29].

- The access list includes 10 District Attorney offices, 11 campus police departments, and 4 state agencies. Policy 463.2 limits program purposes to patrol-related activities [3]. No policy defines what purposes external agencies may query SMPD data for. The audit memos do not review entity types or authorized purposes for external agencies [17]. [10]

- State law requires "reasonable security procedures" to protect ALPR information from unauthorized access [12]. Neither Policy 463 [3] nor SOP 205 [4] addresses user account management — no process for revoking access when personnel leave, no authentication requirements specified. The audit memos do not review user offboarding [17].

- The department accesses data from 10 privately-owned community cameras through Flock's "Community Camera Full Access" feature [20] [5, Exhibit A]. It does not own or control these cameras. The audit memos review only "our" cameras and "our" users [17]. Under § 1798.90.53 [12], SMPD's obligations as an end-user apply to all ALPR data it accesses — including data from cameras it does not own.

- SOP 205.6 requires the Support Services Captain to review the SOP itself on an annual basis [4, §205.6]. A PRA for records of annual reviews produced no responsive documents [15]. Three SOP versions exist: October 2019, February 2021, and November 2025 [19]. The November 2025 version changed only the copyright timestamp — the substantive content has been unchanged for nearly five years.

- SOP 205 still references Policy 462, which was renumbered to 463 in January 2023 [2], and describes audit procedures for four platforms the department confirmed it no longer uses [21]. Three annual SOP reviews between the renumber and December 2025 [15], and the November 2025 copyright refresh [4], left these stale references uncorrected.

- SOP 205.7 establishes two oversight bodies [4, §205.7]: an ALPR Steering Committee (Support Services Captain, Communications and Records Manager, Traffic Lieutenant, Investigations Lieutenant) and an ALPR Subcommittee (Investigations Sergeant, Traffic Sergeant, Police IT Senior Systems Analyst). No records of either committee have been produced. O'Keefe referenced a "Flock Committee"—a body not described in SOP 205.

- The city produced "no records responsive" to a PRA requesting quarterly end-user audits and quarterly operator audits [15]. Four individuals served as Support Services Captain during the relevant period — each responsible under SOP 205.5.1 [4, §205.5.1] for receiving quarterly audit submissions. None received any.

---

## 2. Transparency

| Requirement | CA Law | AG Bulletin | SOP 205 | Policy 463 | Compliance |
|---|---|---|---|---|---|
| Policy posting | Conspicuous posting [§.51(b)(1)] | Manual inclusion may not satisfy | — | ❌ No posting requirement | ✅ In manual since ~2020; standalone link Dec 2025 [11] |
| SOP posting | Conspicuous posting [§.51(b)] | — | ✅ Required [§205.1] | — | ❌ Never posted [15] |
| Job titles | Required [§.51(b)(2)(B)] | — | — | ❌ "Members" / "designee" only [§463.4(d)] | ❌ In internal PRA response only [16] |
| Sharing records | — | Review vendor agreements [18] | — | — | ❌ Maintained exclusively by Flock; not subject to PRA [16] [27] |

- Both state law and SOP 205 itself require conspicuous public posting. SOP 205.1: "This Operating Procedure, along with SMPD Lexipol Policy 462, shall be posted conspicuously on our Department website" [4, §205.1]. State law requires the same [12]. Every version of the posted ALPR policy delegates audit procedures, training requirements, and data collection processes to SOP 205 [1] [2] [3].

- SOP 205 is not posted on the department website. The city acknowledged on March 3, 2026: "The Department does not currently have the associated SOPs posted on its website. We acknowledge this oversight and are actively working to ensure the SOPs are published as soon as possible." As of March 16, 2026, it remains unavailable [19] [33]. A member of the public reading the posted policy cannot access the document it cites [4].

- State law requires the published policy to list job titles of employees authorized to use or access the ALPR system [12]. Policy 463 uses only "members" and "authorized designee" [3]. SOP 205 names oversight committee roles [4, §205.7] but not authorized users — and is not publicly posted regardless. The only document listing authorized user titles (Officers, Crime Analysts, Dispatchers, CSOs) is an internal PRA response [17], not a published policy.

- The August 2023 staff report presented to Council stated that SMPD shares with NCRIC and "allied County law enforcement agencies." The 283 agencies with access to SMPD's ALPR data are not disclosed in any city document or council report [8] — they are discoverable only through the Flock transparency page for San Mateo [10].

- The Flock transparency page for San Mateo lists 66 devices [10]. The department's Camera Manager reports 80 [20]. The difference includes 12 Picard devices — AI edge computing hardware that does not appear in any invoice, contract, amendment, or council presentation [5] [6].

- The 12 Picard devices are edge computing hardware paired with ALPR cameras at the same locations. They convert the ALPR camera feed into live video — producing ungoverned surveillance data alongside governed ALPR data. Neither the devices nor the video feeds appear on the public transparency portal [20].

- The department's internal inventory lists 10 privately-owned community cameras [20] whose data SMPD accesses through Flock's "Community Camera Full Access" feature [5, Exhibit A]. No public notice of this access has been identified.

- Sharing agreements that determine which agencies access SMPD’s ALPR data are maintained exclusively by Flock. O’Keefe stated the approval process "is completed within the Flock platform." Flock is a private company. These records cannot be obtained through public records requests to any government agency. [16]

- This was confirmed independently through the UOP investigation. When a PRA to Stockton PD asked whether it had vetted UOP’s status as a public agency before granting access, Stockton’s records custodian responded: "All of the paperwork is created and kept by Flock. We do not retain or have any copies." Stockton’s entire authorization process: clicking "agree" when requested by another department. [27]

---

## 3. Council Representations

| Topic | Staff Report | Documented Reality |
|---|---|---|
| Audits | "Conducts regular audits" (2020 study session) [7] | No audit records before Dec 2025 [11] |
| Standards | "Strictest industry standards" (2023 staff report) [8] | No audits at the time [11] |
| Data retention | "Raw LPR data is not stored or retained" [8] | 30-day retention in contract; SOP describes data storage [5] |
| Sharing scope | "NCRIC and allied County agencies" [8] | 283 agencies statewide [10] |
| Devices | "40 ALPR cameras" (2023); "15 automated license plate readers" (2024) [9] | 12 of the 40 were already deployed before Council approved the program; 53 ALPR + 2 non-ALPR contracted; 80 in Camera Manager [20] |
| Capabilities | Not disclosed in staff report [8] | Statewide/nationwide network, community cameras, Vehicle Fingerprint, audio [5, Exhibit A] |
| Council approval date | "City council approval...in 2020" (audit memo) [17] | Contract approved Aug 2023; 2020 was a study session [8] |
| Pre-contract activity | Not disclosed [8] | 12 Falcon cameras invoiced from Nov 2022 — nine months before Council approved the program [20, INV-10888] |

- During the September 1, 2020 Study Session on Police Accountability (File ID: 20-3547), SMPD presented to Council that "SMPD conducts regular audits to ensure access to the ALPR Databases are within policy" [7]. Kelly O’Keefe confirmed February 10, 2026 that no audit records exist prior to December 2025 [11].

- The August 21, 2023 agenda report (File ID: 23-7622) stated "Staff utilizes strictest industry standards with respect to how data is accessed and by whom" [8]. No documented audits were being conducted at the time [11]. No process for approving external agency access existed [16]. No user offboarding process was in place [3] [4]. Department policy only provided for end-user access audits of the NCRIC platform, not Flock [4, §205.5.1].

- The same report stated "Raw LPR data is not stored or retained by SMPD." The Flock contract specifies a 30-day retention period [5, Exhibit A]. SOP 205 describes in detail how each ALPR platform stores license plate data, photos, and video footage on SMPD’s behalf [4, §205.5.1]. [8]

- The August 2023 staff report (File ID: 23-7622) presented to Council stated that SMPD has "a records sharing agreement with the Northern California Regional Intelligence Center (NCRIC), along with all of the other allied County law enforcement agencies" [8]. The Flock transparency page lists 283 organizations with access statewide [10] — only 14 are San Mateo County agencies.

- The original $501,350 contract was placed on the consent calendar (Agenda Item 13, File ID: 23-7622). The staff report did not disclose [8] that the attached contract included statewide and nationwide network lookup access, community camera full access, Vehicle Fingerprint search capabilities, direct sharing with surrounding jurisdictions, or audio detection (referenced in MSA recitals) [5, Exhibit A and Recitals].

- The Flock deployment was presented to Council as a new project [8]. Twelve of the 40 cameras in the proposal had already been deployed nine months prior. The first invoice — $30,000 for 12 Falcon cameras — has a billing period beginning November 12, 2022 [20, INV-10888]. The invoice was generated February 27, 2023 and paid. The purchase order was issued retroactively five months later (July 27, 2023) [20, POAR PO-0000656]. A second pre-contract invoice — $500 for "Camera Replacement" [20, INV-13720] — was paid in April 2023, four months before Council approved the program.

- O'Keefe's confirmed count of 54 ALPR cameras (40 MSA + 13 Amendment + 1 Flex) leaves no room for the pre-contract 12 as additional units [21]. The 12 cameras were absorbed into the 40-camera MSA that Council approved — meaning Council retroactively authorized cameras that had been collecting data for nine months without any agreement [20]. 

- Seven Flock invoices totaling $12,872 have no associated purchase order — including a $500 "Camera Replacement" charge dated April 2023, before the contract existed. [20]

- Amendment No. 1 (March 2024, File ID: 24-8392, Consent Calendar) described 15 additional devices as "automated license plate readers." The order form lists 13 ALPR and 2 non-ALPR devices. Flock’s own Camera Manager classifies the two Condor devices as "Video Camera," not LPR [20]. The staff report does not mention "video," "surveillance," "PTZ," or "Condor" [9]. Council’s prior ALPR representations — audits, standards, sharing restrictions — do not apply to non-ALPR devices [6, Exhibit C].

- The December 2025 audit memo states: "The San Mateo Police Department obtained city council approval for the use of Flock Automated License Plate Readers in 2020." The contract was approved August 2023 [8]. The 2020 action was a study session on police accountability [7] that mentioned ALPRs but did not involve Flock. [17]

---

## 4. Device Inventory & Non-ALPR Governance

- O’Keefe confirmed February 23, 2026 that the department operates 68 Flock devices, of which 14 do not have ALPR technology. Two were decommissioned, leaving 66 active. O’Keefe stated they would be "deducted from the transparency site by Flock" [21]. The Camera Manager dashboard shows 2 devices offline — consistent with the 2 decommissioned units [20].

- Flock Camera Manager screenshots produced via PRA W012201-022326 report 80 total devices (78 healthy, 2 offline) — 12 more than O’Keefe’s count. [20]

- The 12 additional devices are identified in Camera Manager as "Picard." A thirteenth device not in O’Keefe’s spreadsheet is identified as "Avicore." Neither device type appears in the original contract [5, Exhibit A], Amendment No. 1 [6, Exhibit C], or any publicly available procurement document. [20]

- Picard devices are co-located with LPR cameras at multiple sites. The PRA spreadsheet lists Picard IDs in the "Live Feed" column next to their paired cameras — the Picard provides the video feed. Each paired location produces both ALPR data and non-ALPR video data. Policy 463 and SB 34 govern the ALPR data. Nothing governs the video. [20]

- The Flock contract [5, Exhibit A] and amendment [6, Exhibit C] account for only 2 non-ALPR devices (the Condor PTZ cameras).

- O’Keefe stated there was "no contract for the additional cameras" and that "the cost was below the threshold requiring a new contract." [22]

- No department policy governs video surveillance cameras. Policy 462/463 and SOP 205 cover ALPRs only. SB 34 applies only to ALPR data. Flock’s own contract categorizes Condor cameras as "Video Products." Data retention, access controls, sharing, and auditing of non-ALPR camera data are unaddressed [1] [3] [4] [6, Exhibit C].

- The 2016-2017 San Mateo County Civil Grand Jury found that no jurisdiction in San Mateo County had enacted any ordinance governing acquisition, use, or data management of surveillance technology (Finding F2), and recommended all agencies bring such a policy before their governing body by December 31, 2017 (Recommendation R3). [24]

- San Mateo did not adopt an ordinance in response. A review of the San Mateo Municipal Code (current through February 2026) identified no such ordinance. Council Member Nicole Fernandez confirmed in a July 2025 news report: "We don’t have a CCOPS ordinance." [25]

- By contrast, at least nine other California jurisdictions have adopted surveillance technology ordinances since 2016, including Santa Clara County, Berkeley, Oakland, San Francisco, and Palo Alto.

---

## 5. Vendor Disclosure Authority

| Requirement | CA Law | AG Bulletin | Policy 462/463 | Contract | Compliance |
|---|---|---|---|---|---|
| Pre-approval process | — | — | Required for external agencies [§462.8(a)] [§463.8] | — | ❌ No documented process; no approval records produced [16] |
| Sharing restricted to public agencies | Public agencies only; non-public prohibited [§.55(b)] | State or local only [18] | ✅ CA public agencies only [§463.8] | — | ❌ Private university on access list; does not qualify under §.5(f) [10] |
| Vendor agreement review | — | Review contracts for non-public access provisions [18] | ❌ Not addressed | § 5.3 authorizes disclosure to non-public entities [5] | ❌ No evidence of review; § 5.3 remains in contract [6] |
| Vendor disclosure authority (§ 5.3) | Non-public sharing prohibited [§.55(b)] | — | ❌ Not addressed | Flock discloses independently to third parties [5] | ❌ No process to detect disclosures [17] |
| Vendor notification to SMPD | — | — | — | Not required [5] | ❌ Not addressed in audits [17] |
| Survival of terms | — | — | — | § 5.3 survives termination [5] | ❌ Not addressed [3] |

- Policy 462.8 (2012–December 2025) required that external "Agency members accessing ALPR data are subject to a pre-approval process by SMPD and the database management administration" [1, §462.8(a)]. Policy 463.8 (December 2025–present) restricts sharing to California public agencies only, reviewed by the ALPR Committee Captain, and explicitly excludes out-of-state and federal agencies [3, §463.8].

- Flock MSA § 5.3 authorizes Flock to independently "access, use, preserve and/or disclose the Footage to law enforcement authorities, government officials, and/or third parties" based on Flock's own "good faith belief" that disclosure is "reasonably necessary." This includes disclosures to comply with legal process, enforce the agreement, or address "security, privacy, fraud or technical issues, or emergency situations." No SMPD approval, notification, or involvement is required. [5, §5.3]

- This authority is not incidental to data hosting. § 4.1 provides that SMPD retains ownership; § 5.2 permits only anonymized data use. § 5.3 applies to identifiable "Footage" — defined in the contract as "still images, video, audio and other data." The scope covers everything the cameras capture [5, §1.10]. [5, §5.3] [5, §4.1] [5, §5.2]

- § 5.3 contains no requirement that Flock notify SMPD before or after a disclosure. The department's monthly audit memos review dashboard sharing settings and user access [17]; none reference vendor-initiated disclosures under § 5.3 [5, §5.3].

- MSA §7.3 provides that Section 5 (including §5.3) survives termination of the contract. Flock retains this disclosure authority even after the agreement ends, for any footage still within the retention period. [5, §7.3]

- §5.3 was not disclosed in either the August 2023 staff report (File ID: 23-7622) or the March 2024 amendment staff report (File ID: 24-8392). Neither report mentioned that the contract grants the vendor independent authority to disclose SMPD surveillance data to third parties [8] [9].

- The original MSA (August 2023) was signed by Chief Barberini and Flock’s General Counsel. No city attorney reviewed it [5, signature page]. Two months later, the AG Bulletin guided agencies to review vendor agreements for exactly this kind of provision [18]. Amendment No. 1 (March 2024) added city attorney review [6, signature page] — but did not modify § 5.3.

- A PRA requesting legal analysis of whether § 5.3 is consistent with § 1798.90.55(b) was denied under attorney-client privilege [28]. The privilege claim confirms a legal assessment exists — but the city declines to disclose whether its own contract provision conflicts with state law.

- The December 2025 policy revision tightened data sharing to California-only agencies [3, §463.8] but did not address the contract provision that authorizes Flock to disclose outside those restrictions [5, §5.3].

- SB 34 [12] prohibits public agencies from sharing ALPR information with non-public entities (§ 1798.90.55(b)), with an exception for data hosting. § 5.3 authorizes Flock to independently disclose beyond the hosting function. SB 34 does not directly regulate the vendor's subsequent use of data it holds as a host [5, §5.3].

---

## 6. Network Sharing

| Requirement | CA Law | AG Bulletin | Policy 463 | Staff Report | Compliance | City Audits |
|---|---|---|---|---|---|---|
| Authorized recipients | Public agencies only [§.55(b)] | No out-of-state or federal | ✅ CA public agencies only [§463.8] | "NCRIC and allied County agencies" [8] | ❌ 283 agencies statewide [10] | ❌ |
| Entity type | Public agencies [§.55(b)] | State or local only | ✅ Public agencies [§463.8] | — | ❌ Private university on list; not a public agency under §.5(f) [10] | ❌ |
| Approval process | — | — | ALPR Committee Captain review [§463.8(b)] | — | ❌ No documented process; no approval records produced [16] | ❌ |
| Written agreements | — | — | Required [§462.8(b)–(c)] | — | ❌ No records produced [16] | ❌ |
| Training (external) | — | — | "Shall ensure" [§463.9] | — | ❌ "We do not retain records" [16] | ❌ |
| Network governance | — | Review vendor agreements | ❌ Not addressed | — | ❌ Not addressed in any policy [5, Exhibit A] | ❌ |
| Out-of-state/federal | Prohibited [§.55(b)] | Prohibited | ✅ Excluded [§463.8] | — | ⚠️ Direct sharing disabled; El Cajon (sued by AG for out-of-state sharing) has access [17] | ⚠️ |

- The August 2023 staff report (File ID: 23-7622) presented to Council stated that SMPD has "a records sharing agreement with the Northern California Regional Intelligence Center (NCRIC), along with all of the other allied County law enforcement agencies." [8]

- As of February 18, 2026, the Flock transparency page for San Mateo lists 283 organizations with access to San Mateo ALPR data. The list spans the entire state, from Yuba County to San Diego, and includes agencies in Imperial, Kern, Los Angeles, Riverside, and Orange counties — beyond "allied County law enforcement agencies." [10]

- The list includes El Cajon PD [10], which is currently being sued by the California Attorney General for refusing to stop sharing ALPR data with out-of-state agencies in violation of SB 34 [13].

- California Civil Code § 1798.90.55(b) prohibits sharing ALPR data "except to another public agency." § 1798.90.5(f) defines "public agency" as "the state, any city, county, or city and county, or any agency or political subdivision" thereof. [12]

- San Mateo’s sharing list included University of the Pacific, a private university in Stockton. It is not a state agency, political subdivision, or public institution. It does not qualify as a "public agency" under § 1798.90.5(f). Its inclusion on SMPD’s access list is inconsistent with § 1798.90.55(b). Neither SMPD nor Stockton PD produced any record of a public agency determination. UOP’s own Chief of Police confirmed it is "a private institution and therefore not subject to the CPRA" [29]. [10] [12] [27]

- UOP was present on SMPD’s Flock transparency portal as of February 18, 2026 [10]. As of March 11, it had been removed [30]. The removal was not flagged by any audit and was not communicated to the requestor who raised the issue. Stockton PD’s portal still lists UOP [30].

- SMPD cited § 1798.90.55(b) to deny a resident's request for their own vehicle's ALPR records [26]. Under the same statute [12], University of the Pacific — a private institution — had access to SMPD's entire ALPR dataset [10].

- The department’s interpretation of "ALPR information" under § 1798.90.55(b) is broad enough to deny a resident an aggregate count of their own vehicle’s detections [26] and to withhold aggregate search compliance data from the public [32] — but not broad enough to prevent the department from sharing data with a private vendor who may independently disclose it to third parties under MSA § 5.3 [5].

- The access list includes 10 District Attorney offices, 11 campus police departments, and 4 state agencies. Policy 463.2 limits the program to identifying stolen vehicles, suspect interdiction, warrant service, and stolen property recovery. § 463.4(b) broadens use to "any routine patrol operation or criminal investigation" — but that section applies to "Department members," not external agencies. No policy defines what purposes external agencies may query SMPD data for [3] [4]. [10]

- The Flock contract includes "State Network (LP Lookup Only)" and "Nationwide Network (LP Lookup Only)" as standard FlockOS features. Neither Policy 462/463 [1] [3] nor SOP 205 [4] addresses how these network sharing features are governed, audited, or restricted. [5, Exhibit A]

- Mountain View PD discovered in January 2026 that Flock had enabled statewide access on 29 of 30 cameras for the entire 17-month duration of their program. Flock did not retain records of who accessed the data [13]. When Mountain View audited, it found approximately 240 agencies with access — against an intended list of roughly 75. SMPD’s list: 283 [10].

- PRA W012174-021826 (filed February 18, 2026) requested four categories of records related to external agency access. The city's blanket response: "A good faith and diligent search yielded no records responsive to your request." Each item corresponded to a specific policy requirement: [16]

  - **Pre-approval process:** Policy 462.8(a) required external agencies to be subject to "a pre-approval process by SMPD and the database management administration." Policy 463.8(b) requires ALPR Committee Captain review before access is granted. O'Keefe stated the process "is completed within the Flock platform" and that she "was unable to locate documentation associated." [1, §462.8(a)] [3, §463.8(b)] [16]

  - **Written agreements:** Policy 462.8(b) required a written request including the agency name, person requesting, and intended purpose. Policy 462.8(c) required "the approved request is retained on file." No MOUs, data sharing agreements, or written agreements were produced. [1, §462.8(b)–(c)] [16]

  - **Access determination:** No records documenting how the department determined which of the 283 agencies to grant access, including configuration of Flock's statewide network, nationwide network, or direct sharing settings. [16] [10]

  - **Training verification:** Policy 463.9 requires the ALPR Administrator "shall ensure" that external users receive department-approved training. O'Keefe stated: "We do not retain records of training for external agencies." [3, §463.9] [16]

- The January 2026 audit memo states Lt. Casazza "verified that we are not sharing data with law enforcement agencies outside the state of California" [17]. The Flock Questions response confirms "none of its 31 cameras have statewide or national lookup capabilities enabled." This checks geography. It does not check entity type. University of the Pacific is in California — it passes the geographic check. But it is a private university that does not qualify as a public agency under § 1798.90.5(f) [12]. Three months of audits did not flag this [10].

- Two agencies independently confirmed that Flock controls the sharing process. O’Keefe: the approval process "is completed within the Flock platform." [16] Stockton PD’s records custodian described authorization as clicking "agree" when requested by another department: "All of the paperwork is created and kept by Flock. We do not retain or have any copies." [27] Neither agency described any independent legal review of whether sharing partners qualify as public agencies under § 1798.90.5(f). The records documenting access to public surveillance data are held exclusively by a private vendor — beyond the reach of the Public Records Act.

- A PRA for records documenting the removal of agencies from the sharing list — including UOP — returned no responsive records. O’Keefe confirmed: "Removals from Flock are conducted within the platform at the discretion of the Lieutenant assigned to the ALPR Committee. There are no corresponding records" [31]. Agencies can be added or removed from a public surveillance sharing network at a single officer’s discretion, with no documentation and no approval process. A follow-up asking whether the Flock platform logs these changes was met with: "I did not locate a log of this information in the Flock platform" [31]. No audit trail exists at the agency or the vendor.

---

## 7. Timing of Compliance Activity

- The Attorney General issued Bulletin 2023-DLE-06 on October 27, 2023 — two months after the Flock contract was executed — guiding all California law enforcement agencies to review vendor contracts for SB 34 compliance, conspicuously post ALPR policies, and address audit deficiencies. SMPD's first documented compliance activity occurred over two years later. [18]

- This pattern of retroactive compliance predates the December 2025 compliance window. The first Flock cameras were billed from November 2022 [20, INV-10888]. The purchase order was issued retroactively in July 2023. Council approved the program in August 2023 — nine months after deployment.

- Lt. Steve Casazza was assigned as Flock Committee administrator in July 2025 [17] — five months before the first audit memo, two years into the Flock contract, and thirteen years after the department adopted its first ALPR audit requirement (Policy 462, 2012) [11].

- SOP 205 copyright date: November 10, 2025. Policy 463 copyright date: December 12, 2025. Policy 463 effective date: December 22, 2025. First documented audits: December 2025 [11]. The SOP 205 "update" changed only the copyright date — substantive content unchanged since February 2021 (see §1) [19]. [19] The policy revision and first audits occurred within a six-week window. [4, footer] [3, footer] [11]

- The December 2025 policy revision made significant changes [3]: data sharing restricted to California-only agencies, immigration enforcement prohibition added, retention reduced from one year [1] to 30 days, new monthly/quarterly audit requirements with reporting to Chief.

- The December 2025 revision resolved a 27-month conflict between policy and contract. Policy 462.5 required a minimum one-year retention period [1]. The Flock contract specified 30 days [5, Exhibit A]. For the entire period between contract execution and the policy revision, one of them was being violated. The audit memos do not note this discrepancy [17].

- This window coincides with the Attorney General's lawsuit against El Cajon for out-of-state ALPR sharing. [13] Mountain View's discovery in January 2026 that Flock had enabled unauthorized statewide access for 17 months followed weeks later. [13]

---

## Source Documents

**How to look up City Council records:** Direct packet links are provided where available. Alternatively, visit https://www.cityofsanmateo.org/publicmeetings — use Advanced Search → Tracking Number field to search by File ID (e.g., 20-3547).

| # | Document | Link |
|---|----------|------|
| 1 | Policy 462 — SMPD Policy Manual (Aug 2020, Mar 2021) | [Wayback Aug 2020](https://web.archive.org/web/20201114041922/https://www.cityofsanmateo.org/DocumentCenter/View/79089/San-Mateo-PD-Policy-Manual) · [Wayback Mar 2021](https://web.archive.org/web/20210515131231/https://www.cityofsanmateo.org/DocumentCenter/View/79089/San-Mateo-PD-Policy-Manual) |
| 2 | Policy 463 (Jan 2023, pre-revision) — SMPD Policy Manual | [Wayback Jan 2023](https://web.archive.org/web/20231218150327/https://www.cityofsanmateo.org/DocumentCenter/View/79089/San-Mateo-PD-Policy-Manual) |
| 3 | Policy 463 (Dec 2025, current) — Automated License Plate Readers | [City website](https://www.cityofsanmateo.org/DocumentCenter/View/99914/ALPR-Policy-12-23-2025) · [Wayback Jan 2026](https://web.archive.org/web/20260213002712/https://www.cityofsanmateo.org/DocumentCenter/View/79089/San-Mateo-PD-Policy-Manual) |
| 4 | SOP 205 — ALPR Operating Procedure (three versions: Oct 2019, Feb 2021, Nov 2025) | Not publicly posted; obtained via PRA. [Nov 2025](https://github.com/none-below/sm-alpr/blob/main/assets/san-mateo-public-records/W012159-021226/SOP_205.pdf) · [Oct 2019](https://github.com/none-below/sm-alpr/blob/main/assets/san-mateo-public-records/W012198-022326/SOP_205_-_Oct_25_2019.pdf) · [Feb 2021](https://github.com/none-below/sm-alpr/blob/main/assets/san-mateo-public-records/W012198-022326/SOP_205_-_Feb_10_2021.pdf). **Version history:** Oct 2019 covered only PIPs + NCRIC (3 pages). Feb 2021 was a major rewrite: added Flock, Vigilant, BOSS; added California Values Act prohibition; created §205.5.1 platform audit subsection; created §205.7 ALPR Committees; added dual quarterly audit requirement. Nov 2025 is word-for-word identical to Feb 2021 — only the copyright date and page numbers changed. Still references Policy 462 (renumbered to 463 in January 2023). [19] [21] |
| 5 | Flock Safety MSA — executed Aug 25, 2023, $501,350 | Council packet for File ID: 23-7622, starting p. 255. [Packet](https://sanmateo.primegov.com/Public/CompiledDocument?meetingTemplateId=6887&compileOutputType=1) |
| 6 | Amendment No. 1 — executed Mar 20, 2024, $138,950 | Council packet for File ID: 24-8392, p. 175. [Packet](https://sanmateo.primegov.com/Public/CompiledDocument?meetingTemplateId=7905&compileOutputType=1) |
| 7 | Council Study Session — Police Accountability, Sep 1, 2020 | File ID: 20-3547 (36-page packet; staff report p. 3; ALPR audit claim p. 24). [Packet](https://sanmateo.primegov.com/Public/CompiledDocument?meetingTemplateId=2769&compileOutputType=1) |
| 8 | Council Agenda Report — Flock purchase, Aug 21, 2023 | File ID: 23-7622 (653-page packet; staff report pp. 242–244; resolution p. 245; order form p. 251; MSA starts p. 255). [Packet](https://sanmateo.primegov.com/Public/CompiledDocument?meetingTemplateId=6887&compileOutputType=1) |
| 9 | Council Agenda Report — Flock amendment, Mar 18, 2024 | File ID: 24-8392 (1,487-page packet; staff report pp. 5–8; Amendment No. 1 p. 175; Exhibit C / Condor disclosure p. 177). [Packet](https://sanmateo.primegov.com/Public/CompiledDocument?meetingTemplateId=7905&compileOutputType=1) |
| 10 | Flock Transparency Portal — San Mateo CA PD | [transparency.flocksafety.com](https://transparency.flocksafety.com/san-mateo-ca-pd) · [Archived copy](https://github.com/none-below/sm-alpr/blob/main/assets/san-mateo-public-records/W012174-021826/2026-02-18-flock-transparency-smpd.pdf) |
| 11 | Email correspondence — Resident, O'Keefe, Khojikian, Barberini, Newsom | Feb 5–13, 2026 |
| 12 | CA Civil Code § 1798.90.5 et seq. (SB 34) — ALPR operator and end-user obligations | Operator: [§ 1798.90.51](https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?sectionNum=1798.90.51.&lawCode=CIV) · End-user: [§ 1798.90.53](https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?sectionNum=1798.90.53.&lawCode=CIV) · Definitions: [§ 1798.90.5](https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?sectionNum=1798.90.5.&lawCode=CIV) · Sharing restrictions: [§ 1798.90.55](https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?sectionNum=1798.90.55.&lawCode=CIV) |
| 13 | Regional news and government sources | [City of Mountain View official statement](https://www.mountainview.gov/Home/Components/News/News/1203/284) · [MV Voice](https://www.mv-voice.com/police/2026/01/30/amid-immigration-crackdown-mountain-view-discovers-unauthorized-access-to-license-plate-data/) · [KQED](https://www.kqed.org/news/12072077/as-california-cities-grow-wary-of-flock-safety-cameras-mountain-views-shuts-its-off) · [CA AG re El Cajon](https://oag.ca.gov/news/press-releases/attorney-general-bonta-sues-el-cajon-illegally-sharing-license-plate-data-out) |
| 14 | PRA W012159-021226 — Flock Committee records (closed Feb 2026) | Calendar entries produced (3 meetings, 2024–2025); "no responsive records" for meeting minutes, agendas, notes, or summaries. [Calendar entries](https://github.com/none-below/sm-alpr/blob/main/assets/san-mateo-public-records/W012159-021226/Calendar_-_Pak.pdf) |
| 15 | PRA W012160-021226 — Audit records and SOP reviews (closed Feb 22, 2026) | Requested: quarterly end-user audits, quarterly operator audits, annual SOP 205 reviews, Support Services Captain identity. Response: "A good faith and diligent search yielded no records responsive" to items 1–3. Item 4 produced: Dave Norris (9/9/18–4/9/21), Dave Peruzzaro (5/16/21–1/5/25), Matt Lethin (1/5/25–6/13/25), Jennifer Maravillas (6/22/25–present). Inline questions about SOP 205 review date and posting location not addressed. [PRA response](https://github.com/none-below/sm-alpr/blob/main/assets/san-mateo-public-records/W012160-021226/W012160-021226_Message_History.pdf) |
| 16 | PRA W012174-021826 — External agency access/sharing (closed Feb 2026) | Response by item: (1) agency access approval process "completed within the Flock platform," unable to locate documentation; (2–3) no responsive records for MOUs or sharing configuration; (4) "We do not retain records of training for external agencies." 283 agencies have access, zero training documentation. PRA filing text cited 285 agencies; manual count of Feb 18 portal archive yields 283 (difference: "University of California, Berkeley" parsed as two entries in automated counts). [PRA response](https://github.com/none-below/sm-alpr/blob/main/assets/san-mateo-public-records/W012174-021826/W012174-021826_Message_History.pdf) |
| 17 | Email from Kelly O'Keefe with attachments — Audit memos and Flock Questions response (via email thread, PRA W012129-020926) | Three monthly audit memos (Lt. Casazza to Chief Barberini: Nov 2025, Dec 2025, Jan 2026) and a Flock Questions response document describing audit process. [Audit memos](https://github.com/none-below/sm-alpr/blob/main/assets/san-mateo-public-records/W012129-020926/Monthly%20Audits.pdf) · [Flock Questions](https://github.com/none-below/sm-alpr/blob/main/assets/san-mateo-public-records/W012129-020926/Flock%20Questions-Response.pdf) |
| 18 | CA Attorney General Bulletin 2023-DLE-06 — ALPR/SB 34 compliance guidance | [oag.ca.gov (PDF)](https://oag.ca.gov/system/files/media/2023-dle-06.pdf). Notes agencies should review vendor agreements for SB 34 compliance; clarifies sharing restrictions (no out-of-state, no federal, no private entities); conspicuous posting requirement. |
| 19 | PRA W012198-022326 — SOP 205 version history and posting status (closed Mar 2026) | Produced: two prior SOP 205 versions (Oct 25, 2019 and Feb 10, 2021). [PRA response](https://github.com/none-below/sm-alpr/blob/main/assets/san-mateo-public-records/W012198-022326/W012198-022326_Message_History.pdf) · [SOP 205 Oct 2019](https://github.com/none-below/sm-alpr/blob/main/assets/san-mateo-public-records/W012198-022326/SOP_205_-_Oct_25_2019.pdf) · [SOP 205 Feb 2021](https://github.com/none-below/sm-alpr/blob/main/assets/san-mateo-public-records/W012198-022326/SOP_205_-_Feb_10_2021.pdf) |
| 20 | PRA W012201-022326 — Camera inventory, decommissions, and invoices (closed Feb 26, 2026) | Produced: Flock Camera Manager screenshots (80 devices/80 live locations), O’Keefe spreadsheet (60 active devices), all Flock invoices since Jan 2023, and purchase orders. First Flock invoice ($30,000) dated February 27, 2023 — six months before the MSA was executed; purchase order issued retroactively five months later. Response also revealed three conflicting device counts, 12 Picard devices and 1 Avicore device absent from all contracts, and $12,872 in invoices with no purchase order. [Camera Manager](https://github.com/none-below/sm-alpr/blob/main/assets/san-mateo-public-records/W012201-022326/San_Mateo_CA_PD_-_Flock_Camera_Manager.pdf) · [Device spreadsheet](https://github.com/none-below/sm-alpr/blob/main/assets/san-mateo-public-records/W012201-022326/Flock_Camera_Location_Spreadsheet.pdf) · [Invoices and POs](https://github.com/none-below/sm-alpr/tree/main/assets/san-mateo-public-records/W012339-031326) |
| 21 | Email — O'Keefe camera count clarification (Feb 23, 2026) | Of 68 Flock devices, 54 have LPR capability and 14 do not. Two decommissioned (to be removed from transparency portal by Flock), leaving 66 active. "All cameras are part of the audit." "We do not own any cameras supported by Vigilant, BOSS, PIPS, NCRIC therefore we have no data to share." [Google Drive](https://drive.google.com/file/d/1B-6xZj_vtbsVHyUI1pQPKVq4IhEdzWaQ/view?usp=sharing) |
| 22 | Email — O'Keefe camera expansion clarification (Feb 23, 2026) | "There was no contract for the additional cameras, they were perched upon existing units and the cost was below the threshold requiring a new contract." Confirms 13 devices beyond the 55 covered by MSA and Amendment No. 1 were deployed without contract amendment or council action. [Google Drive](https://drive.google.com/file/d/1nlvd1qWqt4eTGGqPhWGDiSgLhDFQyZXP/view?usp=sharing) |
| 23 | Email — O'Keefe Verkada confirmation (Feb 25, 2026) | Confirmed trailers were purchased and do not have ALPR. Confirmed "The City has a contract with Verkada for cameras at city sites, those also do not have ALPR." [Google Drive](https://drive.google.com/file/d/1KjmWBiI0aoGqmtmRoTSuGfkXHAIPraTS/view?usp=sharing) |
| 24 | 2016-2017 San Mateo County Civil Grand Jury — "A Delicate Balance: Privacy vs. Protection" (filed July 12, 2017) | Finding F2: "The County and cities in San Mateo County have not enacted any ordinances governing their acquisition and use of surveillance technology, or the accessibility, management, or retention of the information acquired." Recommendation R3: agencies bring surveillance technology policy/ordinance before governing body by Dec 31, 2017. San Mateo did not adopt an ordinance in response. [sanmateo.courts.ca.gov](https://sanmateo.courts.ca.gov/divisions/civil-grand-jury/archived-final-reports-civil-grand-jury) · [PDF](https://sanmateo.courts.ca.gov/system/files/surveillance.pdf) |
| 25 | San Mateo Daily Journal — "Surveillance in parks expands in San Mateo" (July 22, 2025) | Council Member Nicole Fernandez: "We don't have a CCOPS ordinance." Article notes San Francisco, Palo Alto, and Davis have passed CCOPS ordinances; San Mateo has not. Also references Verkada park cameras ($91K contract, six parks) and "Connect San Mateo" private camera network. [smdailyjournal.com](https://www.smdailyjournal.com/news/local/surveillance-in-parks-expands-in-san-mateo/article_b3cad750-23cc-4d3c-8734-7fd41b8e54a1.html) |
| 26 | Email — Personal ALPR data request and denial (Feb 19 – Mar 5, 2026) | Resident requested own vehicle’s ALPR detections and access logs per § 1798.90.55(a). Initial denial (Feb 26) cited § 1798.90.55(b) and public interest exemption (Gov. Code 7922.000; ACLU v. Superior Court). Follow-up (Feb 27) challenged scope, requested Condor video footage and access logs separately. O’Keefe response (Mar 5): "There is no existing process for verifying what ALPR data the Department holds on specific vehicles." Condor cameras "do not have LPR data and therefore do not have query information — I can not search which vehicles were captured on those cameras." Confirmed access logs maintained through Flock. Final follow-up (Mar 5) requested vehicle fingerprint searches, non-ALPR detections, aggregate counts, and access logs; cited Picard edge computing devices. Pending response. [Google Drive](https://drive.google.com/file/d/1CLyxbYa3x7Dr4auDWAEagKjQBqGXc_eM/view?usp=sharing) |
| 27 | PRA 10310081 — Stockton PD re: UOP ALPR sharing (closed Mar 4, 2026) | University of the Pacific appears on SMPD’s Flock sharing list (§6); UOP is located in Stockton. This PRA asked whether Stockton PD — UOP’s local law enforcement agency — independently determined UOP qualifies as a "public agency" under § 1798.90.5(f). Response (Maryann Weiman): MOUs are "created and kept on file by Flock." Authorization process: select "agree" when requested. "All of the paperwork is created and kept by Flock. We do not retain or have any copies." No public agency determination, no legal review, no retained documentation. [Google Drive](https://drive.google.com/file/d/1SJyzFXZhE6Z8RhaHgSFhl1a2glTwTX9C/view?usp=sharing) |
| 28 | PRA W012297-030826 — Section 5.3 vendor disclosure records (filed Mar 8, 2026) | Requested: Flock notifications to SMPD re: §5.3 disclosures, SMPD tracking records, communications re: scope/exercise of §5.3, legal review of §5.3 vs. SB 34, disclosure logs, monitoring procedures. City Attorney claimed attorney-client privilege on Item 4 (legal review of §5.3 vs. § 1798.90.55(b)), confirming a legal analysis exists but declining to produce it. Remaining items producing on rolling basis. [PRA response](https://github.com/none-below/sm-alpr/blob/main/assets/san-mateo-public-records/W012297-030826/W012297-030826_Message_History.pdf) |
| 29 | Email — Chief Grant Bedford, UOP Department of Public Safety (Mar 11, 2026) | Response to CPRA requesting legal authority for UOP’s status as a "public agency." Bedford: "While University of the Pacific is a private institution and therefore not subject to the CPRA, we are happy to share Pacific’s policy regarding the ALPR system." Linked to UOP ALPR policy page. Confirms UOP is private and not subject to CPRA. [Google Drive](https://drive.google.com/file/d/1rsQMiX2p90tArc93OCm6fEh0vcbDGWNA/view?usp=sharing) |
| 30 | Flock Transparency Portal archives — SMPD and Stockton PD (Mar 11, 2026) | SMPD portal: UOP no longer listed. [Google Drive](https://drive.google.com/file/d/1nRz3hYWN-F5Qmc_AbZ3PiW_H5tuezXf-/view?usp=sharing). Stockton portal: UOP still listed. [Google Drive](https://drive.google.com/file/d/1y0sCFQgVnhYlRZ-iniwTCDGIBmksPnc2/view?usp=sharing). Compare to Feb 18 archive [10] where UOP was present on SMPD’s list. |
| 31 | PRA W012328-031226 — UOP removal communications and sharing list changes (filed Mar 12, 2026; closed Mar 13, 2026) | "No records responsive." O’Keefe: "Removals from Flock are conducted within the platform at the discretion of the Lieutenant assigned to the ALPR Committee. There are no corresponding records." No internal communications regarding UOP were produced. Confirms sharing list changes require no documentation, no approval process, and are made at a single officer’s discretion through the Flock platform. Follow-up asked whether the Flock platform maintains a log of additions/removals. O’Keefe: "I did not locate a log of this information in the Flock platform." No audit trail exists at the agency or the vendor. [PRA response](https://github.com/none-below/sm-alpr/blob/main/assets/san-mateo-public-records/W012328-031226/W012328-031226_Message_History.pdf) |
| 32 | PRA W012320-031126 — Search activity compliance and audit records (closed Mar 13, 2026) | Requested: quarterly search activity audit records under SOP 205, aggregate search compliance data (total searches, case number entry rates, justification verification), external agency search counts. PRA explicitly distinguished search activity audits from sharing configuration audits and stated re-production of Casazza memos would not be responsive. Response: "no records responsive to your request for aggregate data. All audit records maintained by the Department were provided to you in your request #W012129-020926." Remaining items denied under § 7923.600 (investigation records) and § 1798.90.55(b). Confirms no search activity compliance audit has ever been conducted; sharing configuration memos are the only audit records that exist. [PRA response](https://github.com/none-below/sm-alpr/blob/main/assets/san-mateo-public-records/W012320-031126/W012320-031126_Message_History.pdf) |
| 33 | Wayback Machine snapshot — SMPD License Plate Readers page (Mar 16, 2026) | Archived snapshot of cityofsanmateo.org/3211/License-Plate-Readers. Page links to full policy manual (not standalone Policy 463). SOP 205 not posted. No mention of Flock by name. [Wayback](https://web.archive.org/web/20260316184149/https://www.cityofsanmateo.org/3211/License-Plate-Readers) |

---

## Key Contacts

- **Kelly O'Keefe** — Police Technical Services Administrator, Oversees Records Division. Primary PRA contact. [11] [17]
- **Ed Barberini** — Chief of Police. Signed both Flock contracts. [5] [6]
- **Alex Khojikian** — City Manager. [11]
- **Rob Newsom** — Council Member. Forwarded resident concerns to City Manager. [11]
- **Lt. Matthew Earnshaw** — Staff contact on August 2023 agenda report. [8]
- **Lt. Paul Pak** — Point of contact on Amendment No. 1. [6] Calendar entries produced from his account. [14]
- **Lt. Steve Casazza** — Flock Committee administrator (assigned July 2025). Author of audit memos. [17]
- **Capt. Matt Lethin** — Support Services Captain (1/5/25–6/13/25). [15] Staff contact on March 2024 amendment. [9] Email "no longer valid." [14]
- **Bahar Abdollahi** — Assistant City Attorney. "Approved as to Form" on Amendment No. 1 (not on original MSA). [6]
- **Mark Smith** — Flock Safety General Counsel. Signed original MSA. [5]
- **Andrew Trujillo** — Organized ALPR Committee and LPR meetings (2024–2025). [14]
- **Samantha Leung** — ALPR Committee Meeting attendee. [14]
- **Mikhail Venikov** — ALPR Committee Meeting attendee. [14]
- **Jillian Goshin** — ALPR Committee Meeting attendee. Email no longer valid. [14]
- **Dave Norris** — Support Services Captain (9/9/18–4/9/21). [15]
- **Dave Peruzzaro** — Support Services Captain (5/16/21–1/5/25). [15]
- **Jennifer Maravillas** — Support Services Captain (6/22/25–present). [15]

---

## Appendix A: Flock Transparency Portal — Agency Access Breakdown

*Source: Flock Safety Transparency Portal for San Mateo CA PD, archived February 18, 2026 [10]. Manual count: 283 organizations.*

### Summary

| Category | Count | Notes |
|----------|-------|-------|
| **Total organizations listed** | 283 | |
| San Mateo County agencies | 14 | The "allied County law enforcement agencies" referenced in the 2023 staff report [8] |
| Agencies outside San Mateo County | 269 | |
| Deactivated | 1 | Santa Cruz CA PD |

### Entity types on the access list

The 2023 staff report described sharing with "NCRIC and allied County law enforcement agencies." [8] The access list includes the following categories of organizations beyond county law enforcement:

**District Attorney offices (10):** Kings County CA DAs Office, Marin County CA DA, Monterey County District Attorney's Office, Placer County CA DA Office, Riverside County CA District Attorney, Sacramento CA DA, San Francisco District Attorney CA, San Joaquin CA DA, Santa Clara DA CA, Solano County DA CA

**Campus and education police departments (11):** San Jose State University CA, Cal State Fullerton (CA), Cal State San Bernadino [sic] CA PD, Cerritos College CA PD, Rio Hondo College PD CA, San Joaquin Delta College PD (CA), Sequoias Community College District CA PD, UC Irvine Campus PD CA, University of California Berkeley, University of the Pacific (CA), West Valley Mission College Dist Campus (CA). **Note:** University of the Pacific is a private institution that does not qualify as a "public agency" under § 1798.90.5(f). Its inclusion on the access list is inconsistent with § 1798.90.55(b). UOP’s Chief of Police confirmed in writing that it is "a private institution" [29]. UOP was subsequently removed from SMPD’s portal but remains on Stockton PD’s [30]. [12] All other campus entities listed are part of the UC, CSU, or California community college systems (state agencies).

**State agencies (4):** Cal Fire, California Highway Patrol, California State Parks, NCRIC

**Other non-PD/SO entities (3):** Orange County Fire Authority (CA), Sacramento County Parks Department CA, Los Angeles Port Police CA

### Geographic range

**Southernmost agencies (10):** Brawley CA PD, Calexico CA PD, Chula Vista CA PD, El Cajon CA PD, El Centro CA PD, Imperial City CA PD, Imperial County CA SO, National City CA PD, Oceanside CA PD, San Diego Harbor CA PD

**Northernmost agencies include:** Yuba County Sheriffs Office, Yuba City CA PD, Sutter County CA SO, Shasta County SO, Tehama County CA SO, Lassen County SO (CA), Modoc County CA SO

### San Mateo County agencies (14)

Belmont CA PD, Brisbane CA PD, Burlingame CA PD, Colma CA PD, Daly City CA PD, Foster City CA PD, Hillsborough CA PD, Menlo Park CA PD, Pacifica CA PD, Redwood City CA PD, San Bruno CA PD, San Mateo County CA SO, South San Francisco CA PD, Town of Woodside CA (SMCSO)

---

## Appendix B: Statutory Compliance Matrix

*Element-by-element comparison of California Civil Code §§ 1798.90.51 (operator), 1798.90.53 (end-user), and 1798.90.55 (sharing) against Policy 463 [3], SOP 205 [4], and documented practice. Analysis date: March 4, 2026. Statute text: [12].*

SMPD is both an ALPR **operator** (§ 1798.90.51 — it operates Flock cameras) and an ALPR **end-user** (§ 1798.90.53 — it accesses ALPR data, including from community cameras). Both sections impose nearly identical obligations.

| # | Statutory Element | Statute | Policy 463 / SOP 205 | Practice | Status |
|---|---|---|---|---|---|
| 1 | Security procedures | Maintain reasonable security procedures — operational, administrative, technical, physical [§.51(a)] | 463.6: login/password, access logs. SOP: VPN, MFA for Flock [3] [4] | Flock platform controls; no logging of sharing list additions or removals [31]; no user offboarding process; no incident response procedure described | ❌ Non-compliant |
| 2 | Policy posting | Usage/privacy policy posted conspicuously on agency website [§.51(b)(1)] | 463: in policy manual since ~2020; standalone link Dec 2025. SOP 205.1 requires conspicuous posting [3] [4] | SOP 205 never posted. Policy in 400+ page manual until Dec 2025 [11] [15] | ❌ Non-compliant |
| 3 | Authorized purposes | Describe authorized purposes for ALPR use [§.51(b)(2)(A)] | 463.2: stolen vehicles, warrants, suspect interdiction. 463.4(b): "any routine patrol operation or criminal investigation" [3] | 463.2 purposes are narrow; 463.4(b) applies to "Department members" only. No authorized purposes defined for 283 external agencies [10] | ⚠️ Partial |
| 4 | Job titles | Job title or designation of authorized employees [§.51(b)(2)(B)] | 463: "members" / "authorized designee." SOP: no titles [3] [4] | Titles listed only in internal PRA response: Officers, Crime Analysts, Dispatchers, CSOs [17] | ❌ Non-compliant |
| 5 | Monitoring & compliance | How system will be monitored for security and compliance [§.51(b)(2)(C)] | 463.6(c): audits "as described in SOP 205." 463.10: monthly/quarterly [3] | No end-user audits conducted. Monthly memos review sharing config only. SOP 205 end-user audit criteria (case numbers, search justifications) apply to NCRIC, not Flock [17] [11] [21] | ❌ Non-compliant |
| 6 | Sharing process | Purposes, process, and restrictions on sharing [§.51(b)(2)(D)] | 463.8: CA public agencies only, ALPR Committee Captain review [3] | 283 agencies via Flock network, not through manual approval. § 5.3 vendor disclosure not addressed [10] [5] | ⚠️ Partial |
| 7 | Custodian title | Title of official custodian responsible for implementation [§.51(b)(2)(E)] | 463: "ALPR Committee Captain." SOP: "Support Services Captain" [3] [4] | Two conflicting titles across two documents | ⚠️ Partial |
| 8 | Accuracy | Measures to ensure accuracy and correct errors [§.51(b)(2)(F)] | 463.4(f): visual plate confirmation. 463.10: audit memos note errors [3] | No public correction process. No mechanism for external agencies to report errors [10] | ⚠️ Partial |
| 9 | Retention | Retention period and destruction process [§.51(b)(2)(G)] | 463.5: 30 days. SOP: 30-day auto-delete for Flock video [3] [4] | Retention stated and implemented. Prior 27-month policy/contract conflict now resolved [5] [1] | ✅ Addressed |
| 10 | End-user audit process | Periodic system audits for end-user data access [§.53(b)(2)(C)] | Not separately addressed for end-user data [3] [4] | Community camera data accessed via Flock; audit memos review only "our" cameras [17] [20] | ❌ Non-compliant |
| 11 | Sharing restrictions | Share only with public agencies [§.55(b)] | 463.8: CA public agencies only [3] | University of the Pacific (private, does not qualify under §.5(f)) on access list. No public agency determination by SMPD or Stockton PD; UOP Chief confirmed private institution [29]. Subsequently removed from SMPD portal; remains on Stockton [30]. § 5.3 authorizes Flock disclosure to third parties [10] [5] | ❌ Non-compliant |

**Summary:** 1 addressed, 4 partial, 6 non-compliant.

---


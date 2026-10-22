#!/usr/bin/env python3
"""Build docs/axon-video.html: the sources page for the video "San Mateo rushed into a bad Axon deal".

Every quote in the video links to its meeting video at the moment it was said, and every on-screen figure links to the
public record behind it, in the order the video shows them. Repo documents link to their GitHub copies; San Mateo
council packets (gitignored as PDFs) link to the City's PrimeGov copies.

usage: python3 scripts/build_axon_video_page.py   (fails if any linked repo file is missing)
"""
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "axon-video.html"
BLOB = "https://github.com/none-below/sm-alpr/blob/main/"
PG = "https://sanmateo.primegov.com"
HA = PG + "/api/compilemeetingattachmenthistory/historyattachment/?historyId="
REF = "assets/public-records/san-mateo/reference/"
W584 = "assets/san-mateo-public-records/W013584-100626/"
PR = "assets/public-records/"

# ── documents: key -> (label, repo path or URL) ────────────────────────────────────────────────────────────────
DOCS = {
    # San Mateo, Oct 5, 2026 (item 12, the ALPR amendment)
    "sr12": ("Oct 5, 2026 staff report, item 12", REF + "Axon_ALPR_Amendment_1_Agenda_Report_2026-10-05.pdf"),
    "sr12_pg": ("same, City copy (PrimeGov)", HA + "f551d96a-0a27-4112-90ea-33917e6ddc53"),
    "q009": ("Axon quote Q-865809-009 (Amendment No. 1, Exhibit D)", REF + "Axon_ALPR_Amendment_1_Exhibit_D_Quote_Q-865809-009_2026-10-05.pdf"),
    "q009_pg": ("same, City copy (PrimeGov)", HA + "69f24864-5ef1-440f-93c3-ae9dfd7f6e02"),
    # quote history, public records request W013584
    "q841238": ("Quote Q-841238, issued Apr 17, 2026", W584 + "2_-_Q-841238-46129TS.pdf"),
    "q003": ("Quote Q-865809-003, issued Jul 20, 2026", W584 + "1_-_Q-865809-003.pdf"),
    "q008": ("Quote Q-865809-008, issued Aug 25, 2026", W584 + "1_-_Q-865809-008.pdf"),
    "q009w": ("Quote Q-865809-009, issued Sep 5, 2026", W584 + "1_-_Q-865809-009.pdf"),
    "w584": ("City's response letter, request W013584", W584 + "PRA_Response_Letter__W013584-100626.pdf"),
    # Axon's standard terms
    "mspa253": ("Axon Master Services and Purchasing Agreement v25.3 (§2.1, §8 Free Trial)", REF + "Axon_MSPA_v25.3_2026-04-08.pdf"),
    "mspa10": ("Axon Master Services and Purchasing Agreement v1.0 (posted since July 2026; same wording)", REF + "Axon_MSPA_v1.0_2026-07-07.pdf"),
    # Flock
    "flock": ("Flock agreement, signed Aug 25, 2023 (60-month initial term)", "assets/san-mateo-public-records/W012159-021226/Flock_Agreement_2023-2028.pdf"),
    "flock_ort": ("Flock Amendment No. 1 (grant-funded cameras)", "assets/san-mateo-public-records/W012159-021226/Flock_Amendment_No_1_-_ORT.pdf"),
    # San Mateo's earlier Axon approvals (all on consent)
    "a2020": ("May 18, 2020 staff report (item 7): body cams + Tasers, $1,267,301.58; 2017 rate $151,800/yr", PG + "/meeting/document/1354.pdf?name=Agenda%20Report"),
    "a2021": ("Aug 16, 2021 staff report (item 23): in-car + interview-room video, $549,943.90", PG + "/meeting/document/2323.pdf?name=Agenda%20Report"),
    "a2022": ("Sep 19, 2022 staff report (item 15): Amendment 1, VR, $298,126.10", PG + "/meeting/document/3650.pdf?name=Agenda%20Report"),
    "a2023": ("Mar 20, 2023 staff report (item 12): Amendment 2, drones, $110,575.95", PG + "/meeting/document/4017.pdf?name=Agenda%20Report"),
    "a2024a": ("Oct 21, 2024 staff report (item 16): Amendment 3, drones, $83,348.90", HA + "e753b08f-ec70-4ec8-af53-86ab6af95dcc"),
    "a2024": ("Dec 9, 2024 staff report (item 13): $7,350,052.13 bundle, 5 + 5 years, yearly cost table", HA + "cfd21f1b-e35e-49de-bc18-0006abcaeb7c"),
    "m2024": ("Dec 9, 2024 minutes: consent calendar 5–0, item 13 = Res. 117 (2024)", REF + "Council_Minutes_Regular_Meeting_2024-12-09.pdf"),
    "p2024": ("Dec 9, 2024 packet, full text", "assets/san-mateo/council-packets/1652_2024-12-09/packet.txt"),
    # budget and Measure H
    "b0601": ("FY 2026-27 proposed budget report (Jun 1, 2026): $17.0M deficit", REF + "FY2026-27_Proposed_Budget_Report_and_Budget_Message_2026-06-01.pdf"),
    "b0615": ("FY 2026-27 budget adoption report (Jun 15, 2026): $16.8M deficit", REF + "FY2026-27_Budget_Adoption_Report_and_Budget_Message_2026-06-15.pdf"),
    "tax": ("Sales tax measure report (Jul 20, 2026): about $7 million a year", REF + "Sales_Tax_Measure_Report_Resolution_Ordinance_2026-07-20.pdf"),
    "m0720": ("Jul 20, 2026 minutes: Resolution 84 (2026) places it on the ballot", REF + "Council_Minutes_Regular_Meeting_Draft_2026-07-20.pdf"),
    "measureh": ("City of San Mateo ballot measure page (Measure H)", "https://www.cityofsanmateo.org/ballotmeasure"),
    # Axon investors
    "earnings": ("Axon Q1 2026 earnings call transcript (Axon investor site)", "https://investor.axon.com/image/Axon_Q1_2026_Earnings_Call_Transcript.pdf"),
    "earnings_dc": ("same transcript, DocumentCloud copy", "https://www.documentcloud.org/documents/28538524-axon-q1-2026-earnings-call-transcript/"),
    "webcast": ("Axon Q1 2026 earnings call webcast (May 6, 2026)", "https://vimeo.com/1189936613"),
    # other agencies' records
    "kennewick": ("Kennewick, WA: Axon quote Q-753565 (40 Outposts at $0)", PR + "kennewick-wa/reference/Axon_Quote_Q-753565_2025-11-20.pdf"),
    "knoxville": ("Knoxville, TN: Axon quote Q-786169 (27 Outposts at $0)", PR + "knoxville-tn/reference/Axon_Quote_Q-786169_2025-12-15.pdf"),
    "jupiter": ("Jupiter, FL: Axon quote Q-671876 (10 Outposts at $0)", PR + "jupiter-fl/reference/Axon_Quote_Q-671876_2025-10-02.pdf"),
    "douglas": ("Douglas County, CO: Axon quote Q-845030 (10 donor Outposts at $0; 100 at $211.98)", PR + "douglas-county-co/reference/Axon_Quote_Q-845030_2026-07-07.pdf"),
    "glenwood": ("Glenwood Springs, CO: Axon quote Q-840238 (4 Outposts at $0)", PR + "glenwood-springs-co/reference/Axon_Quote_Q-840238_2026-05-12.pdf"),
    "angels": ("Angels Camp, CA: Axon quote Q-814574 (3 Outposts at $0)", PR + "angels-camp/reference/Axon_Quote_Q-814574_2026-06-23.pdf"),
    "rialto": ("Rialto, CA: Axon quote Q-725907 (5 Lightposts at $0)", PR + "rialto/reference/Axon_Quote_Q-725907_2025-10-30.pdf"),
    "chino": ("Chino, CA: Axon quote Q-795243 (5 Outposts at $0)", PR + "chino/reference/Axon_Quote_Q-795243_2026-05-19.pdf"),
    "circleville": ("Circleville, OH: Ordinance 10-76-2025 with Axon quote Q-749055 (2 Outposts at $0)", PR + "circleville-oh/reference/Ordinance_10-76-2025_with_Axon_Quote_Q-749055_2025-09-27.pdf"),
    "woodscross": ("Woods Cross, UT: Resolution 2026-1008 with Axon quotes Q-835693", PR + "woods-cross-ut/reference/Resolution_2026-1008_Axon_Memo_and_Quotes_Q-835693_2026-07-21.pdf"),
    "grandchute": ("Grand Chute, WI: Axon memo and quote (Axon absorbs $11,500 Flock termination fee)", PR + "grand-chute-wi/reference/Axon_Agreement_Memo_and_Quote_Q-853193_2026-06-02.pdf"),
    "grandchute_flock": ("Grand Chute, WI: Flock early-termination memo", PR + "grand-chute-wi/reference/Flock_Early_Termination_Memo_2026-06-02.pdf"),
    "gardengrove": ("Garden Grove, CA: item 442 memo (5 Outposts “at no additional cost”; sign by Dec 31, 2025)", PR + "garden-grove/reference/Item_442_Axon_Agreement_Memo_2025-12-09.pdf"),
    "dalycity": ("Daly City, CA: agenda report (“no-cost pilot … through June of 2027”)", PR + "daly-city/reference/Axon_ALPR_Amendment_Agenda_Report_2026-06-08.pdf"),
    "dalycity_min": ("Daly City, CA: minutes, Resolution 26-108", PR + "daly-city/reference/Council_Minutes_Excerpt_Resolution_26-108_2026-06-08.pdf"),
    "abilene": ("Abilene, TX: Axon quote Q-863523-016 (28 Outposts inside the officer plan, no Outpost price row)", PR + "abilene-tx/reference/Axon_Quote_Q-863523-016_2026-09-30.pdf"),
    "ferndale_rca": ("Ferndale, MI: request for council action (first year deferred)", PR + "ferndale-mi/reference/Axon_ALPR_Request_for_Council_Action_2025-12-15.pdf"),
    "inglewood": ("Inglewood, CA: staff report (27% discount; option to backload payments)", PR + "inglewood/reference/Axon_Public_Safety_Technology_Package_Report_2026-04-28.pdf"),
    "richmond": ("Richmond, CA: staff presentation (first payment in FY28), DocumentCloud", "https://www.documentcloud.org/documents/28725596"),
    "durango": ("Durango, CO: Durango Herald, Sept 3, 2026", "https://www.durangoherald.com/articles/news/durango-will-end-flock-safety-contract-in-2027/"),
    "pricebook": ("Sourcewell price book, p. 29 (via Ann Arbor matter 26-1100)", PR + "ann-arbor-mi/reference/Axon_Sourcewell_Price_Book_2026-06-02.pdf"),
}

# the per-camera chart: (agency, months, net $/camera/month, doc path)
PERCAM = [
    ("Reno, NV (pending)", 96, 225.64, PR + "reno-nv/reference/Axon_Quote_Q-906822-019_2026-10-09.pdf"),
    ("San Mateo: 14 added in 2027", 86, 222.01, REF + "Axon_ALPR_Amendment_1_Exhibit_D_Quote_Q-865809-009_2026-10-05.pdf"),
    ("Bristol, CT", 54, 221.43, PR + "bristol-ct/reference/PD_Sole_Source_Letter_and_Axon_Quote_Q-865784_2026-08-20.pdf"),
    ("Sun Prairie, WI (estimate)", 120, 217.95, PR + "sun-prairie-wi/reference/Axon_Budgetary_Estimate_Q-932192-001_2026-09-22.pdf"),
    ("San Mateo: first 40", 98, 215.64, REF + "Axon_ALPR_Amendment_1_Exhibit_D_Quote_Q-865809-009_2026-10-05.pdf"),
    ("Douglas County, CO", 120, 211.98, PR + "douglas-county-co/reference/Axon_Quote_Q-845030_2026-07-07.pdf"),
    ("Plymouth, MN", 120, 211.98, PR + "plymouth-mn/reference/Agenda_6.11_Staff_Report_with_Axon_Quote_Q-789773_2026-06-23.pdf"),
    ("Ferndale, MI", 60, 199.94, PR + "ferndale-mi/reference/Axon_Quote_Q-775929_2025-11-13.pdf"),
    ("Plantation, FL", 60, 199.94, PR + "plantation-fl/reference/Axon_Amendment_1_Midtown_Quote_Q-818490_2026-03-12.pdf"),
    ("San Bernardino, CA", 60, 199.36, PR + "san-bernardino/reference/Axon_Quote_Q-800742_2026-03-05.pdf"),
    ("South Pasadena, CA (offer)", 60, 199.36, PR + "south-pasadena/reference/Item_15_ALPR_Agenda_Report_with_Axon_Quote_Q-823902_2026-08-19.pdf"),
    ("Englewood, CO (failed 3–3)", 60, 199.36, PR + "englewood-co/reference/Axon_Quote_Q-864235-001_2026-08-28.pdf"),
    ("Duncanville, TX", 60, 199.36, PR + "duncanville-tx/reference/Axon_Quote_Q-895527-002_2026-08-24.pdf"),
    ("Junction City, OR (offer)", 60, 183.27, PR + "junction-city-or/reference/Axon_ALPR_Presentation_Item_with_Quotes_2025-10-28.pdf"),
    ("Woods Cross, UT", 120, 132.49, PR + "woods-cross-ut/reference/Resolution_2026-1008_Axon_Memo_and_Quotes_Q-835693_2026-07-21.pdf"),
    ("Charleston, SC", 60, 100.60, PR + "charleston-sc/reference/Axon_Quote_Q-686825_Signed_2026-02-09.pdf"),
]

CLIPS = json.loads(r'''{
 "h_generous": {
  "role": "Police Chief",
  "place": "San Mateo, CA",
  "when": "City Council, Oct 5, 2026",
  "url": "https://www.youtube.com/watch?v=A9YdDQOmTwE",
  "t": 5833,
  "text": "This is a very generous offer by Axon"
 },
 "h_knox": {
  "role": "Council Member Amelia Parker",
  "place": "Knoxville, TN",
  "when": "City Council, Jan 20, 2026",
  "url": "https://www.youtube.com/watch?v=UfBZI1KyHVI",
  "t": 1675,
  "text": "One of my concerns with Axon are the ongoing price increases"
 },
 "h_lure": {
  "role": "Finance Director Jamar Kelly",
  "place": "Reading, PA",
  "when": "Council committee, Aug 18, 2025 · Axon renewal",
  "url": "https://www.youtube.com/watch?v=a6Ej4XcMaLA",
  "t": 4896,
  "text": "They lure you in with the initial contract and sweeteners."
 },
 "h_nash": {
  "role": "Council Member Delishia Porterfield",
  "place": "Nashville, TN",
  "when": "Metro Council, Dec 5, 2023 · Axon Tasers",
  "url": "https://www.youtube.com/watch?v=xfHXR8v6jKA",
  "t": 6944,
  "text": "That they won't be back in another six months saying that this technology is now antiquated and they need more."
 },
 "knoxprice": {
  "role": "Council Member Amelia Parker",
  "place": "Knoxville, TN",
  "when": "City Council, Jan 20, 2026",
  "url": "https://www.youtube.com/watch?v=UfBZI1KyHVI",
  "t": 1675,
  "text": "One of my concerns with Axon are the ongoing price increases that happen as new technology is acquired, as changes are made to these subscription services. And within one year, we've already seen that happening."
 },
 "evan_newprod": {
  "role": "Council Member Clare Kelly",
  "place": "Evanston, IL",
  "when": "City Council, Dec 8, 2025 · Axon contract",
  "url": "https://www.youtube.com/watch?v=lyh-ZWWZZ-A",
  "t": 4859,
  "text": "I would like to know which are these just increased prices on what we already have and what are new products."
 },
 "balt_services": {
  "role": "Police Department presenter",
  "place": "Baltimore, MD",
  "when": "Board of Estimates, May 6, 2026 · Axon contract",
  "url": "https://www.youtube.com/watch?v=KZXRlhMZPwA",
  "t": 8392,
  "text": "The spend has increased over time and that's because I believe that Axon has increased the amount of services that they provide with their product offering."
 },
 "nash_3x": {
  "role": "Council Member Delishia Porterfield",
  "place": "Nashville, TN",
  "when": "Metro Council, Dec 5, 2023 · Axon Tasers",
  "url": "https://www.youtube.com/watch?v=xfHXR8v6jKA",
  "t": 6769,
  "text": "Less than one year and 10 months ago, we entered into a five-year contract for TASERS. They have a five-year lifespan for 1,400 TASERS for a total of $6.5 million. Today, we have MMPD requesting more, newer, and better TASERS with a hefty price tag of $18 .3 million."
 },
 "nash_more": {
  "role": "Council Member Delishia Porterfield",
  "place": "Nashville, TN",
  "when": "Metro Council, Dec 5, 2023 · Axon Tasers",
  "url": "https://www.youtube.com/watch?v=xfHXR8v6jKA",
  "t": 6944,
  "text": "And there is no guarantee that if you pass this, that they won't be back in another six months saying that this technology is now antiquated and they need more."
 },
 "reading_lure": {
  "role": "Finance Director Jamar Kelly",
  "place": "Reading, PA",
  "when": "Council committee, Aug 18, 2025 · Axon renewal",
  "url": "https://www.youtube.com/watch?v=a6Ej4XcMaLA",
  "t": 4894,
  "text": "This is an industry trend with Axon. They lure you in with the initial contract and sweeteners. And then on the back end, the data storage is where they really make their money."
 },
 "sanjose_dep": {
  "role": "Mayor Matt Mahan",
  "place": "San Jose, CA",
  "when": "City Council, Oct 21, 2025 · Axon evidence contract",
  "url": "https://www.youtube.com/watch?v=A993VSuqNtY",
  "t": 7959,
  "text": "One concern I have that I just have to give voice to is platform dependence. And it's not clear to me how competitive this market is."
 },
 "sm_close": {
  "role": "Police Chief",
  "place": "San Mateo, CA",
  "when": "City Council, Oct 5, 2026",
  "url": "https://www.youtube.com/watch?v=A9YdDQOmTwE",
  "t": 5833,
  "text": "This is a very generous offer by Axon and I don't know how long it would be on the table."
 },
 "n_intro": {
  "role": "Resident, public comment",
  "place": "San Mateo, CA",
  "when": "City Council, Oct 19, 2026",
  "url": "https://www.youtube.com/watch?v=CFkMlUTYct0",
  "t": 2851,
  "text": "So two weeks ago you approved the Axon camera deal and I just wanted to clarify some things that you were told versus what's on the record."
 },
 "n_others_a": {
  "role": "Resident, public comment",
  "place": "San Mateo, CA",
  "when": "City Council, Oct 19, 2026",
  "url": "https://www.youtube.com/watch?v=CFkMlUTYct0",
  "t": 2882,
  "text": "The chief also said that they are not making this offer to everyone. In June, Daly City approved a no-cost pilot of Axon's cameras, and I found 35 other agencies across the country who have similar Axon camera deals."
 },
 "n_others_b": {
  "role": "Resident, public comment",
  "place": "San Mateo, CA",
  "when": "City Council, Oct 19, 2026",
  "url": "https://www.youtube.com/watch?v=CFkMlUTYct0",
  "t": 2899,
  "text": "And many of them had more free cameras and a really free trial."
 },
 "sm_others": {
  "role": "Police Chief",
  "place": "San Mateo, CA",
  "when": "City Council, Oct 5, 2026",
  "url": "https://www.youtube.com/watch?v=A9YdDQOmTwE",
  "t": 5858,
  "text": "I'm frankly very concerned that they have other places to go should we not look to this as an opportunity."
 },
 "p_table": {
  "role": "Mayor Adam Loraine",
  "place": "San Mateo, CA",
  "when": "City Council, Oct 5, 2026",
  "url": "https://www.youtube.com/watch?v=A9YdDQOmTwE",
  "t": 6164,
  "text": "So I'm just going to humbly move that we consider tabling this for a month or longer with the idea that we would like to have at least one town hall meeting before we engage in this."
 },
 "p_dead": {
  "role": "Mayor Loraine asks; the Police Chief answers",
  "place": "San Mateo, CA",
  "when": "City Council, Oct 5, 2026",
  "url": "https://www.youtube.com/watch?v=A9YdDQOmTwE",
  "t": 5874,
  "text": "Even one month, do you think, would be too long to wait? We've been negotiating with them for some time. I think it would be difficult. I can't give you a definite answer. I mean, they're running a business. I understand that. I don't know. I don't know whether we'd be back in front of you again."
 },
 "n_deadlines": {
  "role": "Resident, public comment",
  "place": "San Mateo, CA",
  "when": "City Council, Oct 19, 2026",
  "url": "https://www.youtube.com/watch?v=CFkMlUTYct0",
  "t": 2869,
  "text": "I reviewed the quote history for the contract and found that there were multiple deadlines, June, September, October. Each time, Axon sent a new version of the quote with a later deadline. The price never went up."
 },
 "nashfomo": {
  "role": "Council Member Quin Evans Segall",
  "place": "Nashville, TN",
  "when": "Metro Council, Dec 5, 2023 · Axon Tasers",
  "url": "https://www.youtube.com/watch?v=xfHXR8v6jKA",
  "t": 7098,
  "text": "This causes them to feel something called anticipatory regret. If we do not take this deal today, we will have FOMO and we will lose this amazing deal."
 },
 "p_nocost2": {
  "role": "Police Chief, answering Deputy Mayor Fernandez",
  "place": "San Mateo, CA",
  "when": "Study session, Oct 5, 2026",
  "url": "https://www.youtube.com/watch?v=fESvU2-vmF8",
  "t": 4947,
  "text": "So there will be no cost for the Axon system throughout the life of the Flock contract."
 },
 "n_nocost": {
  "role": "Resident, public comment",
  "place": "San Mateo, CA",
  "when": "City Council, Oct 19, 2026",
  "url": "https://www.youtube.com/watch?v=CFkMlUTYct0",
  "t": 2914,
  "text": "But the quote on paper charges for 40 cameras every month starting November 15th of this year. Only five are free. Kennewick got 40 free cameras. And the only difference is the payments for those cameras are deferred to 2028."
 },
 "p_nocost1": {
  "role": "Police Chief",
  "place": "San Mateo, CA",
  "when": "City Council, Oct 5, 2026",
  "url": "https://www.youtube.com/watch?v=A9YdDQOmTwE",
  "t": 5838,
  "text": "I mean, it's not very often that we get someone who's willing to provide a service for such an extended period of time at no cost."
 },
 "p_open2": {
  "role": "Council Member Newsom",
  "place": "San Mateo, CA",
  "when": "City Council, Oct 5, 2026",
  "url": "https://www.youtube.com/watch?v=A9YdDQOmTwE",
  "t": 6203,
  "text": "I think the contract is open-ended, so we have a full year to decide whether we're going to pursue it. Based on that, I would recommend that we don't lose the opportunity."
 },
 "n_open": {
  "role": "Resident, public comment",
  "place": "San Mateo, CA",
  "when": "City Council, Oct 19, 2026",
  "url": "https://www.youtube.com/watch?v=CFkMlUTYct0",
  "t": 2936,
  "text": "It is a fixed 98 month contract to January 2035. The only way out without paying for the hardware is to give notice by this next September. If we wait a day past that, we are paying from this coming November, you know, from a few weeks from now."
 },
 "p_motion": {
  "role": "The motion, as stated on the dais",
  "place": "San Mateo, CA",
  "when": "City Council, Oct 5, 2026",
  "url": "https://www.youtube.com/watch?v=A9YdDQOmTwE",
  "t": 6680,
  "text": "And then have a check-in to get the assessment from the police department on Axon before the expiration of the contract. Just to clarify, I think before the expiration of this trial period. The pilot period, yes."
 },
 "n_trial": {
  "role": "Resident, public comment",
  "place": "San Mateo, CA",
  "when": "City Council, Oct 19, 2026",
  "url": "https://www.youtube.com/watch?v=CFkMlUTYct0",
  "t": 2956,
  "text": "The approval motion called it a trial and a pilot and Axon's contract does have a block that talks about trials, but we did not use that portion. We just signed up for this thing with the five free cameras and delayed first payment."
 },
 "cwirko_gain_s": {
  "role": "Council Member Danielle Cwirko-Godycki",
  "place": "San Mateo, CA",
  "when": "City Council, Oct 5, 2026",
  "url": "https://www.youtube.com/watch?v=A9YdDQOmTwE",
  "t": 6417,
  "text": "What would we gain from revisiting this in six to eight months?"
 },
 "sm_duel": {
  "role": "Police Chief",
  "place": "San Mateo, CA",
  "when": "City Council, Oct 5, 2026",
  "url": "https://www.youtube.com/watch?v=A9YdDQOmTwE",
  "t": 5798,
  "text": "It would not exceed one year and if I had to throw out a guess, I'd throw it out somewhere between the six and nine month range to get a good sample size of whether or not we see an advantage to one system over the other."
 },
 "p_foot": {
  "role": "Police Chief",
  "place": "San Mateo, CA",
  "when": "Study session, Oct 5, 2026",
  "url": "https://www.youtube.com/watch?v=fESvU2-vmF8",
  "t": 2965,
  "text": "The proposed Axon agreement does not expand our existing technology footprint."
 },
 "axfeatures": {
  "role": "Axon President Josh Isner",
  "place": "Axon",
  "when": "Q1 2026 earnings call, May 6, 2026",
  "url": "https://vimeo.com/1189936613",
  "t": null,
  "text": "So the more features we add, you should expect that to be reflected in the price as we revise it each year."
 },
 "axplan": {
  "role": "Axon CEO Rick Smith",
  "place": "Axon",
  "when": "Q1 2026 earnings call, May 6, 2026",
  "url": "https://vimeo.com/1189936613",
  "t": null,
  "text": "Axon Vision is new. You didn't know about it when you signed your contract. Frankly, maybe we didn't either, but you're going to get it if you're on that plane."
 },
 "knox_rushed": {
  "role": "Council Member Amelia Parker",
  "place": "Knoxville, TN",
  "when": "City Council, Jan 20, 2026",
  "url": "https://www.youtube.com/watch?v=UfBZI1KyHVI",
  "t": 1717,
  "text": "And so we rushed into a contract last year, $27 million contract, only $2 million covered by grants, $25 million coming from local city dollars. Now we're asked to increase the contract again this year."
 },
 "sm_item13": {
  "role": "City Clerk, reading the consent calendar",
  "place": "San Mateo, CA",
  "when": "City Council, Dec 9, 2024",
  "url": "https://www.youtube.com/watch?v=X5i23DyzeV8",
  "t": 267,
  "text": "Item number 13, Axon Master Services and Purchasing Agreement approval."
 },
 "sm_consent": {
  "role": "Mayor Diaz Nash and the City Clerk",
  "place": "San Mateo, CA",
  "when": "City Council, Dec 9, 2024",
  "url": "https://www.youtube.com/watch?v=X5i23DyzeV8",
  "t": 644,
  "text": "So is there a motion from council to approve the consent calendar minus 18, 19, and 25? So moved. Second. Thank you. City Clerk, could you please call the roll? Certainly. Council Member Loraine? Yes. Deputy Mayor Newsom? Yes. Council Member Hedges? Yes. Council Member Lee? Yes. Mayor Diaz Nash? Yes. Motion carries 5-0."
 },
 "sm_vote": {
  "role": "Roll call",
  "place": "San Mateo, CA",
  "when": "City Council, Oct 5, 2026",
  "url": "https://www.youtube.com/watch?v=A9YdDQOmTwE",
  "t": 6710,
  "text": "Deputy Mayor Fernandez? Yes. Councilmember Cwirko-Godycki? Yes. Councilmember Diaz Nash? Yes. Councilmember Newsom? Yes. Mayor Loraine? No. Motion carries."
 },
 "sm_open": {
  "role": "Police Chief",
  "place": "San Mateo, CA",
  "when": "City Council, Oct 5, 2026",
  "url": "https://www.youtube.com/watch?v=A9YdDQOmTwE",
  "t": 5848,
  "text": "We had been negotiating with them for a long time to get to this point. And they are not making this offer to everyone."
 },
 "gg": {
  "role": "Deputy Police Chief",
  "place": "Garden Grove, CA",
  "when": "City Council, Dec 9, 2025",
  "url": "https://www.youtube.com/watch?v=ho89U-CxmM0",
  "t": 4278,
  "text": "Axon advised the police department of a new pricing model that they were implementing to all new customers and contract renewals."
 },
 "dc": {
  "role": "Police Chief",
  "place": "Daly City, CA",
  "when": "City Council, June 8, 2026",
  "url": "https://cpmedia.azureedge.net/dalycityca/0395b732d4.mp4",
  "t": 4425,
  "text": "Agreement includes a no-cost pilot of Axon's fixed ALPR solutions through June of 2027. If the pilot is successful, the first payment of $120,000 will be due in June of 2027."
 },
 "tempe": {
  "role": "Mayor",
  "place": "Tempe, AZ",
  "when": "City Council, Mar 26, 2026",
  "url": "https://www.youtube.com/watch?v=R8hr1letpYo",
  "t": 2900,
  "text": "That's why we are piloting this trial with Axon at no cost to the city of Tempe taxpayer."
 },
 "elk2": {
  "role": "Interim Police Chief",
  "place": "Elkhart, IN",
  "when": "Board of Public Safety, Aug 11, 2026",
  "url": "https://www.youtube.com/watch?v=yASJBcUKaq0",
  "t": 216,
  "text": "As you know, Axon is a primary system we use with body cams, evidence, and a handful of other softwares in the building. So we'd be looking at their LPR cameras to help consolidate systems."
 },
 "eng": {
  "role": "Police Department presenter",
  "place": "Englewood, CO",
  "when": "City Council, Aug 24, 2026",
  "url": "https://www.youtube.com/watch?v=YKhQoASL1Hg",
  "t": 5845,
  "text": "Leader pricing is what they call it where you either get 20 off every year or you get your first year free"
 },
 "fern": {
  "role": "Police Chief",
  "place": "Ferndale, MI",
  "when": "City Council, Dec 15, 2025",
  "url": "https://www.youtube.com/watch?v=JNwENHITTC8",
  "t": 13706,
  "text": "The cost of year one is deferred"
 },
 "rich": {
  "role": "Police Dept. senior accountant",
  "place": "Richmond, CA",
  "when": "City Council, Oct 6, 2026",
  "url": "https://www.youtube.com/watch?v=hoYsr2Df9Ck",
  "t": 18892,
  "text": "Based on actual installation timing of the proposed first year payment will occur in fiscal year of 2028."
 },
 "ingl": {
  "role": "Police Chief",
  "place": "Inglewood, CA",
  "when": "City Council, Apr 28, 2026",
  "url": "https://www.youtube.com/watch?v=5TE6wQe0K4I",
  "t": 1311,
  "text": "Mayor is a tough negotiator we got a very good price on this"
 },
 "abi": {
  "role": "Police Chief",
  "place": "Abilene, TX",
  "when": "City Council, Sep 24, 2026",
  "url": "https://abilenetx.new.swagit.com/videos/402153",
  "t": 5746,
  "text": "It will not change the contract. They threw that in for the price it was already agreed to."
 },
 "gc": {
  "role": "Police Chief",
  "place": "Grand Chute, WI",
  "when": "Town Board, June 2, 2026",
  "url": "https://www.youtube.com/watch?v=7-PQFg18P4w",
  "t": 3524,
  "text": "In total, Axon has applied more than $75,000 in discounts to this proposal."
 },
 "dur2": {
  "role": "Police Chief",
  "place": "Durango, CO",
  "when": "City Council, Sep 1, 2026",
  "url": "https://www.youtube.com/watch?v=m7pTggr5zAo",
  "t": 1173,
  "text": "So we're not going to renew our flock contract in 2027. We're going to transition to Axon, which is a longstanding partner."
 },
 "knox2": {
  "role": "Mayor",
  "place": "Knoxville, TN",
  "when": "City Council, Jan 20, 2026",
  "url": "https://www.youtube.com/watch?v=UfBZI1KyHVI",
  "t": 1197,
  "text": "And put the license plate readers under the same bundled contract."
 },
 "kil2": {
  "role": "Police Chief",
  "place": "Killeen, TX",
  "when": "City Council, Oct 8, 2026",
  "url": "https://www.youtube.com/watch?v=EwADlnWHAhA",
  "t": 5431,
  "text": "The proposed agreement extends through 2036 and incorporates additional products and services, the drones, the outposts, and the VR systems."
 },
 "ant": {
  "role": "Police Lieutenant",
  "place": "Antioch, CA",
  "when": "City Council, Aug 25, 2026",
  "url": "https://www.youtube.com/watch?v=VTsH-VqMgUM",
  "t": 23869,
  "text": "They have other folks ahead of us in line, essentially. There are other communities in the Bay Area that are signing on to their platform. So we basically fall in line with."
 },
 "chat": {
  "role": "Council Vice Chair, police reply",
  "place": "Chattanooga, TN",
  "when": "City Council, Sep 22, 2026",
  "url": "https://www.youtube.com/watch?v=PFKmzsPbZiA",
  "t": 812,
  "text": "They are trying to offer this for free. It's almost like a sales pitch, pretty much. That is my understanding."
 },
 "axq": {
  "role": "J.P. Morgan analyst, to Axon",
  "place": "Axon",
  "when": "Q1 2026 earnings call, May 6, 2026",
  "url": "https://vimeo.com/1189936613",
  "t": null,
  "text": "Are you actually seeing folks rip and replace hardware to essentially install Outpost"
 },
 "axyes2": {
  "role": "Axon President Josh Isner",
  "place": "Axon",
  "when": "Q1 2026 earnings call, May 6, 2026",
  "url": "https://vimeo.com/1189936613",
  "t": null,
  "text": "We absolutely are."
 },
 "axgateway": {
  "role": "Axon President Josh Isner",
  "place": "Axon",
  "when": "Q1 2026 earnings call, May 6, 2026",
  "url": "https://vimeo.com/1189936613",
  "t": null,
  "text": "It's a new sensor in this broader ecosystem you can run ai on it at the edge you uh you have immediate utility from the plate reads um and it fits in this broader play with axon vision"
 },
 "aximmat": {
  "role": "Axon COO and CFO Brittany Bagley",
  "place": "Axon",
  "when": "Q1 2026 earnings call, May 6, 2026",
  "url": "https://vimeo.com/1189936613",
  "t": null,
  "text": "One of the fun things about talking about Prepared and Carbyne and, you know, Outpost is that these are all things that are not called out in our revenue commentary because they're still immaterial to our revenue in this quarter. So we did 34% growth without any of these, and these are amazing drivers to support our long-term growth in our future."
 },
 "axearly": {
  "role": "Axon President Josh Isner",
  "place": "Axon",
  "when": "Q1 2026 earnings call, May 6, 2026",
  "url": "https://vimeo.com/1189936613",
  "t": null,
  "text": "It serves customers well to get in early. And, you know, the longer you wait, the more the sum of the parts adds up."
 }
}''')

# ── the page, in video order: (chapter time, title, [entries]) ─────────────────────────────────────────────────
# entry: ("clip", key) | ("claim", text, [doc keys]) | ("note", text) | ("percam",)
SECTIONS = [
    ("0:00", "The opening", [
        ("clip", "h_generous"), ("clip", "h_knox"), ("clip", "h_lure"), ("clip", "h_nash"),
        ("claim", "San Mateo’s Axon bill, per year: $151,800 in 2017; just under $450,000 in 2024; $705,754 from FY 2026-27; "
                  "about $850,755 once ALPR payments start in June 2028.", ["a2020", "a2024", "sr12", "q009"]),
    ]),
    ("1:03", "Other cities: the Axon bill grows", [
        ("clip", "knoxprice"), ("clip", "evan_newprod"), ("clip", "balt_services"), ("clip", "nash_3x"), ("clip", "nash_more"),
        ("clip", "reading_lure"), ("clip", "sanjose_dep"),
    ]),
    ("2:30", "The Chief’s offer and the 4–1 vote", [
        ("clip", "sm_close"), ("clip", "sm_vote"),
        ("claim", "Approved Oct 5, 2026, 4–1: Amendment No. 1 adding Axon license plate cameras, $1,145,260.45 over 98 months.", ["sr12", "sr12_pg", "q009"]),
        ("note", "Minutes for Oct 5, 2026 were not yet published when this page was made; the roll call is in the meeting video."),
    ]),
    ("2:51", "Public comment, Oct 19, 2026", [("clip", "n_intro")]),
    ("3:22", "The same deal, in other cities", [
        ("clip", "sm_open"), ("clip", "gg"), ("claim", "Garden Grove: 5 Outposts at no additional cost, with a signing deadline.", ["gardengrove"]),
        ("clip", "dc"), ("claim", "Daly City: a “no-cost pilot” through June 2027; first payment due June 2027 if the pilot succeeds.", ["dalycity", "dalycity_min"]),
        ("clip", "tempe"), ("clip", "elk2"), ("clip", "eng"),
        ("clip", "fern"), ("claim", "Ferndale: year one deferred.", ["ferndale_rca"]),
        ("clip", "rich"), ("claim", "Richmond: first payment in FY28.", ["richmond"]),
        ("clip", "ingl"), ("claim", "Inglewood: 27% discount and an option to backload payments.", ["inglewood"]),
        ("clip", "abi"), ("claim", "Abilene: 28 Outposts inside the officer plan, with no Outpost price row.", ["abilene"]),
        ("clip", "gc"), ("claim", "Grand Chute: Axon absorbed the $11,500 Flock termination fee.", ["grandchute", "grandchute_flock"]),
        ("clip", "dur2"), ("claim", "Durango: the Chief asked Axon not to charge until the last Flock contract ends.", ["durango"]),
        ("clip", "knox2"), ("clip", "kil2"), ("clip", "ant"), ("clip", "chat"),
        ("clip", "axq"), ("clip", "axyes2"), ("clip", "axgateway"), ("clip", "aximmat"), ("clip", "axearly"),
        ("claim", "Axon’s executives, to investors (May 6, 2026).", ["earnings", "earnings_dc", "webcast"]),
        ("clip", "n_others_a"), ("clip", "n_others_b"),
    ]),
    ("6:08", "“Other places to go”?", [
        ("clip", "sm_others"),
        ("claim", "San Mateo: 5 of 45 cameras free (the “donor” line); the rest charged.", ["q009"]),
        ("claim", "Cameras priced at $0 on other agencies’ quotes: Kennewick 40, Knoxville 27, Jupiter 10, Douglas County 10, "
                  "Glenwood Springs 4, Angels Camp 3, Rialto 5 Lightposts, Chino 5, Circleville 2, Woods Cross 1 Lightpost.",
         ["kennewick", "knoxville", "jupiter", "douglas", "glenwood", "angels", "rialto", "chino", "circleville", "woodscross"]),
        ("claim", "Grand Chute, WI: Axon paid the $11,500 Flock exit fee.", ["grandchute"]),
    ]),
    ("6:21", "Was it rushed? The deadlines", [
        ("clip", "p_table"),
        ("note", "Not tabled: approved that night, 4–1, with a community meeting “by January 2027” (after the vote, not before)."),
        ("clip", "p_dead"), ("clip", "n_deadlines"),
        ("claim", "The deadlines: the Apr 17 quote expired June 19; the Jul 20 quote expired Sept 29; the Sept 5 revision expired Oct 16. "
                  "The total never went up: $1,169,541, then $1,145,260.", ["q841238", "q003", "q008", "q009w", "w584"]),
        ("clip", "nashfomo"),
    ]),
    ("7:21", "“No cost”? The price per camera", [
        ("clip", "p_nocost2"), ("clip", "n_nocost"),
        ("claim", "The quote charges 40 cameras for all 98 months from Nov 15, 2026 (and 14 more for 86 months); only 5 are free. "
                  "Payments start June 1, 2028: deferred, not waived.", ["q009", "q009_pg", "kennewick", "knoxville"]),
        ("clip", "p_nocost1"), ("percam",),
    ]),
    ("7:56", "Locked in for 98 months", [
        ("clip", "p_open2"),
        ("claim", "Fixed 98-month term, Nov 15, 2026 to Jan 14, 2035. Leaving penalty-free takes 60 days’ notice within the first "
                  "12 months (about Sept 15, 2027, a derived date); after that, “MSRP reconciliation” on the discount.", ["q009", "sr12"]),
        ("clip", "n_open"),
        ("claim", "The term starts on the quote’s Contract Start date (Nov 15, 2026) whether or not cameras are installed: "
                  "Axon’s terms say subscriptions “begin on the date stated in the Quote.”", ["q009", "mspa253", "mspa10"]),
    ]),
    ("8:26", "A “trial”?", [
        ("clip", "p_motion"), ("clip", "n_trial"),
        ("claim", "Axon’s own Free Trial: cancel on 10 business days’ notice, hardware goes back. San Mateo’s deal isn’t that: "
                  "60 days’ notice on a 98-month term.", ["mspa253", "mspa10", "sr12", "q009"]),
    ]),
    ("9:00", "Two license plate systems at once", [
        ("clip", "cwirko_gain_s"), ("clip", "sm_duel"),
        ("claim", "Axon is to run “concurrently with the existing Flock system” in the “same general locations.”", ["sr12"]),
        ("claim", "Flock’s contract: signed Aug 25, 2023 for a 60-month initial term (so through Aug 2028, a derived date; the staff report "
                  "says “through 2028”), with no termination for convenience. A separate grant-funded Flock order renews on its own schedule.",
         ["flock", "flock_ort", "sr12"]),
        ("clip", "p_foot"),
    ]),
    ("9:24", "Axon’s pitch to investors", [
        ("clip", "axfeatures"), ("clip", "axplan"), ("claim", "Axon Q1 2026 earnings call, May 6, 2026.", ["earnings", "earnings_dc", "webcast"]),
    ]),
    ("9:40", "San Mateo’s own Axon history", [
        ("clip", "knox_rushed"),
        ("claim", "San Mateo’s Axon contract total: $7,350,052 (Dec 2024); $8,495,313 with this deal (Oct 2026).", ["a2024", "sr12"]),
        ("clip", "sm_item13"), ("clip", "sm_consent"),
        ("claim", "Six Axon approvals since 2020, all on the consent calendar.", ["a2020", "a2021", "a2022", "a2023", "a2024a", "a2024", "m2024", "p2024"]),
        ("claim", "FY 2026-27 General Fund deficit: $16.8 million ($17.0M proposed).", ["b0601", "b0615"]),
        ("claim", "Measure H, Nov 3, 2026: a ¼-cent sales tax, about $7 million a year. Axon at about $850,755 a year from 2028 is about "
                  "1 in 8 of those dollars.", ["tax", "m0720", "measureh", "a2024", "sr12"]),
    ]),
    ("10:29", "The vote and the exit window", [
        ("clip", "sm_vote"),
        ("claim", "The penalty-free exit closes about Sept 15, 2027 (60 days before month 12); after that, the contract runs to Jan 14, 2035.", ["q009", "sr12"]),
    ]),
    ("10:44", "Why it’s a bad deal, and who voted for it", [
        ("note", "Rushed: approved before any town hall, while still bound to Flock until Aug 2028; Axon’s deadline had already moved twice. "
                 "Locked in: 98 months, to 2035; the penalty-free exit closes 10 months in. Not free: charged from Nov 15, 2026, installed "
                 "or not; the first bill comes in June 2028. Not a better price: Axon’s usual discount is 20% off list (Englewood’s presenter "
                 "above: “20 off every year or … your first year free”); most agencies got at least that; San Mateo got 17% or less."),
        ("note", "Oct 5, 2026 roll call: Yes — Deputy Mayor Nicole Fernandez, Council Members Danielle Cwirko-Godycki, Lisa Diaz Nash and "
                 "Robert Newsom Jr. No — Mayor Adam Loraine, who had moved to wait a month for a town hall."),
    ]),
]

INTRO = ("Every quote in the video links to the meeting video at the moment it was said, and every on-screen figure links to the "
         "public record behind it, in the order the video shows them. Dates marked “derived” are calculated from the "
         "documents rather than printed in them. Per-camera comparisons use each agency’s Axon quote; some were offers or drafts, "
         "as labeled.")
DISCLOSURE = ("Disclosure: I'm a San Mateo resident. This video is part of a public-records project on San Mateo's license plate cameras "
              "that I've worked on since January 2026. While working on this video, I donated to Mayor Loraine's campaign, and I've also "
              "donated to Seema Patel's campaign (she is running against Council Member Newsom). I made this video on my own, unpaid, and "
              "it is not coordinated with any campaign.")


FIXES = [(r"\bMMPD\b", "MNPD"), (r"\$(\d+) \.(\d)", r"$\1.\2"), (r"(\d) ,(\d)", r"\1,\2"), (r" -(\w)", r"-\1")]


def clean(text):
    """speech-to-text artifacts in the transcripts ("$18 .3", "MMPD" for Metro Nashville PD)"""
    for pat, rep in FIXES:
        text = re.sub(pat, rep, text)
    return text


def doc_url(target):
    return target if target.startswith("http") else BLOB + target


def ts(t):
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def video_link(c):
    url, t = c.get("url"), c.get("t")
    if not url:
        return ""
    if t is None:
        return url, "watch the webcast"
    if "youtube.com" in url:
        return f"{url}&t={t}s", f"watch at {ts(t)}"
    if "swagit.com" in url:
        return f"{url}?ts={t}", f"watch at {ts(t)}"
    return f"{url}#t={t}", f"watch at {ts(t)}"


def check():
    missing = [p for _, p in DOCS.values() if not p.startswith("http") and not (ROOT / p).exists()]
    missing += [p for *_, p in PERCAM if not (ROOT / p).exists()]
    keys = {e[1] for _, _, es in SECTIONS for e in es if e[0] == "clip"}
    missing += [f"clip:{k}" for k in keys if k not in CLIPS]
    missing += [f"doc:{d}" for _, _, es in SECTIONS for e in es if e[0] == "claim" for d in e[2] if d not in DOCS]
    if missing:
        sys.exit("missing:\n  " + "\n  ".join(missing))


def render():
    e = html.escape
    out = []
    for when, title, entries in SECTIONS:
        out.append(f'<section><h2><span class="t">{e(when)}</span> {e(title)}</h2>')
        for ent in entries:
            if ent[0] == "clip":
                c = CLIPS[ent[1]]
                link = video_link(c)
                who = " · ".join(x for x in (c.get("role"), c.get("place"), c.get("when")) if x)
                a = f' <a href="{e(link[0])}">{e(link[1])}</a>' if link else ""
                out.append(f'<div class="clip"><blockquote>“{e(clean(c["text"]))}”</blockquote><div class="who">{e(who)}{a}</div></div>')
            elif ent[0] == "claim":
                docs = "".join(f'<li><a href="{e(doc_url(DOCS[d][1]))}">{e(DOCS[d][0])}</a></li>' for d in ent[2])
                out.append(f'<div class="claim"><p>{e(ent[1])}</p><ul>{docs}</ul></div>')
            elif ent[0] == "note":
                out.append(f'<div class="note"><p>{e(ent[1])}</p></div>')
            elif ent[0] == "percam":
                rows = "".join(f'<tr{" class=sm" if a.startswith("San Mateo") else ""}><td><a href="{e(doc_url(p))}">{e(a)}</a></td>'
                               f'<td>{m}</td><td>${v:,.2f}</td></tr>' for a, m, v, p in PERCAM)
                out.append('<div class="claim"><p>Price per Outpost camera, per month, from each agency’s Axon quote '
                           '(11 of 14 others were quoted less than San Mateo; longer terms include more hardware refreshes):</p>'
                           f'<table><tr><th>Agency (links to quote)</th><th>Months</th><th>Net $/camera/month</th></tr>{rows}</table>'
                           f'<ul><li><a href="{e(doc_url(DOCS["pricebook"][1]))}">{e(DOCS["pricebook"][0])}</a></li></ul></div>')
        out.append("</section>")
    return "\n".join(out)


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' https://gc.zgo.at; connect-src 'self' https://gc.zgo.at https://none-below.goatcounter.com;">
<title>Sources: San Mateo rushed into a bad Axon deal</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, system-ui, sans-serif; background: #0f172a; color: #e2e8f0; line-height: 1.5; }
  main { max-width: 860px; margin: 0 auto; padding: 32px 16px 64px; }
  h1 { font-size: 26px; color: #f8fafc; margin: 0 0 8px; }
  h2 { font-size: 19px; color: #f8fafc; margin: 36px 0 12px; border-bottom: 1px solid #334155; padding-bottom: 6px; }
  h2 .t { color: #fbbf24; font-variant-numeric: tabular-nums; margin-right: 6px; }
  p.lead, p.disc { color: #94a3b8; font-size: 14px; }
  p.disc { border-left: 3px solid #fbbf24; padding-left: 10px; }
  a { color: #60a5fa; }
  .clip { margin: 14px 0; }
  blockquote { margin: 0; padding: 8px 12px; background: #1e293b; border-left: 3px solid #475569; border-radius: 4px; }
  .who { font-size: 13px; color: #94a3b8; margin: 4px 0 0 12px; }
  .claim, .note { margin: 14px 0; padding: 10px 12px; background: #172033; border: 1px solid #334155; border-radius: 6px; }
  .claim p, .note p { margin: 0 0 6px; }
  .claim ul { margin: 0; padding-left: 18px; font-size: 14px; }
  table { border-collapse: collapse; width: 100%; font-size: 14px; margin: 6px 0 8px; }
  td, th { border-bottom: 1px solid #334155; padding: 4px 6px; text-align: left; }
  td:nth-child(2), td:nth-child(3) { text-align: right; font-variant-numeric: tabular-nums; }
  tr.sm td { color: #fbbf24; font-weight: 600; }
</style>
</head>
<body>
<main>
<h1>Sources: “San Mateo rushed into a bad Axon deal”</h1>
<p class="lead">__INTRO__</p>
<p class="disc">__DISCLOSURE__</p>
<p class="lead">Part of <a href="index.html">none-below.github.io/sm-alpr</a>. Section times match the video’s chapters.</p>
__BODY__
</main>
<script data-goatcounter="https://none-below.goatcounter.com/count"
        async src="https://gc.zgo.at/count.js"></script>
</body>
</html>
"""

if __name__ == "__main__":
    check()
    OUT.write_text(PAGE.replace("__INTRO__", html.escape(INTRO)).replace("__DISCLOSURE__", html.escape(DISCLOSURE))
                   .replace("__BODY__", render()))
    print(f"wrote {OUT.relative_to(ROOT)}")

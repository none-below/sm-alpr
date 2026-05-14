"""Canonical PRA topic/policy tags.

Edit this file to add or rename tags. build_pra_registry.py validates every
tag used in any metadata.json against this map and errors on unknown tags
to prevent silent typos.

Keys are tag slugs; values are human-readable labels for the UI.
"""

TAGS = {
    # Policies
    "policy-463": "Policy 463 (ALPR System)",
    "policy-463.10": "Policy 463.10 (Data Search Audits)",
    "sop-205": "SOP 205 (ALPR Operating Procedures)",
    "sop-205.5.1": "SOP 205.5.1 (Audit Procedures)",
    # Contracts
    "flock-msa": "Flock Master Services Agreement",
    "msa-5.3": "MSA §5.3 (Vendor Disclosure)",
    "pre-msa": "Pre-MSA Flock contract era (2020–2023)",
    # Programs / partners
    "ncric": "NCRIC sharing",
    "uop": "University of the Pacific access",
    "external-sharing": "External agency sharing",
    "vendor-disclosure": "Vendor-initiated disclosure",
    # Process
    "audit-compliance": "Audit compliance",
    "website-posting": "Website / transparency-page posting",
    "council-authorization": "Council authorization process",
    "pra-process": "PRA process / portal mechanics",
    # Tech adjacent
    "condor": "Condor (non-ALPR) cameras",
    "fusus": "Axon Fusus",
}

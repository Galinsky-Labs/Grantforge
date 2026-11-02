"""Prepare everything for a submission the applicant must click through
themselves (Grants.gov Workspace without S2S, foundation portals)."""
from __future__ import annotations

import json
from pathlib import Path

from ..db import ApplicantProfile, Match, SessionLocal


def _sf424_prefill(match: Match, applicant: ApplicantProfile | None) -> dict:
    """The data a Grants.gov Workspace SF-424 asks for, pre-filled from what we know."""
    d = match.draft_sections or {}
    opp = match.opportunity
    a = applicant
    return {
        "1_type_of_submission": "Application",
        "2_type_of_application": "New",
        "5a_federal_entity_identifier": "",
        "8a_legal_name": a.legal_name if a else "",
        "8b_ein": a.ein if a else "",
        "8c_uei": a.uei if a else "",
        "8d_address": a.address if a else "",
        "8f_contact_name": a.contact_name if a else "",
        "8f_contact_title": a.contact_title if a else "",
        "8f_contact_phone": a.contact_phone if a else "",
        "8f_contact_email": a.contact_email if a else "",
        "9_type_of_applicant": a.entity_type if a else "",
        "10_federal_agency": opp.funder,
        "11_assistance_listing": (opp.raw or {}).get("cfda"),
        "12_funding_opportunity_number": (opp.raw or {}).get("number"),
        "12_funding_opportunity_title": opp.title,
        "15_descriptive_title_of_project": d.get("title"),
        "16a_applicant_congressional_district": a.congressional_district if a else "",
        "18a_federal_funding_requested": d.get("requested_amount_usd"),
        "21_authorized_representative": a.contact_name if a else "",
    }


def prepare_manual(match: Match, log_fn=print) -> dict:
    with SessionLocal() as s:
        applicant = s.get(ApplicantProfile, 1)
    opp = match.opportunity
    pkg = Path(match.package_dir)
    pkg.mkdir(parents=True, exist_ok=True)
    channel = opp.submission_channel

    steps: list[str] = []
    if channel == "grants_gov":
        steps = [
            "Confirm the organisation has an active SAM.gov registration (Unique Entity ID) — required for any federal award.",
            "Log in at https://www.grants.gov with an account that holds the Authorized Organization Representative (AOR) role for this UEI.",
            f"Open the opportunity: {opp.url} and click 'Apply' to create a Workspace.",
            "Fill the SF-424 using sf424_prefill.json in this package.",
            "Upload application.docx (or paste its sections into the narrative attachment the NOFO specifies).",
            "Complete any other forms listed in CHECKLIST.md (budget form SF-424A, assurances, etc.).",
            "Run 'Check Application' in Workspace, then 'Sign and Submit'. Save the Grants.gov tracking number here.",
        ]
        (pkg / "sf424_prefill.json").write_text(json.dumps(_sf424_prefill(match, applicant), indent=2), encoding="utf-8")
    else:
        target = opp.submission_target or opp.url
        steps = [
            f"Open the application portal: {target}",
            "Create or sign in to an account for the applicant organisation.",
            "Copy each section from application.md into the matching portal field (headings match the funder's required sections where they were stated).",
            "Attach application.docx where a narrative upload is requested.",
            "Answer the items in CHECKLIST.md that are marked needs_applicant_data.",
            "Submit and save the confirmation number here.",
        ]
    instructions = "# How to submit\n\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) + "\n"
    (pkg / "SUBMIT_INSTRUCTIONS.md").write_text(instructions, encoding="utf-8")
    log_fn(f"Package prepared for manual upload at {pkg}")
    return {"status": "ready_for_manual_upload", "channel": channel, "package_dir": str(pkg), "steps": steps}

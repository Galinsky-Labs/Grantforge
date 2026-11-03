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
        "sf424a_section_b_object_classes": {k: v for k, v in ((match.budget or {}).get("totals") or {}).items()},
        "sf424a_total_direct": (match.budget or {}).get("direct"),
        "sf424a_total_indirect": (match.budget or {}).get("indirect"),
        "sf424a_total": (match.budget or {}).get("total"),
        "21_authorized_representative": a.contact_name if a else "",
    }


def prepare_manual(match: Match, log_fn=print) -> dict:
    with SessionLocal() as s:
        applicant = s.get(ApplicantProfile, 1)
    opp = match.opportunity
    pkg = Path(match.package_dir)
    pkg.mkdir(parents=True, exist_ok=True)
    channel = opp.submission_channel
    files = dict(match.package_files or {})
    from ..export import file_prefix

    prefix = file_prefix(match, "submit")
    docx_name = files.get("docx", "the application .docx file")

    steps: list[str] = []
    if channel == "grants_gov":
        steps = [
            "Confirm the organisation has an active SAM.gov registration (Unique Entity ID) — required for any federal award.",
            "Log in at https://www.grants.gov with an account that holds the Authorized Organization Representative (AOR) role for this UEI.",
            f"Open the opportunity: {opp.url} and click 'Apply' to create a Workspace.",
            f"Fill the SF-424 using {prefix}_sf424_prefill.json in this package.",
            f"Fill the SF-424A budget form from {files.get('budget_csv', 'the budget section')} (object-class totals are already summed).",
            f"Upload {docx_name} as the narrative attachment the funding notice specifies, converting to PDF if the notice requires it.",
            "Upload the files in the attachments folder where the notice asks for them.",
            f"Complete the other forms listed in {files.get('checklist', 'the checklist')}.",
            "Run 'Check Application' in Workspace, then 'Sign and Submit'. Save the Grants.gov tracking number here.",
        ]
        files["sf424"] = f"{prefix}_sf424_prefill.json"
        (pkg / files["sf424"]).write_text(json.dumps(_sf424_prefill(match, applicant), indent=2), encoding="utf-8")
    else:
        target = opp.submission_target or opp.url
        steps = [
            f"Open the application portal: {target}",
            "Create or sign in to an account for the applicant organisation.",
            f"Copy each section from {files.get('md', 'the .md file')} into the matching portal field (headings match the funder's required sections where they were stated).",
            f"Attach {docx_name} where a narrative upload is requested, and the files in the attachments folder where supporting documents are requested.",
            f"Answer the items in {files.get('checklist', 'the checklist')} that are marked needs_applicant_data.",
            "Submit and save the confirmation number here.",
        ]
    instructions = "# How to submit\n\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) + "\n"
    files["instructions"] = f"{prefix}_SUBMIT_INSTRUCTIONS.md"
    (pkg / files["instructions"]).write_text(instructions, encoding="utf-8")
    match.package_files = files
    from .. import blobstore

    blobstore.save_many([pkg / files["instructions"]] + ([pkg / files["sf424"]] if "sf424" in files else []))
    log_fn(f"Package prepared for manual upload at {pkg}")
    return {"status": "ready_for_manual_upload", "channel": channel, "package_dir": str(pkg), "steps": steps}

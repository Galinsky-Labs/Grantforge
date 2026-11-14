"""Grants.gov Applicant System-to-System (S2S) submission.

STATUS: scaffold. The transport (mutual-TLS SOAP) and the prerequisites are
implemented/documented; the per-form XML assembly is not, because every
opportunity's package names its own set of form schemas (SF-424 family,
agency-specific forms) and each must be rendered against the schema Grants.gov
publishes for that package.

What an organisation needs before this module can submit anything:

1. Active SAM.gov registration (UEI) for the applicant organisation.
2. Grants.gov organisation registration with an Authorized Organization
   Representative (AOR) account.
3. A CA-signed TLS client certificate for the submitting system, registered
   with the Grants.gov PMO through the Certificate Request Form, then
   associated with the AOR.  (Self-signed certificates are not accepted.)
4. Successful integration testing against the Grants.gov Applicant Training
   environment before production access.

Endpoints and WSDLs are listed at
https://grants.gov/system-to-system/applicant-system-to-system/ (Versions &
WSDLs).  Key operations: GetOpportunityList, GetApplicationZip/SubmitApplication,
GetApplicationStatusDetail, GetSubmissionList.

Set GF_GRANTS_GOV_S2S_CERT and GF_GRANTS_GOV_S2S_KEY (PEM paths) to enable the
transport.  Until form assembly exists, :func:`submit_s2s` sends nothing to
Grants.gov: it prepares the same complete Workspace package as the manual
channel and records why, so an authorized application is never left failed
or half-submitted.
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from ..config import settings
from ..db import Match

log = logging.getLogger(__name__)

PRODUCTION_ENDPOINT = "https://ws07.grants.gov/grantsws-applicant/services/v2/ApplicantWebServicesSoapPort"
TRAINING_ENDPOINT = "https://trainingws.grants.gov/grantsws-applicant/services/v2/ApplicantWebServicesSoapPort"


def s2s_configured() -> bool:
    return bool(settings.grants_gov_s2s_cert and settings.grants_gov_s2s_key
                and Path(settings.grants_gov_s2s_cert).exists() and Path(settings.grants_gov_s2s_key).exists())


def mtls_client(training: bool = True) -> httpx.Client:
    """An HTTPS client that presents the organisation's registered certificate."""
    return httpx.Client(
        cert=(settings.grants_gov_s2s_cert, settings.grants_gov_s2s_key),
        timeout=120,
        base_url=TRAINING_ENDPOINT if training else PRODUCTION_ENDPOINT,
        headers={"Content-Type": "text/xml; charset=utf-8"},
    )


def submit_s2s(match: Match, log_fn=print) -> dict:
    """Certificates are configured, but the per-form XML is not implemented, so nothing is sent
    over S2S. The complete Workspace package (SF-424 and SF-424A prefill, steps) is prepared instead."""
    from .manual_submitter import prepare_manual

    number = (match.opportunity.raw or {}).get("number")
    log_fn(f"S2S: certificates found, but form assembly for {number} is not implemented; "
           "preparing the Grants.gov Workspace package instead")
    result = prepare_manual(match, log_fn)
    result["s2s"] = "not_used: form assembly is not implemented; submit through Grants.gov Workspace"
    return result

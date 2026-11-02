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
transport.  :func:`submit_s2s` currently builds the application zip layout and
raises a clear error at the SOAP step so that nothing half-submitted happens.
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
    opp = match.opportunity
    number = (opp.raw or {}).get("number")
    log_fn(f"S2S: preparing submission for {number}")
    # The complete, honest state of this module: the transport is ready, the
    # form XML is not.  Fail loudly rather than pretend.
    raise NotImplementedError(
        "Grants.gov S2S form assembly is not implemented. The narrative package is complete in "
        f"{match.package_dir}; submit it through Grants.gov Workspace, or extend submit_s2s() with the "
        f"form schemas listed for opportunity {number}."
    )

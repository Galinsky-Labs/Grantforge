"""Grants.gov client against recorded response shapes (no network)."""
import httpx

from grantforge.sources.grants_gov import GrantsGovClient, GrantsGovSource, normalise_detail

SEARCH_RESPONSE = {
    "errorcode": 0,
    "msg": "Webservice Succeeds",
    "data": {
        "hitCount": 1,
        "startRecord": 0,
        "oppHits": [
            {
                "id": "357305",
                "number": "HHS-2026-ACL-TEST-0001",
                "title": "Community Technology Demonstration Grants",
                "agencyCode": "HHS-ACL",
                "agencyName": "Administration for Community Living",
                "openDate": "11/21/2026",
                "closeDate": "01/17/2027",
                "oppStatus": "posted",
                "docType": "synopsis",
                "cfdaList": ["93.213"],
            }
        ],
    },
}

FETCH_RESPONSE = {
    "errorcode": 0,
    "msg": "Webservice Succeeds",
    "data": {
        "id": 357305,
        "opportunityNumber": "HHS-2026-ACL-TEST-0001",
        "opportunityTitle": "Community Technology Demonstration Grants",
        "agencyDetails": {"agencyName": "Administration for Community Living"},
        "synopsis": {
            "synopsisDesc": "<p>Funds demonstration projects that apply <b>novel technology</b> to community needs.</p><p>Applicants must propose a pilot.</p>",
            "awardCeiling": "250000",
            "awardFloor": "50000",
            "estimatedFunding": "2000000",
            "numberOfAwards": "8",
            "costSharing": False,
            "responseDate": "01/17/2027",
            "postingDate": "11/21/2026",
            "applicantTypes": [
                {"id": "12", "description": "Nonprofits having a 501(c)(3) status with the IRS, other than institutions of higher education"},
                {"id": "23", "description": "Small businesses"},
            ],
            "applicantEligibilityDesc": "See the NOFO for cost-sharing details.",
            "agencyContactEmail": "grants@acl.example.gov",
            "agencyContactName": "Program Officer",
        },
        "synopsisAttachmentFolders": [{"synopsisAttachments": [{"fileName": "NOFO.pdf"}]}],
    },
}


def _mock_transport(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("search2"):
        return httpx.Response(200, json=SEARCH_RESPONSE)
    if request.url.path.endswith("fetchOpportunity"):
        return httpx.Response(200, json=FETCH_RESPONSE)
    return httpx.Response(404)


def make_client() -> GrantsGovClient:
    return GrantsGovClient(httpx.Client(transport=httpx.MockTransport(_mock_transport)), pause=0)


def test_normalise_detail_merges_hit_and_detail():
    opp = normalise_detail(SEARCH_RESPONSE["data"]["oppHits"][0], FETCH_RESPONSE["data"])
    assert opp.source == "grants_gov"
    assert opp.source_id == "357305"
    assert opp.title == "Community Technology Demonstration Grants"
    assert opp.funder == "Administration for Community Living"
    assert opp.close_date == "2027-01-17"
    assert opp.open_date == "2026-11-21"
    assert opp.award_ceiling == 250000 and opp.award_floor == 50000
    assert "Small businesses" in opp.applicant_types
    assert "<p>" not in opp.description and "novel technology" in opp.description
    assert opp.contact_email == "grants@acl.example.gov"
    assert opp.submission_channel == "grants_gov"
    assert opp.raw["number"] == "HHS-2026-ACL-TEST-0001"
    assert opp.raw["attachments"] == ["NOFO.pdf"]


def test_source_runs_queries_and_fetches_details():
    src = GrantsGovSource(make_client())
    profile = {"search_queries": ["community technology", "demonstration"], "keywords": []}
    logs = []
    out = src.search(profile, logs.append)
    assert len(out) == 1  # de-duplicated across the two queries
    assert out[0].title.startswith("Community Technology")
    assert any("unique opportunities" in line for line in logs)


def test_forecast_record_is_marked_forecasted():
    detail = {
        "id": 1,
        "opportunityTitle": "Forecast",
        "forecast": {"forecastDesc": "Coming soon", "estApplicationResponseDate": "03/01/2027", "applicantTypes": ["Individuals"]},
    }
    opp = normalise_detail({"id": "1"}, detail)
    assert opp.status == "forecasted"
    assert opp.close_date == "2027-03-01"
    assert opp.applicant_types == ["Individuals"]

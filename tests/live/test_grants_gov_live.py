"""Live checks against the real Grants.gov API. Run by .github/workflows/live-tests.yml.

Every raw response is saved under live-artifacts/ so the field names the code
relies on can be inspected after the run.
"""
import json
import os
from pathlib import Path

import pytest

from grantforge.sources.grants_gov import (
    GrantsGovClient, _bytes_to_text, download_attachment, fetch_nofo_text, normalise_detail,
)

pytestmark = pytest.mark.live
ART = Path(os.environ.get("GF_LIVE_ARTIFACTS", "live-artifacts"))
ART.mkdir(parents=True, exist_ok=True)


@pytest.fixture(scope="module")
def client():
    return GrantsGovClient(pause=0.3)


@pytest.fixture(scope="module")
def hits(client):
    data = client.search("energy", statuses="posted", rows=25)
    (ART / "search2_energy.json").write_text(json.dumps(data, indent=1))
    return data.get("oppHits", [])


def test_search2_returns_expected_fields(hits):
    assert hits, "search2 returned no posted 'energy' opportunities"
    for key in ("id", "number", "title", "agencyCode", "openDate", "closeDate", "oppStatus"):
        assert key in hits[0], f"search2 hit is missing '{key}'; keys are {sorted(hits[0])}"


def test_fetch_opportunity_normalises(client, hits):
    report = []
    for hit in hits[:8]:
        detail = client.fetch(hit["id"])
        (ART / f"fetchOpportunity_{hit['id']}.json").write_text(json.dumps(detail, indent=1))
        opp = normalise_detail(hit, detail)
        report.append({"id": opp.source_id, "title": opp.title, "funder": opp.funder, "close": opp.close_date,
                       "ceiling": opp.award_ceiling, "types": opp.applicant_types,
                       "attachments": len(opp.raw["attachments"]), "desc_chars": len(opp.description)})
        assert opp.title and opp.funder
        assert opp.description, f"opportunity {opp.source_id} has no description text"
    (ART / "normalised_report.json").write_text(json.dumps(report, indent=1))
    assert any(r["types"] for r in report), "no applicant types parsed from any opportunity"


def test_attachment_download_and_nofo_text(client, hits):
    tried = []
    for hit in hits[:15]:
        detail = client.fetch(hit["id"])
        opp = normalise_detail(hit, detail)
        atts = [a for a in opp.raw["attachments"] if a.get("id")]
        if not atts:
            continue
        att = atts[0]
        data = download_attachment(att["id"])
        tried.append({"opp": opp.source_id, "file": att["fileName"], "bytes": len(data), "head": data[:8].hex()})
        text = _bytes_to_text(att["fileName"], data)
        nofo = fetch_nofo_text(opp.raw, print)
        (ART / f"nofo_{opp.source_id}.txt").write_text(nofo[:200000])
        tried[-1]["single_text_chars"] = len(text)
        tried[-1]["nofo_chars"] = len(nofo)
        if nofo.strip():
            break
    (ART / "attachments_report.json").write_text(json.dumps(tried, indent=1))
    assert tried, "none of the first 15 opportunities listed attachments with ids"
    assert any(t["bytes"] > 1000 for t in tried), f"attachment downloads were empty: {tried}"
    assert any(t["nofo_chars"] > 500 for t in tried), f"no funding-notice text could be read: {tried}"

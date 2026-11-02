"""Grant sources.

Each source turns a project profile into a list of :class:`RawOpportunity`
records. :func:`upsert_opportunities` stores them, de-duplicated on
(source, source_id).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Iterable, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import Opportunity, utcnow


@dataclass
class RawOpportunity:
    source: str
    source_id: str
    title: str
    funder: str = ""
    url: str = ""
    status: str = "unknown"
    open_date: str = ""
    close_date: str = ""  # ISO yyyy-mm-dd when known
    award_floor: int | None = None
    award_ceiling: int | None = None
    applicant_types: list[str] = field(default_factory=list)
    eligibility_text: str = ""
    description: str = ""
    contact_email: str = ""
    submission_channel: str = "unknown"
    submission_target: str = ""
    raw: dict | None = None


class GrantSource(Protocol):
    name: str

    def search(self, profile: dict, log_fn=print) -> list[RawOpportunity]: ...


def upsert_opportunities(session: Session, items: Iterable[RawOpportunity]) -> list[Opportunity]:
    out: list[Opportunity] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item.source, item.source_id)
        if key in seen:
            continue
        seen.add(key)
        row = session.scalar(select(Opportunity).where(Opportunity.source == item.source, Opportunity.source_id == item.source_id))
        data = asdict(item)
        if row is None:
            row = Opportunity(**data)
            session.add(row)
        else:
            for k, v in data.items():
                # Keep an LLM-derived submission channel if the source cannot tell us.
                if k in {"submission_channel", "submission_target"} and v in {"unknown", ""} and getattr(row, k):
                    continue
                setattr(row, k, v)
            row.fetched_at = utcnow()
        out.append(row)
    session.commit()
    return out


def all_sources(settings) -> list:
    from .grants_gov import GrantsGovSource
    from .web_search import WebSearchSource

    sources: list = [GrantsGovSource()]
    if settings.enable_web_search_source and settings.llm_provider != "mock":
        sources.append(WebSearchSource())
    elif settings.enable_web_search_source:
        sources.append(WebSearchSource())  # mock provider returns canned data
    return sources

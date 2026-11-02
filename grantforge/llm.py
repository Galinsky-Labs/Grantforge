"""LLM access.

Two providers:
  * ``anthropic`` – the real thing (Claude Messages API).
  * ``mock``      – deterministic canned answers keyed by task name, used by the
                    test-suite and by ``--dry-run`` so the whole pipeline can be
                    exercised without an API key or network access.

Every call goes through :func:`complete_json` or :func:`complete_text`, with a
short ``task`` label so the mock can answer and so logs are readable.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from .config import settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def complete_text(task: str, system: str, user: str, max_tokens: int | None = None, web_search: bool = False) -> str:
    provider = _provider()
    return provider.text(task, system, user, max_tokens or settings.llm_max_tokens, web_search)


def complete_json(task: str, system: str, user: str, max_tokens: int | None = None, web_search: bool = False) -> Any:
    """Ask for JSON and parse it. Retries once with a repair prompt on bad JSON."""
    system_json = system + "\n\nRespond with a single JSON value and nothing else: no prose, no code fences."
    raw = complete_text(task, system_json, user, max_tokens, web_search)
    try:
        return parse_json(raw)
    except ValueError:
        log.warning("Bad JSON from LLM for task %s; asking for repair", task)
        repair = complete_text(
            task + ":repair",
            "You fix malformed JSON. Output only the corrected JSON value.",
            raw[:20000],
            max_tokens,
        )
        return parse_json(repair)


def parse_json(raw: str) -> Any:
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the first balanced {...} or [...] block.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("LLM response was not valid JSON")


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------

class AnthropicProvider:
    def __init__(self) -> None:
        if not settings.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set (or set GF_LLM_PROVIDER=mock for a dry run)")
        import anthropic  # local import so the mock path has no SDK dependency

        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._anthropic = anthropic

    def text(self, task: str, system: str, user: str, max_tokens: int, web_search: bool) -> str:
        kwargs: dict[str, Any] = dict(
            model=settings.llm_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if web_search:
            kwargs["tools"] = [
                {"type": "web_search_20250305", "name": "web_search", "max_uses": settings.web_search_max_uses}
            ]
        delay = 2.0
        for attempt in range(4):
            try:
                t0 = time.time()
                msg = self._client.messages.create(**kwargs)
                text = "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")
                log.info("llm[%s] %.1fs in=%s out=%s", task, time.time() - t0, msg.usage.input_tokens, msg.usage.output_tokens)
                return text
            except (self._anthropic.RateLimitError, self._anthropic.APIConnectionError, self._anthropic.InternalServerError) as exc:
                if attempt == 3:
                    raise LLMError(f"LLM call failed after retries: {exc}") from exc
                log.warning("llm[%s] transient error (%s); retrying in %.0fs", task, exc.__class__.__name__, delay)
                time.sleep(delay)
                delay *= 2
        raise LLMError("unreachable")


class MockProvider:
    """Canned answers so the pipeline can run end-to-end offline."""

    def text(self, task: str, system: str, user: str, max_tokens: int, web_search: bool) -> str:
        base = task.split(":")[0]
        handler = getattr(self, f"_{base}", None)
        if handler is None:
            return json.dumps({"mock": True, "task": task})
        return handler(user)

    # Each handler returns a JSON string matching what the real prompt asks for.
    def _summarize_document(self, user: str) -> str:
        return "Mock summary: " + user[:200].replace("\n", " ")

    def _build_profile(self, user: str) -> str:
        return json.dumps(
            {
                "project_name": "Mock Project",
                "one_line": "A mock project that demonstrates the pipeline.",
                "problem": "Grant discovery and writing takes too much of a builder's time.",
                "solution": "An automated pipeline that reads project material, finds matching grants and drafts applications.",
                "technology": ["Python", "LLM", "FastAPI"],
                "stage": "prototype",
                "domains": ["technology", "open source software", "productivity"],
                "beneficiaries": ["independent inventors", "small nonprofits"],
                "keywords": ["software", "automation", "grant"],
                "geography": "United States",
                "team": "Solo founder with a technology background.",
                "funding_need_usd": {"min": 25000, "max": 250000},
                "funding_uses": ["engineering time", "hosting", "pilot users"],
                "evidence_of_traction": ["working prototype"],
                "search_queries": ["software innovation", "open source", "small business technology", "civic technology"],
            }
        )

    def _score_opportunity(self, user: str) -> str:
        return json.dumps(
            {
                "score": 78,
                "eligible": True,
                "rationale": "Mock rationale: the opportunity funds technology prototypes and the project is a technology prototype.",
                "blockers": [],
                "submission_channel": "email",
                "submission_target": "grants@example.org",
            }
        )

    def _draft_application(self, user: str) -> str:
        return json.dumps(
            {
                "title": "Mock Application",
                "sections": [
                    {"heading": "Project Summary", "body": "This is a mock project summary paragraph."},
                    {"heading": "Statement of Need", "body": "This is a mock statement of need."},
                    {"heading": "Project Description and Approach", "body": "This is a mock approach section."},
                    {"heading": "Timeline", "body": "Month 1-3: build. Month 4-6: pilot."},
                    {"heading": "Budget and Justification", "body": "Personnel $60,000; Hosting $5,000; Total $65,000."},
                    {"heading": "Qualifications", "body": "Mock qualifications."},
                    {"heading": "Evaluation", "body": "Mock evaluation plan."},
                ],
                "checklist": [
                    {"item": "SF-424 Application for Federal Assistance", "status": "needs_applicant_data"},
                    {"item": "Budget narrative", "status": "drafted"},
                ],
                "gaps": ["Confirm the legal applicant name.", "Confirm the requested amount."],
                "cover_email": {
                    "subject": "Application: Mock Project",
                    "body": "Dear Program Officer,\n\nPlease find attached our application.\n\nSincerely,\nApplicant",
                },
            }
        )

    def _web_search_grants(self, user: str) -> str:
        return json.dumps(
            [
                {
                    "title": "Mock Foundation Technology Grant",
                    "funder": "Mock Foundation",
                    "url": "https://example.org/grants/tech",
                    "close_date": "2027-03-01",
                    "award_floor": 10000,
                    "award_ceiling": 100000,
                    "applicant_types": ["Individuals", "Small businesses", "Nonprofits"],
                    "description": "Funds early-stage technology projects with public benefit.",
                    "submission_channel": "portal",
                    "submission_target": "https://example.org/apply",
                }
            ]
        )

    def _parse_opportunity_page(self, user: str) -> str:
        return self._web_search_grants(user)[1:-1]  # single object


_provider_instance: Any = None


def _provider():
    global _provider_instance
    if _provider_instance is None:
        if settings.llm_provider == "mock":
            _provider_instance = MockProvider()
        else:
            _provider_instance = AnthropicProvider()
    return _provider_instance


def reset_provider() -> None:
    global _provider_instance
    _provider_instance = None

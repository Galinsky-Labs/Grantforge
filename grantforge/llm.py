"""LLM access, spend tracking and the monthly spend cap.

Two providers:
  * ``anthropic`` – the Claude Messages API.
  * ``mock``      – deterministic canned answers keyed by task name, used by the
                    test-suite and by dry runs so the whole pipeline can be
                    exercised without an API key or network access.

Every call goes through :func:`complete_json` or :func:`complete_text` with a
short ``task`` label. Every call is recorded in the ``llm_usage`` table with
its token counts and cost, and every call is refused once the month's spend
reaches ``GF_MONTHLY_BUDGET_USD``.

Prices are per million tokens (MTok), verified on 2026-11-01 against
https://platform.claude.com/docs/en/about-claude/pricing . Web search costs
$10 per 1,000 searches (platform.claude.com web-search-tool docs).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .config import settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMTruncated(LLMError):
    """The model stopped because it hit max_tokens; the output is incomplete."""


class BudgetExceeded(LLMError):
    pass


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------

# model id -> (input, output, 5-minute cache write, cache read) in USD per MTok
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.10),
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-fable-5-1": (10.00, 50.00, 12.50, 0.25),
    "claude-mythos-5-1": (10.00, 50.00, 12.50, 0.25),
}
# Haiku 5.5 has two tiers split at a 100,000-token prompt.
HAIKU_55_SMALL = (0.10, 0.50, 0.125, 0.01)
HAIKU_55_LARGE = (0.50, 2.50, 0.625, 0.05)
WEB_SEARCH_USD = 0.01  # $10 per 1,000 searches


def price_for(model: str, prompt_tokens: int) -> tuple[float, float, float, float]:
    if settings.price_input_per_mtok and settings.price_output_per_mtok:
        i, o = float(settings.price_input_per_mtok), float(settings.price_output_per_mtok)
        return (i, o, i * 1.25, i * 0.1)
    if model == "claude-haiku-5-5":
        return HAIKU_55_SMALL if prompt_tokens <= 100_000 else HAIKU_55_LARGE
    if model in PRICES:
        return PRICES[model]
    # Unknown model and no override: count it at the highest listed rate so the cap still protects you.
    return PRICES["claude-fable-5-1"]


def cost_usd(model: str, usage: "Usage") -> float:
    prompt = usage.input_tokens + usage.cache_write_tokens + usage.cache_read_tokens
    p_in, p_out, p_write, p_read = price_for(model, prompt)
    return round(
        (usage.input_tokens * p_in + usage.output_tokens * p_out + usage.cache_write_tokens * p_write
         + usage.cache_read_tokens * p_read) / 1_000_000 + usage.web_searches * WEB_SEARCH_USD,
        6,
    )


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    web_searches: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.web_searches += other.web_searches


@dataclass
class LLMResult:
    text: str
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)


def month_spend_usd() -> float:
    from sqlalchemy import func, select

    from .db import LLMUsage, SessionLocal

    start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with SessionLocal() as s:
        return float(s.scalar(select(func.coalesce(func.sum(LLMUsage.cost_usd), 0.0)).where(LLMUsage.created_at >= start)) or 0.0)


def _record(task: str, model: str, usage: Usage) -> None:
    from .db import LLMUsage, SessionLocal

    try:
        with SessionLocal() as s:
            s.add(LLMUsage(task=task, model=model, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                           cache_write_tokens=usage.cache_write_tokens, cache_read_tokens=usage.cache_read_tokens,
                           web_searches=usage.web_searches, cost_usd=cost_usd(model, usage)))
            s.commit()
    except Exception:  # spend tracking must never break the pipeline
        log.exception("could not record LLM usage")


def check_budget() -> None:
    if settings.monthly_budget_usd and settings.monthly_budget_usd > 0:
        spent = month_spend_usd()
        if spent >= settings.monthly_budget_usd:
            raise BudgetExceeded(
                f"Monthly model spend cap reached (${spent:.2f} of ${settings.monthly_budget_usd:.2f}). "
                "Raise GF_MONTHLY_BUDGET_USD or wait for the next month."
            )


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def complete_text(task: str, system: str, user: str, max_tokens: int | None = None, web_search: bool = False,
                  cached_context: str | None = None) -> str:
    """Return the model's text. Raises LLMTruncated if the output hit max_tokens.

    ``cached_context`` is a large block shared by several calls (source material,
    funding notice). It is sent first with a cache breakpoint so repeated calls
    pay the cache-read price for it.
    """
    check_budget()
    result = _provider().text(task, system, user, max_tokens or settings.llm_max_tokens, web_search, cached_context)
    _record(task, settings.llm_model if settings.llm_provider != "mock" else "mock", result.usage)
    if result.stop_reason == "max_tokens":
        raise LLMTruncated(f"Model output for '{task}' was cut off at {max_tokens or settings.llm_max_tokens} tokens")
    return result.text


def complete_json(task: str, system: str, user: str, max_tokens: int | None = None, web_search: bool = False,
                  cached_context: str | None = None) -> Any:
    """Ask for JSON and parse it. Retries once with a repair prompt on malformed JSON."""
    system_json = system + "\n\nRespond with a single JSON value and nothing else: no prose, no code fences."
    raw = complete_text(task, system_json, user, max_tokens, web_search, cached_context)
    try:
        return parse_json(raw)
    except ValueError:
        log.warning("Bad JSON from LLM for task %s; asking for repair", task)
        repair = complete_text(task + ":repair", "You fix malformed JSON. Output only the corrected JSON value.",
                               raw[:20000], max_tokens)
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
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start: end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("LLM response was not valid JSON")


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------

class AnthropicProvider:
    MAX_CONTINUATIONS = 6  # pause_turn continuations for long web-search turns

    def __init__(self) -> None:
        if not settings.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set (or set GF_LLM_PROVIDER=mock for a dry run)")
        import anthropic

        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=0, timeout=600)
        self._anthropic = anthropic

    def text(self, task: str, system: str, user: str, max_tokens: int, web_search: bool,
             cached_context: str | None) -> LLMResult:
        content: list[dict] = []
        if cached_context:
            content.append({"type": "text", "text": cached_context, "cache_control": {"type": "ephemeral"}})
        content.append({"type": "text", "text": user})
        messages: list[dict] = [{"role": "user", "content": content}]
        kwargs: dict[str, Any] = dict(model=settings.llm_model, max_tokens=max_tokens, system=system)
        if web_search:
            kwargs["tools"] = [{"type": settings.web_search_tool_version, "name": "web_search",
                                "max_uses": settings.web_search_max_uses}]
        total = Usage()
        texts: list[str] = []
        for _ in range(self.MAX_CONTINUATIONS + 1):
            msg = self._create(task, messages=messages, **kwargs)
            u = msg.usage
            total.add(Usage(
                input_tokens=u.input_tokens or 0,
                output_tokens=u.output_tokens or 0,
                cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
                cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                web_searches=(getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0),
            ))
            texts.extend(b.text for b in msg.content if getattr(b, "type", "") == "text")
            if msg.stop_reason == "pause_turn":
                # Long server-tool turn: send the paused assistant content back unchanged to continue.
                messages = messages + [{"role": "assistant", "content": msg.content}]
                continue
            return LLMResult("".join(texts), msg.stop_reason or "end_turn", total)
        return LLMResult("".join(texts), "pause_turn", total)

    def _create(self, task: str, **kwargs):
        delay = 2.0
        retryable = (self._anthropic.RateLimitError, self._anthropic.APIConnectionError,
                     self._anthropic.InternalServerError, self._anthropic.APITimeoutError)
        for attempt in range(5):
            try:
                t0 = time.time()
                msg = self._client.messages.create(**kwargs)
                log.info("llm[%s] %.1fs in=%s out=%s stop=%s", task, time.time() - t0,
                         msg.usage.input_tokens, msg.usage.output_tokens, msg.stop_reason)
                return msg
            except retryable as exc:
                if attempt == 4:
                    raise LLMError(f"LLM call failed after retries: {exc}") from exc
                log.warning("llm[%s] transient error (%s); retrying in %.0fs", task, exc.__class__.__name__, delay)
                time.sleep(delay)
                delay *= 2
        raise LLMError("unreachable")


class MockProvider:
    """Canned answers so the pipeline can run end-to-end offline.

    Tests may put callables in ``MockProvider.overrides[task_base]`` to change
    an answer: ``fn(user_text) -> str``.
    """

    overrides: dict[str, Callable[[str], str]] = {}

    def text(self, task: str, system: str, user: str, max_tokens: int, web_search: bool,
             cached_context: str | None) -> LLMResult:
        base = task.split(":")[0]
        handler = self.overrides.get(base) or getattr(self, f"_{base}", None)
        out = handler(user) if handler else json.dumps({"mock": True, "task": task})
        return LLMResult(out, "end_turn", Usage(input_tokens=len(user) // 4, output_tokens=len(out) // 4))

    def _summarize_document(self, user: str) -> str:
        return "Mock summary: " + user[:200].replace("\n", " ")

    def _build_profile(self, user: str) -> str:
        return json.dumps({
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
        })

    def _score_opportunity(self, user: str) -> str:
        return json.dumps({
            "score": 78, "eligible": True,
            "rationale": "Mock rationale: the opportunity funds technology prototypes and the project is a technology prototype.",
            "blockers": [], "submission_channel": "email", "submission_target": "grants@example.org",
        })

    def _extract_requirements(self, user: str) -> str:
        return json.dumps({
            "sections": [
                {"heading": "Project Summary", "limit": {"unit": "words", "value": 250}, "guidance": "Summarise the project."},
                {"heading": "Project Narrative", "limit": {"unit": "pages", "value": 5}, "guidance": "Describe need and approach."},
                {"heading": "Budget Justification", "limit": None, "guidance": "Justify each cost."},
            ],
            "review_criteria": ["Technical merit", "Feasibility"],
            "required_forms": ["SF-424", "SF-424A"],
            "required_attachments": ["Resume of the principal investigator"],
            "formatting": "12-point font, 1-inch margins.",
            "cost_sharing": "None required.",
            "submission_notes": "",
        })

    def _plan_application(self, user: str) -> str:
        return json.dumps({
            "title": "Mock Application",
            "requested_amount_usd": 65000,
            "sections": [
                {"heading": "Project Summary", "brief": "Summarise.", "limit": {"unit": "words", "value": 250}},
                {"heading": "Project Narrative", "brief": "Need and approach.", "limit": {"unit": "pages", "value": 5}},
                {"heading": "Budget Justification", "brief": "Justify costs.", "limit": None},
            ],
            "budget": {
                "personnel": [{"item": "Lead engineer, 6 months at 50% effort", "amount": 60000}],
                "fringe": [], "travel": [], "equipment": [],
                "supplies": [{"item": "Cloud hosting", "amount": 5000}],
                "contractual": [], "construction": [], "other": [], "indirect": [],
            },
            "checklist": [
                {"item": "SF-424 Application for Federal Assistance", "status": "needs_applicant_data", "note": ""},
                {"item": "Budget narrative", "status": "drafted", "note": ""},
            ],
            "gaps": ["Confirm the requested amount."],
            "cover_email": {"subject": "Application: Mock Project",
                            "body": "Dear Program Officer,\n\nPlease find attached our application.\n\nSincerely,\nApplicant"},
        })

    def _draft_section(self, user: str) -> str:
        heading = re.search(r"SECTION TO WRITE: (.+)", user)
        name = heading.group(1).strip() if heading else "Section"
        return f"This is the mock {name.lower()} section. It states facts from the material."

    def _rewrite_section(self, user: str) -> str:
        return "A shorter mock section written again from the beginning."

    def _fact_check_section(self, user: str) -> str:
        return json.dumps([
            {"claim": "It states facts from the material.", "status": "supported", "evidence": "We built a thing."},
        ])

    def _web_search_grants(self, user: str) -> str:
        return json.dumps([{
            "title": "Mock Foundation Technology Grant", "funder": "Mock Foundation",
            "url": "https://example.org/grants/tech", "close_date": "2027-03-01",
            "award_floor": 10000, "award_ceiling": 100000,
            "applicant_types": ["Individuals", "Small businesses", "Nonprofits"],
            "description": "Funds early-stage technology projects with public benefit.",
            "submission_channel": "portal", "submission_target": "https://example.org/apply",
        }])

    def _parse_opportunity_page(self, user: str) -> str:
        return json.dumps({
            "title": "Mock Foundation Technology Grant", "funder": "Mock Foundation", "close_date": "2027-03-01",
            "open_date": "unknown", "award_floor": 10000, "award_ceiling": 100000,
            "applicant_types": ["Individuals"], "eligibility_text": "", "description": "Funds early-stage technology.",
            "contact_email": "", "submission_channel": "portal", "submission_target": "https://example.org/apply",
            "is_open": True,
        })


_provider_instance: Any = None


def _provider():
    global _provider_instance
    if _provider_instance is None:
        _provider_instance = MockProvider() if settings.llm_provider == "mock" else AnthropicProvider()
    return _provider_instance


def reset_provider() -> None:
    global _provider_instance
    _provider_instance = None

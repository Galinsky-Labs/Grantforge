"""Model request shape, refusals, the idle worker, hosted-mode timing, login limits, S2S fallback and setup status."""
import importlib
import types
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from grantforge import llm, maintenance, worker
from grantforge import main as webmain
from grantforge.config import settings
from grantforge.db import ApplicantProfile, AuditEvent, Job, LLMUsage, Match, Opportunity, Project, utcnow
from grantforge.security import LoginLimiter
from grantforge.setup_status import blocking, setup_checks


# --------------------------------------------------------------------------
# Claude request shape (a fake client stands in for the network)
# --------------------------------------------------------------------------

def _message(text="ok", stop_reason="end_turn", category=None, blocks=None):
    usage = types.SimpleNamespace(input_tokens=100, output_tokens=20, cache_creation_input_tokens=0,
                                  cache_read_input_tokens=0, server_tool_use=None)
    content = blocks if blocks is not None else [types.SimpleNamespace(type="thinking", thinking=""),
                                                 types.SimpleNamespace(type="text", text=text)]
    details = types.SimpleNamespace(category=category) if stop_reason == "refusal" else None
    return types.SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage, stop_details=details)


class FakeClient:
    """Records every request. ``script`` is a list of messages or exceptions returned in order."""

    def __init__(self, script=None):
        self.calls: list[tuple[str, dict]] = []
        self.script = list(script or [])
        self.messages = types.SimpleNamespace(create=lambda **kw: self._create("messages", kw))
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=lambda **kw: self._create("beta", kw)))

    def _create(self, route, kwargs):
        self.calls.append((route, kwargs))
        item = self.script.pop(0) if self.script else _message()
        if isinstance(item, Exception):
            raise item
        return item


def _bad_request(text):
    import anthropic
    import httpx2

    response = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    return anthropic.BadRequestError(text, response=response, body=None)


@pytest.fixture()
def provider(session, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "llm_model", "claude-sonnet-5-5")
    monkeypatch.setattr(settings, "llm_effort", "medium")
    monkeypatch.setattr(settings, "llm_thinking_headroom", 8000)
    client = FakeClient()
    p = llm.AnthropicProvider(client=client)
    monkeypatch.setattr(llm, "_provider_instance", p)
    yield p
    llm.reset_provider()


def test_request_leaves_room_for_thinking_and_sets_effort(provider):
    assert llm.complete_text("draft_section", "sys", "user", max_tokens=6000) == "ok"
    route, kw = provider._client.calls[0]
    assert kw["max_tokens"] == 14000  # 6000 for the section + 8000 thinking headroom
    assert kw["output_config"] == {"effort": "medium"}
    assert "thinking" not in kw and "temperature" not in kw  # adaptive thinking is the default; sampling params are rejected
    # Sonnet 5.5 can decline a request, so server-side fallback is requested.
    assert route == "beta" and kw["fallbacks"] == "default" and kw["betas"] == [llm.FALLBACK_BETA]


def test_light_tasks_and_repairs_run_at_low_effort(provider):
    llm.complete_text("score_opportunity", "s", "u", max_tokens=1200)
    llm.complete_text("plan_application:repair", "s", "u")
    assert [kw["output_config"]["effort"] for _r, kw in provider._client.calls] == ["low", "low"]


def test_cached_context_calls_share_one_cache_entry(provider):
    """The cache key covers the system prompt, so every call that shares the material sends the same one."""
    llm.complete_text("plan_application", "PLAN RULES", "Plan it.", cached_context="MATERIAL")
    llm.complete_text("draft_section", "SECTION RULES", "Write it.", cached_context="MATERIAL")
    llm.complete_text("fact_check_section", "CHECK RULES", "Check it.", cached_context="MATERIAL")
    prefixes = set()
    for _route, kw in provider._client.calls:
        first, second = kw["messages"][0]["content"]
        assert first == {"type": "text", "text": "MATERIAL", "cache_control": {"type": "ephemeral"}}
        assert "RULES" in second["text"]  # the task's own instructions follow the cached block
        prefixes.add((kw["system"], kw["output_config"]["effort"], first["text"]))
    assert len(prefixes) == 1


def test_refusal_raises_and_is_recorded(provider, session):
    provider._client.script = [_message(text="", stop_reason="refusal", category="general_harms", blocks=[])]
    with pytest.raises(llm.LLMRefused, match="general_harms"):
        llm.complete_json("plan_application", "s", "u")
    assert len(provider._client.calls) == 1  # no "repair" call on an empty refusal
    assert session.query(LLMUsage).filter_by(task="plan_application").count() == 1


def test_truncated_output_is_reported_not_returned(provider):
    provider._client.script = [_message(text="half a sect", stop_reason="max_tokens")]
    with pytest.raises(llm.LLMTruncated):
        llm.complete_text("draft_section", "s", "u", max_tokens=100)


def test_rejected_optional_parameters_are_dropped_once(provider):
    provider._client.script = [_bad_request("fallbacks: not supported for this account"),
                               _bad_request("output_config.effort is not supported on this model")]
    assert llm.complete_text("draft_section", "s", "u") == "ok"
    routes = [r for r, _kw in provider._client.calls]
    assert routes == ["beta", "messages", "messages"]
    assert "output_config" not in provider._client.calls[-1][1] and "fallbacks" not in provider._client.calls[-1][1]
    llm.complete_text("draft_section", "s", "u")  # later calls do not try the rejected parameters again
    assert len(provider._client.calls) == 4 and "output_config" not in provider._client.calls[-1][1]


def test_other_bad_requests_fail_with_the_reason(provider):
    provider._client.script = [_bad_request("prompt is too long: 1200000 tokens")]
    with pytest.raises(llm.LLMError, match="prompt is too long"):
        llm.complete_text("draft_section", "s", "u")


def test_pause_turn_is_continued_with_the_paused_content(provider):
    paused = _message(text="[", stop_reason="pause_turn")
    provider._client.script = [paused, _message(text='{"a": 1}]')]
    assert llm.complete_json("web_search_grants", "s", "u", web_search=True) == [{"a": 1}]
    first, second = (kw for _r, kw in provider._client.calls)
    assert first["tools"][0]["type"] == settings.web_search_tool_version
    assert second["messages"][-1] == {"role": "assistant", "content": paused.content}


def test_new_model_prices():
    u = llm.Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000)
    assert llm.cost_usd("claude-fable-5-2", u) == pytest.approx(8.00 + 40.00 + 0.20)
    assert llm.cost_usd("claude-opus-5-5", u) == pytest.approx(4.00 + 20.00 + 0.20)


def test_broken_fact_check_answer_does_not_lose_the_draft(session):
    from grantforge.drafting import draft_match

    p = Project(name="P", profile={"project_name": "P"})
    o = Opportunity(source="manual", source_id="fc", title="Opp", requirements={"sections": []})
    session.add_all([p, o])
    session.flush()
    m = Match(project_id=p.id, opportunity_id=o.id, state="shortlisted", score=80)
    session.add(m)
    session.commit()
    llm.MockProvider.overrides["fact_check_section"] = lambda _u: "not json at all"
    draft_match(session, m, lambda _line: None)
    assert m.state == "drafted" and m.draft_markdown
    assert any("could not run" in f["claim"] for f in m.fact_check)


# --------------------------------------------------------------------------
# Worker: idle without touching the database, woken by enqueue
# --------------------------------------------------------------------------

def test_idle_worker_does_not_query_the_database(session, monkeypatch):
    """Neon suspends its compute only when nothing queries it, so an idle worker must stay quiet."""
    from sqlalchemy import event

    from grantforge.db import engine

    monkeypatch.setattr(settings, "run_worker", True)
    monkeypatch.setattr(settings, "worker_poll_seconds", 3600.0)
    monkeypatch.setattr(settings, "autopilot_interval_hours", 0)
    monkeypatch.setattr(worker, "IDLE_TICK_SECONDS", 0.05)
    monkeypatch.setattr(maintenance, "_ran_on", "")
    ran: list[int] = []
    monkeypatch.setattr(worker, "run_job", ran.append)
    worker.start()
    try:
        _wait(lambda: maintenance._ran_on != "")  # start-up: recover jobs, first poll, daily maintenance
        import time

        time.sleep(0.2)  # let the loop reach its idle wait
        statements: list[str] = []
        listener = lambda _c, _cur, statement, *_a: statements.append(statement)  # noqa: E731
        event.listen(engine, "before_cursor_execute", listener)
        try:
            time.sleep(0.5)  # ten idle ticks
            assert statements == []
        finally:
            event.remove(engine, "before_cursor_execute", listener)
        job = worker.enqueue(session, "profile", None)  # wakes the worker at once, long before the hourly poll
        _wait(lambda: ran == [job.id])
    finally:
        worker.stop()
        worker._thread.join(timeout=5)


def _wait(condition, seconds=5.0):
    import time

    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_daily_maintenance_runs_once_per_day_even_after_a_restart(session, monkeypatch):
    monkeypatch.setattr(maintenance, "_ran_on", "")
    assert maintenance.due_today() is True
    assert maintenance.due_today() is False
    # A redeploy erases local disk and process memory; the day is remembered in the database.
    monkeypatch.setattr(maintenance, "_ran_on", "")
    assert maintenance.due_today() is False
    assert not list(settings.data_dir.glob(".last_maintenance*"))


def test_autopilot_interval_survives_a_restart(session):
    p = Project(name="P", profile={"project_name": "P"})
    session.add_all([p, Project(name="No profile yet")])
    session.commit()
    assert worker._last_autopilot() == 0.0
    assert worker.run_autopilot() == 1
    jobs = session.query(Job).all()
    assert [(j.kind, j.project_id, j.payload) for j in jobs] == [("pipeline", p.id, {"autopilot": True})]
    assert abs(worker._last_autopilot() - utcnow().timestamp()) < 30
    old = session.query(AuditEvent).filter_by(event=worker.AUTOPILOT_EVENT).one()
    old.created_at = utcnow() - timedelta(hours=30)
    session.commit()
    assert utcnow().timestamp() - worker._last_autopilot() > 29 * 3600


def test_worker_poll_default_depends_on_the_database(monkeypatch):
    from grantforge.config import Settings

    monkeypatch.delenv("GF_WORKER_POLL_SECONDS", raising=False)
    monkeypatch.setenv("GF_DATABASE_URL", "postgresql://u:p@ep-x-pooler.us-east-2.aws.neon.tech/db")
    assert Settings().worker_poll_seconds == 3600.0
    monkeypatch.setenv("GF_DATABASE_URL", "")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert Settings().worker_poll_seconds == 2.0
    monkeypatch.setenv("GF_WORKER_POLL_SECONDS", "45")
    assert Settings().worker_poll_seconds == 45.0


def test_render_external_url_is_the_default_base_url(monkeypatch):
    from grantforge.config import Settings

    monkeypatch.delenv("GF_BASE_URL", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://grantforge-abcd.onrender.com")
    assert Settings().base_url == "https://grantforge-abcd.onrender.com"
    monkeypatch.setenv("GF_BASE_URL", "https://grants.example.com/")
    assert Settings().base_url == "https://grants.example.com"


# --------------------------------------------------------------------------
# Login limits
# --------------------------------------------------------------------------

def test_rotating_addresses_does_not_get_around_the_login_limit():
    limiter = LoginLimiter(max_failures=5, max_total_failures=12)
    for i in range(12):
        ip = f"198.51.100.{i}"
        assert not limiter.blocked(ip)
        limiter.fail(ip)
    assert limiter.blocked("203.0.113.9")  # an address that never failed is blocked too
    limiter.reset("203.0.113.9")
    assert limiter.blocked("203.0.113.9")  # a successful login elsewhere does not clear the global count


def test_client_address_behind_render_and_caddy(monkeypatch):
    from grantforge.security import client_ip

    def request(headers):
        return types.SimpleNamespace(headers=headers, client=types.SimpleNamespace(host="10.0.0.7"))

    forged = {"x-forwarded-for": "1.2.3.4, 198.51.100.20", "cf-connecting-ip": "203.0.113.5"}
    monkeypatch.setattr(settings, "trust_proxy", False)
    assert client_ip(request(forged)) == "10.0.0.7"  # headers are ignored unless a proxy is declared
    monkeypatch.setattr(settings, "trust_proxy", True)
    monkeypatch.delenv("RENDER", raising=False)
    assert client_ip(request(forged)) == "198.51.100.20"  # Caddy: the last hop it recorded
    monkeypatch.setenv("RENDER", "true")
    assert client_ip(request(forged)) == "203.0.113.5"  # Render: the address Cloudflare saw


# --------------------------------------------------------------------------
# S2S certificates present: still nothing half-submitted, and nothing failed
# --------------------------------------------------------------------------

def test_s2s_certificates_fall_back_to_the_workspace_package(session, tmp_path, monkeypatch):
    from grantforge.submit import submit_match

    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x")
    key.write_text("x")
    monkeypatch.setattr(settings, "grants_gov_s2s_cert", str(cert))
    monkeypatch.setattr(settings, "grants_gov_s2s_key", str(key))
    p = Project(name="P")
    o = Opportunity(source="grants_gov", source_id="77", title="Federal Opp", submission_channel="grants_gov",
                    url="https://www.grants.gov/search-results-detail/77", raw={"number": "ABC-77"})
    session.add_all([p, o])
    session.flush()
    pkg = settings.data_dir / "packages" / "s2s_case"
    m = Match(project_id=p.id, opportunity_id=o.id, state="authorized", authorized_at=utcnow(), version=1,
              draft_sections={"title": "T"}, package_dir=str(pkg), package_files={"docx": "a.docx"})
    session.add(m)
    session.commit()
    result = submit_match(session, m, lambda _line: None)
    assert result["status"] == "ready_for_manual_upload" and "not_used" in result["s2s"]
    assert m.state == "ready_for_manual_upload"
    assert (pkg / m.package_files["sf424"]).exists() and (pkg / m.package_files["instructions"]).exists()


# --------------------------------------------------------------------------
# Setup status
# --------------------------------------------------------------------------

def _levels(checks):
    return {c["name"]: c["level"] for c in checks}


def test_setup_status_names_what_is_missing_on_a_hosted_server(session, monkeypatch):
    monkeypatch.setattr(settings, "base_url", "https://grantforge.onrender.com")
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "email_backend", "smtp")
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "smtp_password", "")
    monkeypatch.setattr(settings, "ui_password", "pw")
    monkeypatch.setattr(settings, "submission_test_mode", True)
    checks = setup_checks(session.get(ApplicantProfile, 1))
    levels = _levels(checks)
    assert levels["Model"] == "missing" and levels["Sending email"] == "missing" and levels["Applicant"] == "missing"
    assert levels["Password"] == "ok" and levels["Email submission"] == "warn"
    email = next(c for c in checks if c["name"] == "Sending email")
    assert "GF_SMTP_USER" in email["fix"] and "GF_SMTP_PASSWORD" in email["fix"] and "GF_SMTP_HOST" not in email["fix"]
    assert {c["name"] for c in blocking(checks)} == {"Model", "Sending email", "Applicant"}

    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test-value")
    monkeypatch.setattr(settings, "smtp_user", "me@example.com")
    monkeypatch.setattr(settings, "smtp_password", "app-password")
    session.get(ApplicantProfile, 1).legal_name = "Org LLC"
    checks = setup_checks(session.get(ApplicantProfile, 1))
    assert blocking(checks) == []
    assert "sk-ant-test-value" not in str(checks) and "app-password" not in str(checks)  # never shows a secret


def test_console_email_on_a_public_address_counts_as_missing(session, monkeypatch):
    monkeypatch.setattr(settings, "email_backend", "console")
    assert _levels(setup_checks())["Sending email"] == "warn"  # localhost: fine for a dry run
    monkeypatch.setattr(settings, "base_url", "https://grants.example.com")
    monkeypatch.setattr(settings, "ui_password", "pw")
    assert _levels(setup_checks())["Sending email"] == "missing"


def test_settings_page_shows_setup_status_and_sends_a_test_email(session):
    with TestClient(webmain.app, base_url="http://localhost") as client:
        page = client.get("/settings")
        assert "Setup status" in page.text and "Dry-run mode" in page.text
        home = client.get("/")
        assert "Setup is not finished" in home.text and "Applicant" in home.text
        token = client.cookies.get("gf_csrf")
        before = set((settings.data_dir / "outbox").glob("*.eml"))
        r = client.post("/settings/test-email", data={"csrf": token}, follow_redirects=True)
        assert "wrote the test email to the server" in r.text  # console backend says so plainly
        new = set((settings.data_dir / "outbox").glob("*.eml")) - before
        assert len(new) == 1 and b"Test email" in next(iter(new)).read_bytes()
        assert client.post("/settings/test-email").status_code == 403  # CSRF applies


def test_test_email_failure_is_reported_not_raised(session, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr(webmain, "send_email", boom)
    with TestClient(webmain.app, base_url="http://localhost") as client:
        client.get("/")
        r = client.post("/settings/test-email", data={"csrf": client.cookies.get("gf_csrf")}, follow_redirects=True)
        assert r.status_code == 200 and "could not be sent" in r.text


def test_render_blueprint_matches_the_settings_the_app_reads():
    """Every variable named in render.yaml must be one the app actually reads."""
    import re

    root = Path(__file__).resolve().parent.parent
    keys = set(re.findall(r"- key: (\w+)", (root / "render.yaml").read_text()))
    source = (root / "grantforge" / "config.py").read_text()
    unknown = {k for k in keys if f'"{k}"' not in source}
    assert unknown == set(), f"render.yaml sets variables the app does not read: {unknown}"
    assert {"GF_DATABASE_URL", "GF_DATABASE_URL_DIRECT", "ANTHROPIC_API_KEY", "GF_UI_PASSWORD", "GF_NOTIFY_TO"} <= keys
    assert importlib.import_module("grantforge.config")  # config still imports cleanly

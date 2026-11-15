"""What is configured and what is still missing, shown on the Settings page and the dashboard.

Every setting comes from the environment (see config.py). This module only reads them and says, in
plain words, what each missing one stops from working and which variable fixes it. It never returns
a secret's value.
"""
from __future__ import annotations

import os

from .config import settings
from .notify import smtp_missing
from .security import is_local_only

OK, WARN, MISSING = "ok", "warn", "missing"


def _item(level: str, name: str, text: str, fix: str = "") -> dict:
    return {"level": level, "name": name, "text": text, "fix": fix}


def setup_checks(applicant=None) -> list[dict]:
    """Ordered list of {level: ok|warn|missing, name, text, fix}. 'missing' means a feature cannot work."""
    out: list[dict] = []
    hosted = not is_local_only()

    # --- model -------------------------------------------------------------
    if settings.llm_provider == "mock":
        out.append(_item(WARN, "Model", "Dry-run mode: the mock provider returns canned answers, not real drafts.",
                         "Set GF_LLM_PROVIDER=anthropic and ANTHROPIC_API_KEY."))
    elif not settings.anthropic_api_key:
        out.append(_item(MISSING, "Model", "No API key: profiling, searching, scoring and drafting cannot run.",
                         "Set ANTHROPIC_API_KEY."))
    else:
        out.append(_item(OK, "Model", f"{settings.llm_model}, effort {settings.llm_effort or 'API default'}."))

    # --- email -------------------------------------------------------------
    if not settings.notify_to:
        out.append(_item(MISSING, "Your email address", "No address to send review links and reminders to.",
                         "Set GF_NOTIFY_TO."))
    else:
        out.append(_item(OK, "Your email address", settings.notify_to))
    free = settings.hosting_plan == "free"
    resend_fix = ("Set GF_EMAIL_BACKEND=resend, RESEND_API_KEY and GF_EMAIL_FROM (an address on a domain verified "
                  "in Resend); Resend sends over HTTPS.")
    if settings.email_backend == "smtp":
        lacking = smtp_missing()
        if free:
            out.append(_item(MISSING, "Sending email",
                             "Email is unavailable: the Free hosting plan blocks outbound SMTP connections, so no "
                             "review link, reminder or emailed application can be sent. Drafts still appear on the "
                             "dashboard.",
                             resend_fix + " Or move the service to a paid plan and set the GF_SMTP_* settings."))
        elif lacking:
            out.append(_item(MISSING, "Sending email", "SMTP is selected but not fully configured, so no email can be sent.",
                             "Set " + ", ".join(lacking) + "."))
        else:
            out.append(_item(OK, "Sending email", f"SMTP through {settings.smtp_host}:{settings.smtp_port} as "
                                                  f"{settings.email_from or settings.smtp_user}."))
    elif settings.email_backend == "resend":
        lacking = [n for n, v in (("RESEND_API_KEY", settings.resend_api_key), ("GF_EMAIL_FROM", settings.email_from)) if not v]
        if lacking:
            out.append(_item(MISSING, "Sending email", "Resend is selected but not fully configured, so no email can be sent. "
                             "Drafts still appear on the dashboard.", "Set " + ", ".join(lacking) + "."))
        else:
            out.append(_item(OK, "Sending email", f"Resend (HTTPS API) as {settings.email_from}. The sender's domain "
                                                  "must be verified in Resend."))
    elif settings.email_backend != "console":
        out.append(_item(MISSING, "Sending email", f"Unknown email backend '{settings.email_backend}'.",
                         "Set GF_EMAIL_BACKEND to smtp, resend or console."))
    elif hosted:
        out.append(_item(MISSING, "Sending email",
                         "The console backend only writes emails to the server's disk; nothing reaches your inbox.",
                         resend_fix if free else "Set GF_EMAIL_BACKEND=smtp and the GF_SMTP_* settings, or "
                                                 "GF_EMAIL_BACKEND=resend and RESEND_API_KEY."))
    else:
        out.append(_item(WARN, "Sending email", f"Console backend: emails are written to {settings.data_dir / 'outbox'}.",
                         "Set GF_EMAIL_BACKEND=smtp (or resend) to send real email."))

    # --- hosting plan ------------------------------------------------------
    if free:
        keep = (f"While a job runs, GrantForge requests its own address every {settings.keepalive_seconds} s to stay awake. "
                if settings.keepalive_seconds > 0 else "")
        out.append(_item(WARN, "Hosting plan",
                         "Free plan: the server sleeps after 15 minutes without a visitor and wakes (in about a minute) "
                         "on the next visit. " + keep + "A run that is interrupted resumes from its last finished step "
                         "when the server wakes. Autopilot runs and deadline reminders only happen while it is awake.",
                         "A paid plan (Render Starter) keeps the server running and allows SMTP."))

    # --- access ------------------------------------------------------------
    if settings.ui_password:
        out.append(_item(OK, "Password", "Sign-in and Authorize both require the GrantForge password."))
    elif hosted:
        out.append(_item(MISSING, "Password", "A public address without a password is refused.", "Set GF_UI_PASSWORD."))
    else:
        out.append(_item(WARN, "Password", "No password: anyone who can reach this address can use GrantForge.",
                         "Set GF_UI_PASSWORD."))

    # --- storage -----------------------------------------------------------
    if settings.database_url.startswith("sqlite"):
        if os.environ.get("RENDER"):
            out.append(_item(MISSING, "Database", "SQLite on Render is erased on every deploy and restart.",
                             "Set GF_DATABASE_URL (and GF_DATABASE_URL_DIRECT) to a Neon Postgres database."))
        else:
            out.append(_item(OK, "Database", f"SQLite in {settings.data_dir}, backed up daily to {settings.backup_path}."))
    else:
        out.append(_item(OK, "Database", "Postgres. Uploads and application packages are stored in it as well."))

    # --- submission --------------------------------------------------------
    if not settings.allow_email_submission:
        out.append(_item(OK, "Email submission", "Off: Authorize always prepares a package for you to send."))
    elif settings.submission_test_mode:
        out.append(_item(WARN, "Email submission",
                         "Test mode: an authorized email application is sent to you, not to the funder.",
                         "Set GF_SUBMISSION_TEST_MODE=false after you have received and read one test."))
    else:
        out.append(_item(OK, "Email submission", "Live: an authorized email application is sent to the funder."))

    # --- applicant ---------------------------------------------------------
    if applicant is not None:
        if applicant.legal_name:
            out.append(_item(OK, "Applicant", f"{applicant.legal_name} ({applicant.entity_type})."))
        else:
            out.append(_item(MISSING, "Applicant", "No legal name yet. Eligibility filtering and forms depend on it.",
                             "Fill in the form on this page."))
    return out


def blocking(checks: list[dict]) -> list[dict]:
    return [c for c in checks if c["level"] == MISSING]

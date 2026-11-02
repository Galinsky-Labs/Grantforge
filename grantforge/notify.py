"""Email: the review notification to the user, and generic sending used by the
email submitter. Backends: ``console`` (prints + writes to data/outbox) and
``smtp``."""
from __future__ import annotations

import hashlib
import hmac
from html import escape as esc
import logging
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path

from .config import settings
from .db import Match

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Signed review links (so the email link works without logging in)
# --------------------------------------------------------------------------

def sign_review_token(match_id: int, expires: int | None = None) -> str:
    """Token = '<unix expiry>.<hmac>'. The link opens the review page until it expires.
    Authorizing a submission additionally requires the UI password when one is set."""
    if expires is None:
        expires = int(time.time()) + settings.review_link_days * 86400
    msg = f"review:{match_id}:{expires}".encode()
    sig = hmac.new(settings.app_secret.encode(), msg, hashlib.sha256).hexdigest()[:40]
    return f"{expires}.{sig}"


def verify_review_token(match_id: int, token: str) -> bool:
    try:
        expires_s, sig = (token or "").split(".", 1)
        expires = int(expires_s)
    except ValueError:
        return False
    if expires < time.time():
        return False
    expected = sign_review_token(match_id, expires).split(".", 1)[1]
    return hmac.compare_digest(expected, sig)


def review_url(match_id: int) -> str:
    return f"{settings.base_url}/review/{match_id}?t={sign_review_token(match_id)}"


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------

def send_email(to: str, subject: str, body: str, attachments: list[Path] | None = None, html: str | None = None,
               reply_to: str | None = None) -> dict:
    if not to:
        raise RuntimeError("No recipient address configured (GF_NOTIFY_TO)")
    msg = EmailMessage()
    msg["From"] = settings.email_from or settings.smtp_user or "grantforge@localhost"
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    for path in attachments or []:
        data = Path(path).read_bytes()
        name = Path(path).name
        import mimetypes

        guessed = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith(".docx"):
            guessed = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        maintype, subtype = guessed.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)

    if settings.email_backend == "smtp":
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60) as s:
            if settings.smtp_starttls:
                s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
        log.info("Sent email to %s: %s", to, subject)
        return {"backend": "smtp", "to": to, "subject": subject}

    # console backend: write the .eml to the outbox and print it
    out = settings.data_dir / "outbox" / f"{time.time_ns()}_{hashlib.sha1(subject.encode()).hexdigest()[:8]}.eml"
    out.write_bytes(bytes(msg))
    print(f"\n===== EMAIL (console backend) to {to} =====\nSubject: {subject}\n\n{body}\n===== written to {out} =====\n")
    return {"backend": "console", "to": to, "subject": subject, "file": str(out)}


def notify_drafts_ready(matches: list[Match], project_name: str) -> dict | None:
    if not matches:
        return None
    to = settings.notify_to
    lines = [
        f"GrantForge drafted {len(matches)} grant application(s) for \"{project_name}\".",
        "",
        "Each one is waiting for your review. Nothing is sent until you click Authorize.",
        "",
    ]
    html_rows = []
    for m in matches:
        opp = m.opportunity
        amount = (m.draft_sections or {}).get("requested_amount_usd")
        url = review_url(m.id)
        unsupported = sum(1 for f in m.fact_check or [] if f.get("status") in {"unsupported", "contradicted"})
        lines += [
            f"• {opp.title}",
            f"  Funder: {opp.funder}   Deadline: {opp.close_date or 'not stated'}   Fit score: {m.score}/100",
            f"  Requested: {('$' + format(int(amount), ',')) if amount else 'see draft'}   Channel: {opp.submission_channel}",
            f"  Open questions for you: {len(m.draft_gaps or [])}   Claims not found in your material: {unsupported}",
            f"  Review (link valid {settings.review_link_days} days): {url}",
            "",
        ]
        html_rows.append(
            f"<tr><td><a href='{esc(url)}'><b>{esc(opp.title)}</b></a><br><small>{esc(opp.funder)}</small></td>"
            f"<td>{esc(opp.close_date or '—')}</td><td>{m.score}</td><td>{len(m.draft_gaps or [])}</td>"
            f"<td>{unsupported}</td><td>{esc(opp.submission_channel)}</td></tr>"
        )
    lines.append(f"Dashboard: {settings.base_url}/")
    html = (
        f"<p>GrantForge drafted <b>{len(matches)}</b> application(s) for <b>{esc(project_name)}</b>. "
        f"Nothing is sent until you click Authorize on each review page.</p>"
        "<table border='1' cellpadding='6' style='border-collapse:collapse'>"
        "<tr><th>Opportunity</th><th>Deadline</th><th>Fit</th><th>Questions</th><th>Unsupported claims</th><th>Channel</th></tr>"
        + "".join(html_rows) + "</table>"
        f"<p><a href='{settings.base_url}/'>Open dashboard</a></p>"
    )
    return send_email(to, f"[GrantForge] {len(matches)} application draft(s) ready for review – {project_name}", "\n".join(lines), html=html)


def send_deadline_reminders(items: list[tuple[Match, int]]) -> dict | None:
    """items: (match, days_left). One digest email."""
    if not items or not settings.notify_to:
        return None
    lines = ["These drafted applications have not been submitted and their deadlines are close:", ""]
    for m, days in sorted(items, key=lambda t: t[1]):
        o = m.opportunity
        lines += [f"• {o.title}", f"  Deadline: {o.close_date} ({days} day(s) left)   State: {m.state}",
                  f"  Review: {review_url(m.id)}", ""]
    soonest = min(d for _m, d in items)
    return send_email(settings.notify_to, f"[GrantForge] {len(items)} application deadline(s) within {soonest} day(s)",
                      "\n".join(lines))


def send_budget_notice(spent: float) -> dict | None:
    if not settings.notify_to:
        return None
    return send_email(settings.notify_to, "[GrantForge] Monthly model spend cap reached",
                      f"GrantForge has spent ${spent:.2f} this month, which reaches the cap of "
                      f"${settings.monthly_budget_usd:.2f} set in GF_MONTHLY_BUDGET_USD. Searching and drafting are "
                      "paused until the next month or until you raise the cap. Drafts already written are unaffected.")

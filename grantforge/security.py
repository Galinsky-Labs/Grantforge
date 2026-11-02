"""Web security: login sessions, CSRF protection, login rate limiting, HTML
sanitising and security headers."""
from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from urllib.parse import urlparse

import nh3
from markupsafe import Markup
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .config import settings
from .db import SessionLocal, WebSession, utcnow

SESSION_COOKIE = "gf_session"
CSRF_COOKIE = "gf_csrf"
CSRF_FIELD = "csrf"


# --------------------------------------------------------------------------
# Is a password required?
# --------------------------------------------------------------------------

def is_local_only() -> bool:
    host = urlparse(settings.base_url).hostname or ""
    return host in {"localhost", "127.0.0.1", "::1"}


def password_missing_on_public_host() -> bool:
    """A public GF_BASE_URL with no GF_UI_PASSWORD is refused rather than left open."""
    return not settings.ui_password and not is_local_only()


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

def create_session(ip: str) -> str:
    token = secrets.token_urlsafe(32)
    with SessionLocal() as s:
        s.add(WebSession(token=token, ip=ip[:64], expires_at=utcnow() + timedelta(days=settings.session_days)))
        s.commit()
    return token


def session_valid(token: str | None) -> bool:
    if not token:
        return False
    with SessionLocal() as s:
        row = s.get(WebSession, token)
        if row is None:
            return False
        exp = row.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=utcnow().tzinfo)
        if exp < utcnow():
            s.delete(row)
            s.commit()
            return False
        return True


def delete_session(token: str | None) -> None:
    if not token:
        return
    with SessionLocal() as s:
        row = s.get(WebSession, token)
        if row:
            s.delete(row)
            s.commit()


def is_logged_in(request: Request) -> bool:
    if not settings.ui_password:
        return not password_missing_on_public_host()
    return session_valid(request.cookies.get(SESSION_COOKIE))


# --------------------------------------------------------------------------
# Login rate limiting (per client IP, in memory)
# --------------------------------------------------------------------------

class LoginLimiter:
    def __init__(self, max_failures: int = 5, window_seconds: int = 900):
        self.max_failures = max_failures
        self.window = window_seconds
        self._fails: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, ip: str) -> deque:
        q = self._fails[ip]
        while q and q[0] < time.time() - self.window:
            q.popleft()
        return q

    def blocked(self, ip: str) -> bool:
        with self._lock:
            return len(self._trim(ip)) >= self.max_failures

    def fail(self, ip: str) -> None:
        with self._lock:
            self._trim(ip).append(time.time())

    def reset(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


login_limiter = LoginLimiter()


def client_ip(request: Request) -> str:
    # Behind the bundled Caddy proxy the real address is in X-Forwarded-For. The header is only
    # trusted when GF_TRUST_PROXY=true, because a client talking to the app directly could forge it.
    fwd = request.headers.get("x-forwarded-for", "")
    if settings.trust_proxy and fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------------------
# CSRF: double-submit cookie. Every form carries the cookie's value; a
# cross-site page can neither read the cookie nor guess the value.
# --------------------------------------------------------------------------

def csrf_field(request: Request) -> Markup:
    token = getattr(request.state, "csrf", "")
    return Markup(f'<input type="hidden" name="{CSRF_FIELD}" value="{token}">')


async def csrf_protect(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    from fastapi import HTTPException

    cookie = request.cookies.get(CSRF_COOKIE, "")
    sent = ""
    ctype = request.headers.get("content-type", "")
    if "form" in ctype:
        form = await request.form()  # cached by Starlette; the route reads the same parsed form
        sent = str(form.get(CSRF_FIELD, ""))
    sent = sent or request.headers.get("x-csrf-token", "")
    if not cookie or not sent or not secrets.compare_digest(cookie, sent):
        raise HTTPException(403, "Form expired or was sent from another site. Reload the page and try again.")


# --------------------------------------------------------------------------
# Middleware: CSRF cookie, request size cap, security headers
# --------------------------------------------------------------------------

class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        limit = settings.max_upload_mb * 1024 * 1024 * 5
        try:
            length = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            length = 0
        if length > limit:
            return PlainTextResponse(f"Request too large (limit {settings.max_upload_mb * 5} MB per request).", 413)

        token = request.cookies.get(CSRF_COOKIE)
        new_token = None
        if not token or len(token) < 20:
            token = new_token = secrets.token_urlsafe(24)
        request.state.csrf = token
        response = await call_next(request)
        if new_token:
            response.set_cookie(CSRF_COOKIE, new_token, httponly=True, samesite="strict", secure=settings.https,
                                max_age=60 * 60 * 24 * 30)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        # no-referrer keeps the signed review link from leaking to the funder sites the page links to
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Content-Security-Policy",
                     "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                     "frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'")
        if settings.https:
            h.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response


# --------------------------------------------------------------------------
# HTML sanitising for Markdown that contains text from funders and the model
# --------------------------------------------------------------------------

ALLOWED_TAGS = {"p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "b", "i", "u", "code", "pre",
                "blockquote", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td", "a", "sup", "sub"}


def sanitize_html(html: str) -> str:
    return nh3.clean(html, tags=ALLOWED_TAGS, attributes={"a": {"href", "title"}, "td": {"align"}, "th": {"align"}},
                     url_schemes={"http", "https", "mailto"}, link_rel="noopener noreferrer nofollow")

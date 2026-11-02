"""Application configuration.

Every setting is read from environment variables (a `.env` file in the project
root is loaded automatically). `.env.example` documents each one.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass
class Settings:
    # --- storage -----------------------------------------------------------
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("GF_DATA_DIR", ROOT / "data")))
    database_url: str = field(default_factory=lambda: os.environ.get("GF_DATABASE_URL", ""))

    # --- web app -----------------------------------------------------------
    base_url: str = field(default_factory=lambda: os.environ.get("GF_BASE_URL", "http://localhost:8000").rstrip("/"))
    app_secret: str = field(default_factory=lambda: os.environ.get("GF_APP_SECRET", ""))
    ui_password: str = field(default_factory=lambda: os.environ.get("GF_UI_PASSWORD", ""))

    # --- LLM ---------------------------------------------------------------
    llm_provider: str = field(default_factory=lambda: os.environ.get("GF_LLM_PROVIDER", "anthropic"))
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.environ.get("GF_LLM_MODEL", "claude-sonnet-5-5"))
    llm_max_tokens: int = field(default_factory=lambda: _int("GF_LLM_MAX_TOKENS", 8000))
    enable_web_search_source: bool = field(default_factory=lambda: _bool("GF_ENABLE_WEB_SEARCH_SOURCE", False))
    web_search_max_uses: int = field(default_factory=lambda: _int("GF_WEB_SEARCH_MAX_USES", 8))

    # --- matching ----------------------------------------------------------
    match_threshold: int = field(default_factory=lambda: _int("GF_MATCH_THRESHOLD", 65))
    max_candidates_per_query: int = field(default_factory=lambda: _int("GF_MAX_CANDIDATES_PER_QUERY", 50))
    max_drafts_per_run: int = field(default_factory=lambda: _int("GF_MAX_DRAFTS_PER_RUN", 8))
    min_days_to_deadline: int = field(default_factory=lambda: _int("GF_MIN_DAYS_TO_DEADLINE", 7))
    include_forecasted: bool = field(default_factory=lambda: _bool("GF_INCLUDE_FORECASTED", True))

    # --- email -------------------------------------------------------------
    email_backend: str = field(default_factory=lambda: os.environ.get("GF_EMAIL_BACKEND", "console"))  # console|smtp
    smtp_host: str = field(default_factory=lambda: os.environ.get("GF_SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: _int("GF_SMTP_PORT", 587))
    smtp_user: str = field(default_factory=lambda: os.environ.get("GF_SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.environ.get("GF_SMTP_PASSWORD", ""))
    smtp_starttls: bool = field(default_factory=lambda: _bool("GF_SMTP_STARTTLS", True))
    email_from: str = field(default_factory=lambda: os.environ.get("GF_EMAIL_FROM", ""))
    notify_to: str = field(default_factory=lambda: os.environ.get("GF_NOTIFY_TO", ""))

    # --- autopilot ---------------------------------------------------------
    autopilot_interval_hours: int = field(default_factory=lambda: _int("GF_AUTOPILOT_INTERVAL_HOURS", 0))

    # --- submission --------------------------------------------------------
    allow_email_submission: bool = field(default_factory=lambda: _bool("GF_ALLOW_EMAIL_SUBMISSION", True))
    submission_test_mode: bool = field(default_factory=lambda: _bool("GF_SUBMISSION_TEST_MODE", True))
    submission_test_recipient: str = field(default_factory=lambda: os.environ.get("GF_SUBMISSION_TEST_RECIPIENT", ""))
    grants_gov_s2s_cert: str = field(default_factory=lambda: os.environ.get("GF_GRANTS_GOV_S2S_CERT", ""))
    grants_gov_s2s_key: str = field(default_factory=lambda: os.environ.get("GF_GRANTS_GOV_S2S_KEY", ""))

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "uploads").mkdir(exist_ok=True)
        (self.data_dir / "packages").mkdir(exist_ok=True)
        (self.data_dir / "outbox").mkdir(exist_ok=True)
        if not self.database_url:
            self.database_url = f"sqlite:///{self.data_dir / 'grantforge.db'}"
        if not self.app_secret:
            # Persist a generated secret so review links survive restarts.
            secret_file = self.data_dir / ".app_secret"
            if secret_file.exists():
                self.app_secret = secret_file.read_text().strip()
            else:
                self.app_secret = secrets.token_hex(32)
                secret_file.write_text(self.app_secret)


settings = Settings()

import os
import sys
import tempfile
from pathlib import Path

# Isolated data dir + mock LLM + console email for every test, set before any
# grantforge import reads the environment.
# GF_LIVE=1 (set by the live-test workflow) keeps the real model provider and real network sources.
LIVE = os.environ.get("GF_LIVE") == "1"
_tmp = tempfile.mkdtemp(prefix="gf_test_")
os.environ["GF_DATA_DIR"] = _tmp
if not LIVE:
    os.environ["GF_LLM_PROVIDER"] = "mock"
os.environ["GF_EMAIL_BACKEND"] = "console"
os.environ["GF_NOTIFY_TO"] = "applicant@example.com"
os.environ["GF_BASE_URL"] = "http://localhost:8000"
os.environ["GF_SUBMISSION_TEST_MODE"] = "false"
os.environ["GF_MIN_DAYS_TO_DEADLINE"] = "7"
os.environ.setdefault("GF_MONTHLY_BUDGET_USD", "0")
os.environ["GF_UI_PASSWORD"] = ""
os.environ["GF_RUN_WORKER"] = "false"
# CI also runs the whole suite against a real Postgres server (the same engine Neon runs).
if os.environ.get("GF_TEST_DATABASE_URL"):
    os.environ["GF_DATABASE_URL"] = os.environ["GF_TEST_DATABASE_URL"]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from sqlalchemy import MetaData  # noqa: E402

from grantforge.config import settings  # noqa: E402
from grantforge.db import SessionLocal, engine, init_db  # noqa: E402
from grantforge.llm import MockProvider  # noqa: E402


def reset_database() -> None:
    meta = MetaData()
    meta.reflect(engine)
    meta.drop_all(engine)  # includes alembic_version, so init_db runs every migration again
    init_db()


@pytest.fixture()
def session(monkeypatch):
    reset_database()
    MockProvider.overrides.clear()
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
        MockProvider.overrides.clear()


@pytest.fixture()
def data_dir() -> Path:
    return settings.data_dir

import os
import sys
import tempfile
from pathlib import Path

# Isolated data dir + mock LLM + console email for every test, set before any
# grantforge import reads the environment.
_tmp = tempfile.mkdtemp(prefix="gf_test_")
os.environ["GF_DATA_DIR"] = _tmp
os.environ["GF_LLM_PROVIDER"] = "mock"
os.environ["GF_EMAIL_BACKEND"] = "console"
os.environ["GF_NOTIFY_TO"] = "applicant@example.com"
os.environ["GF_BASE_URL"] = "http://testserver"
os.environ["GF_SUBMISSION_TEST_MODE"] = "false"
os.environ["GF_MIN_DAYS_TO_DEADLINE"] = "7"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from grantforge.db import Base, SessionLocal, engine, init_db  # noqa: E402


@pytest.fixture()
def session():
    Base.metadata.drop_all(engine)
    init_db()
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()

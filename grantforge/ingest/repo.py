"""Bring a code repository (git URL or local path) in as a document."""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from .extract import extract_directory

log = logging.getLogger(__name__)


def ingest_repo(location: str) -> tuple[str, str]:
    """Return (display_name, text) for a git URL or a local directory path."""
    p = Path(location).expanduser()
    if p.is_dir():
        return p.name, extract_directory(p)
    if location.startswith(("http://", "https://", "git@", "ssh://")):
        tmp = Path(tempfile.mkdtemp(prefix="gf_repo_"))
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", "--quiet", location, str(tmp / "repo")],
                check=True,
                capture_output=True,
                timeout=600,
            )
            name = location.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
            return name, extract_directory(tmp / "repo")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"git clone failed: {exc.stderr.decode(errors='ignore')[:500]}") from exc
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    raise RuntimeError(f"Not a directory or git URL: {location}")

"""Bring a code repository (git URL or local path) in as a document.

Security: the web UI calls :func:`ingest_repo` with ``allow_local=False``, so a
browser user can only clone public https URLs on public hosts. Reading local
directories is available from the command line only, where the operator
already has access to the machine.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..net import UnsafeURL, check_url
from .extract import extract_directory

log = logging.getLogger(__name__)


def ingest_repo(location: str, allow_local: bool = True) -> tuple[str, str]:
    """Return (display_name, text) for a git URL, or a local directory when allow_local is True."""
    location = location.strip()
    if allow_local:
        p = Path(location).expanduser()
        if p.is_dir():
            return p.name, extract_directory(p)
    if not location.startswith("https://"):
        if allow_local and location.startswith(("http://", "git@", "ssh://")):
            pass  # operator-supplied on the CLI
        else:
            raise RuntimeError("Only https:// git repository URLs can be added here.")
    if location.startswith(("https://", "http://")):
        try:
            check_url(location)
        except UnsafeURL as exc:
            raise RuntimeError(str(exc)) from exc
    tmp = Path(tempfile.mkdtemp(prefix="gf_repo_"))
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # never hang waiting for a password
    if not allow_local:
        env["GIT_ALLOW_PROTOCOL"] = "https"  # blocks file://, ext::, ssh and submodule tricks
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--quiet", "--no-recurse-submodules", location, str(tmp / "repo")],
            check=True, capture_output=True, timeout=600, env=env,
        )
        name = location.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        return name, extract_directory(tmp / "repo")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"git clone failed: {exc.stderr.decode(errors='ignore')[:500]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("git clone took longer than 10 minutes and was stopped") from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

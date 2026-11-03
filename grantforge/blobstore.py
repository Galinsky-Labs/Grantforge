"""Durable file storage in the database.

Uploads and package files are written to the local data folder as before, and
every write is also saved in the ``stored_files`` table. When a file is needed
and the local copy is missing (Render erases the container's disk on every
deploy and restart), it is restored from the database first.

Keys are paths relative to GF_DATA_DIR, for example
``packages/match_4/EPA-R1-2027-03_v2-edited_2140w_application.docx``.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import delete, select

from .config import settings
from .db import SessionLocal, StoredFile, utcnow

log = logging.getLogger(__name__)


def key_for(path: Path) -> str | None:
    try:
        return str(Path(path).resolve().relative_to(settings.data_dir.resolve()))
    except ValueError:
        return None  # outside the data folder (e.g. a CLI-ingested file); not stored


def save(path: Path) -> None:
    key = key_for(path)
    if key is None or not Path(path).is_file():
        return
    data = Path(path).read_bytes()
    with SessionLocal() as s:
        row = s.get(StoredFile, key)
        if row is None:
            s.add(StoredFile(key=key, content=data, size=len(data)))
        else:
            row.content, row.size, row.updated_at = data, len(data), utcnow()
        s.commit()


def save_many(paths) -> None:
    for p in paths:
        save(Path(p))


def ensure_local(path: Path) -> bool:
    """Make sure the file exists on local disk, restoring it from the database if needed."""
    path = Path(path)
    if path.is_file():
        return True
    key = key_for(path)
    if key is None:
        return False
    with SessionLocal() as s:
        row = s.get(StoredFile, key)
        if row is None:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(row.content)
    return True


def list_dir(directory: Path) -> list[tuple[str, int, object]]:
    """(relative name, size, updated_at) for every stored file under a directory."""
    prefix = key_for(directory)
    if prefix is None:
        return []
    prefix = prefix.rstrip("/") + "/"
    with SessionLocal() as s:
        rows = s.execute(select(StoredFile.key, StoredFile.size, StoredFile.updated_at)
                         .where(StoredFile.key.startswith(prefix))).all()
    return [(k[len(prefix):], size, ts) for k, size, ts in rows]


def remove(path: Path) -> None:
    key = key_for(path)
    if key is None:
        return
    with SessionLocal() as s:
        s.execute(delete(StoredFile).where(StoredFile.key == key))
        s.commit()

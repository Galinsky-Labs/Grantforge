#!/usr/bin/env python3
"""Command-line entry point. Same pipeline as the web UI, no browser needed.

Examples
  # create a project from a folder of material and run everything
  python scripts/run_pipeline.py --name "Potential energy harvester" --add ./docs --add ./repo --run

  # dry run with the mock LLM (no API key, no network needed)
  GF_LLM_PROVIDER=mock python scripts/run_pipeline.py --name Demo --add README.md --run

  # list projects / matches
  python scripts/run_pipeline.py --list
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from grantforge.db import Document, Match, Project, SessionLocal, init_db  # noqa: E402
from grantforge.ingest import extract_directory, extract_with_warning, ingest_repo  # noqa: E402
from grantforge.pipeline import run_pipeline  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", help="project name (created if it does not exist)")
    ap.add_argument("--notes", default="", help="free-text notes for the grant writer")
    ap.add_argument("--add", action="append", default=[], help="file, directory, zip or git URL to ingest (repeatable)")
    ap.add_argument("--attach", action="append", default=[], help="file to ingest AND send with every application (repeatable)")
    ap.add_argument("--run", action="store_true", help="run the full pipeline after ingesting")
    ap.add_argument("--max-drafts", type=int, help="override GF_MAX_DRAFTS_PER_RUN for this run")
    ap.add_argument("--backup", action="store_true", help="write a backup archive now and exit")
    ap.add_argument("--rebuild-profile", action="store_true")
    ap.add_argument("--list", action="store_true", help="list projects and their matches")
    args = ap.parse_args()

    init_db()
    if args.backup:
        from grantforge.maintenance import backup

        print(backup())
        return 0
    args.add = list(args.add) + [a for a in args.attach if a not in args.add]
    if args.max_drafts:
        from grantforge.config import settings

        settings.max_drafts_per_run = args.max_drafts
    with SessionLocal() as s:
        if args.list:
            for p in s.scalars(select(Project)).all():
                print(f"[{p.id}] {p.name} — {p.status} — {len(p.documents)} inputs")
                for m in s.scalars(select(Match).where(Match.project_id == p.id).order_by(Match.score.desc())).all():
                    if m.state != "rejected":
                        print(f"    {m.score:3d} {m.state:12s} {m.opportunity.title[:70]}  ({m.opportunity.close_date or 'no deadline'})")
            return 0
        if not args.name:
            ap.error("--name is required unless --list")
        p = s.scalar(select(Project).where(Project.name == args.name))
        if p is None:
            p = Project(name=args.name, notes=args.notes)
            s.add(p)
            s.commit()
            print(f"Created project {p.id}: {p.name}")
        elif args.notes:
            p.notes = args.notes
        for loc in args.add:
            path = Path(loc).expanduser()
            warning = ""
            if path.is_dir():
                name, text, kind = path.name, extract_directory(path), "repo"
            elif path.is_file():
                (text, warning), name, kind = extract_with_warning(path), path.name, "file"
            else:
                name, text = ingest_repo(loc)
                kind = "repo"
            s.add(Document(project_id=p.id, kind=kind, name=name, path=str(path.resolve()) if path.is_file() else str(loc),
                           text=text, char_count=len(text), warning=warning, is_attachment=loc in args.attach))
            print(f"  added {kind} {name}: {len(text):,} chars" + (f"  WARNING: {warning}" if warning else ""))
        s.commit()
        if args.run:
            result = run_pipeline(s, p, print, rebuild_profile=args.rebuild_profile)
            print(f"\nDone: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

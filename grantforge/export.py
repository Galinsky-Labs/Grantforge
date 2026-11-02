"""Write a match's application package to disk: Markdown, DOCX, JSON metadata."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .db import Match


def write_package(match: Match, pkg_dir: Path) -> Path:
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "application.md").write_text(match.draft_markdown or "", encoding="utf-8")
    write_docx(match, pkg_dir / "application.docx")
    opp = match.opportunity
    meta = {
        "match_id": match.id,
        "opportunity": {
            "title": opp.title,
            "funder": opp.funder,
            "url": opp.url,
            "close_date": opp.close_date,
            "source": opp.source,
            "source_id": opp.source_id,
            "submission_channel": opp.submission_channel,
            "submission_target": opp.submission_target,
            "number": (opp.raw or {}).get("number"),
        },
        "requested_amount_usd": (match.draft_sections or {}).get("requested_amount_usd"),
        "checklist": match.draft_checklist or [],
        "gaps": match.draft_gaps or [],
        "cover_email": (match.draft_sections or {}).get("cover_email", {}),
    }
    (pkg_dir / "package.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    checklist_lines = ["# Submission checklist", ""]
    for item in match.draft_checklist or []:
        mark = "x" if item.get("status") == "drafted" else " "
        checklist_lines.append(f"- [{mark}] {item.get('item')} — {item.get('status')}" + (f": {item.get('note')}" if item.get("note") else ""))
    checklist_lines += ["", "# Open questions for the applicant", ""] + [f"- {g}" for g in match.draft_gaps or []]
    (pkg_dir / "CHECKLIST.md").write_text("\n".join(checklist_lines), encoding="utf-8")
    return pkg_dir


_INLINE = re.compile(r"(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)")


def _add_inline(paragraph, text: str) -> None:
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            paragraph.add_run(part[2:-2]).bold = True
        elif part.startswith("*") and part.endswith("*"):
            paragraph.add_run(part[1:-1]).italic = True
        elif part.startswith("`") and part.endswith("`"):
            run = paragraph.add_run(part[1:-1])
            run.font.name = "Courier New"
        else:
            paragraph.add_run(part)


def write_docx(match: Match, path: Path) -> None:
    import docx
    from docx.shared import Pt

    d = match.draft_sections or {}
    doc = docx.Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    doc.add_heading(d.get("title", "Application"), level=0)
    opp = match.opportunity
    p = doc.add_paragraph()
    p.add_run("Funding opportunity: ").bold = True
    p.add_run(opp.title)
    p = doc.add_paragraph()
    p.add_run("Funder: ").bold = True
    p.add_run(opp.funder or "")
    if opp.close_date:
        p = doc.add_paragraph()
        p.add_run("Deadline: ").bold = True
        p.add_run(opp.close_date)
    if d.get("requested_amount_usd"):
        p = doc.add_paragraph()
        p.add_run("Amount requested: ").bold = True
        p.add_run(f"${int(d['requested_amount_usd']):,}")

    for sec in d.get("sections", []):
        doc.add_heading(sec.get("heading", ""), level=1)
        for block in str(sec.get("body", "")).split("\n\n"):
            block = block.strip()
            if not block:
                continue
            lines = block.splitlines()
            if all(re.match(r"^\s*([-*•]|\d+[.)])\s+", ln) for ln in lines):
                for ln in lines:
                    text = re.sub(r"^\s*([-*•]|\d+[.)])\s+", "", ln)
                    style_name = "List Number" if re.match(r"^\s*\d+[.)]", ln) else "List Bullet"
                    _add_inline(doc.add_paragraph(style=style_name), text)
            elif lines[0].startswith("### "):
                doc.add_heading(lines[0][4:], level=2)
                rest = "\n".join(lines[1:]).strip()
                if rest:
                    _add_inline(doc.add_paragraph(), rest)
            elif "|" in block and len(lines) >= 2 and set(lines[1].replace("|", "").strip()) <= set("-: "):
                rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in lines if not set(ln.replace("|", "").strip()) <= set("-: ")]
                table = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows))
                table.style = "Table Grid"
                for i, r in enumerate(rows):
                    for j, cell in enumerate(r):
                        table.cell(i, j).text = cell
            else:
                _add_inline(doc.add_paragraph(), " ".join(ln.strip() for ln in lines))
    doc.save(str(path))

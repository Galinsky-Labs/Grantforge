"""Write a match's application package to disk.

Every file name carries the opportunity, the version number, what changed in
that version (draft, redraft, edited) and the length, for example
``NSF-26-501_v3-edited_2140w_application.docx``. Earlier versions stay in the
folder, so the newest file is always the one with the highest version number.
"""
from __future__ import annotations

import csv
import json
import re
import shutil
from pathlib import Path

from .db import Match

BUDGET_LABELS = {
    "personnel": "a. Personnel", "fringe": "b. Fringe Benefits", "travel": "c. Travel", "equipment": "d. Equipment",
    "supplies": "e. Supplies", "contractual": "f. Contractual", "construction": "g. Construction", "other": "h. Other",
    "indirect": "j. Indirect Charges",
}


def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text or "").strip("-.")
    return (s[:n] or "application").rstrip("-.")


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def file_prefix(match: Match, change: str) -> str:
    opp = match.opportunity
    base = slug((opp.raw or {}).get("number") or opp.title, 40)
    return f"{base}_v{match.version}-{change}"


def write_package(match: Match, pkg_dir: Path, change: str = "draft") -> dict:
    pkg_dir.mkdir(parents=True, exist_ok=True)
    words = word_count(match.draft_markdown)
    prefix = file_prefix(match, change)
    files: dict = {}

    files["md"] = f"{prefix}_{words}w_application.md"
    (pkg_dir / files["md"]).write_text(match.draft_markdown or "", encoding="utf-8")

    files["docx"] = f"{prefix}_{words}w_application.docx"
    write_docx(match, pkg_dir / files["docx"])

    if match.budget and match.budget.get("total"):
        files["budget_csv"] = f"{prefix}_budget-${match.budget['total']}.csv"
        write_budget_csv(match.budget, pkg_dir / files["budget_csv"])

    opp = match.opportunity
    meta = {
        "match_id": match.id,
        "version": match.version,
        "change": change,
        "opportunity": {
            "title": opp.title, "funder": opp.funder, "url": opp.url, "close_date": opp.close_date,
            "source": opp.source, "source_id": opp.source_id, "submission_channel": opp.submission_channel,
            "submission_target": opp.submission_target, "number": (opp.raw or {}).get("number"),
        },
        "requested_amount_usd": (match.draft_sections or {}).get("requested_amount_usd"),
        "budget": match.budget,
        "checklist": match.draft_checklist or [],
        "gaps": match.draft_gaps or [],
        "length_report": match.length_report or [],
        "fact_check": match.fact_check or [],
        "cover_email": (match.draft_sections or {}).get("cover_email", {}),
    }
    files["json"] = f"{prefix}_package.json"
    (pkg_dir / files["json"]).write_text(json.dumps(meta, indent=2), encoding="utf-8")

    lines = [f"# Submission checklist (version {match.version}, {change})", ""]
    for item in match.draft_checklist or []:
        mark = "x" if item.get("status") == "drafted" else " "
        lines.append(f"- [{mark}] {item.get('item')} — {item.get('status')}" + (f": {item.get('note')}" if item.get("note") else ""))
    lines += ["", "# Open questions for the applicant", ""] + [f"- {g}" for g in match.draft_gaps or []]
    bad = [f for f in match.fact_check or [] if f.get("status") in {"unsupported", "contradicted"}]
    if bad:
        lines += ["", "# Claims not found in your material", ""]
        lines += [f"- [{f['status']}] {f['section']}: {f['claim']}" for f in bad]
    over = [r for r in match.length_report or [] if not r.get("ok")]
    if over:
        lines += ["", "# Sections over the funder's limit", ""]
        lines += [f"- {r['section']}: {r['actual']} {r['unit']} against a limit of {r['limit']}" for r in over]
    files["checklist"] = f"{prefix}_checklist.md"
    (pkg_dir / files["checklist"]).write_text("\n".join(lines), encoding="utf-8")

    att_dir = pkg_dir / "attachments"
    files["attachments"] = []
    for doc in match.project.documents:
        if doc.is_attachment and doc.path and Path(doc.path).is_file():
            att_dir.mkdir(exist_ok=True)
            shutil.copy2(doc.path, att_dir / Path(doc.path).name)
            files["attachments"].append(f"attachments/{Path(doc.path).name}")
    return files


def write_budget_csv(budget: dict, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["SF-424A object class", "Item", "Amount (USD)"])
        for key, label in BUDGET_LABELS.items():
            for it in budget["lines"].get(key, []):
                w.writerow([label, it["item"], it["amount"]])
        w.writerow([])
        for key, label in BUDGET_LABELS.items():
            w.writerow([label, "Category total", budget["totals"].get(key, 0)])
        w.writerow(["i. Total Direct Charges", "", budget["direct"]])
        w.writerow(["j. Indirect Charges", "", budget["indirect"]])
        w.writerow(["k. TOTALS", "", budget["total"]])


_INLINE = re.compile(r"(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)")


def _add_inline(paragraph, text: str) -> None:
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            paragraph.add_run(part[2:-2]).bold = True
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            paragraph.add_run(part[1:-1]).italic = True
        elif part.startswith("`") and part.endswith("`"):
            paragraph.add_run(part[1:-1]).font.name = "Courier New"
        else:
            paragraph.add_run(part)


def _add_markdown_blocks(doc, body: str) -> None:
    for block in str(body).split("\n\n"):
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
                _add_markdown_blocks(doc, rest)
        elif "|" in block and len(lines) >= 2 and set(lines[1].replace("|", "").strip()) <= set("-: "):
            rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in lines
                    if not set(ln.replace("|", "").strip()) <= set("-: ")]
            table = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows))
            table.style = "Table Grid"
            for i, r in enumerate(rows):
                for j, cell in enumerate(r):
                    table.cell(i, j).text = cell.replace("**", "")
        else:
            _add_inline(doc.add_paragraph(), " ".join(ln.strip() for ln in lines))


def write_docx(match: Match, path: Path) -> None:
    import docx
    from docx.shared import Pt

    d = match.draft_sections or {}
    doc = docx.Document()
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)
    doc.add_heading(d.get("title", "Application"), level=0)
    opp = match.opportunity
    meta = [("Funding opportunity", opp.title), ("Opportunity number", (opp.raw or {}).get("number")),
            ("Funder", opp.funder), ("Deadline", opp.close_date)]
    if d.get("requested_amount_usd"):
        meta.append(("Amount requested", f"${int(d['requested_amount_usd']):,}"))
    for label, value in meta:
        if value:
            p = doc.add_paragraph()
            p.add_run(f"{label}: ").bold = True
            p.add_run(str(value))
    for sec in d.get("sections", []):
        doc.add_heading(sec.get("heading", ""), level=1)
        _add_markdown_blocks(doc, sec.get("body", ""))
    b = match.budget
    if b and b.get("total"):
        doc.add_heading("Budget Summary", level=1)
        rows = [("Object class", "Item", "Amount")]
        for key, label in BUDGET_LABELS.items():
            for it in b["lines"].get(key, []):
                rows.append((label, it["item"], f"${it['amount']:,}"))
        rows += [("", "Total direct", f"${b['direct']:,}"), ("", "Indirect", f"${b['indirect']:,}"),
                 ("", "Total", f"${b['total']:,}")]
        table = doc.add_table(rows=len(rows), cols=3)
        table.style = "Table Grid"
        for i, r in enumerate(rows):
            for j, cell in enumerate(r):
                table.cell(i, j).text = cell
    doc.save(str(path))

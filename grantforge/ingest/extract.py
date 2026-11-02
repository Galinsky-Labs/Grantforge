"""Turn any supported input file into plain text."""
from __future__ import annotations

import io
import json
import logging
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".html", ".htm", ".xml", ".tex", ".bib",
}
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp",
    ".cs", ".rb", ".php", ".sh", ".bash", ".zsh", ".sql", ".r", ".jl", ".scala", ".m", ".mm", ".dart", ".lua",
    ".sol", ".vue", ".svelte", ".css", ".scss", ".proto", ".graphql", ".tf", ".dockerfile",
}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", "target", ".idea", ".vscode", "vendor"}
SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Cargo.lock", "Pipfile.lock"}
MAX_FILE_CHARS = 60_000
MAX_REPO_CHARS = 400_000


def extract_text(path: Path) -> str:
    """Extract text from a single file. Returns '' for unsupported binaries."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            return _pdf(path)
        if suffix == ".docx":
            return _docx(path)
        if suffix == ".zip":
            return extract_zip(path)
        if suffix == ".ipynb":
            return _ipynb(path)
        if suffix in TEXT_EXTENSIONS or suffix in CODE_EXTENSIONS or path.name.lower() in {"dockerfile", "makefile", "readme", "license"}:
            return _plain(path)
        # Unknown extension: try as UTF-8 text, give up if it is binary.
        return _plain(path, strict=True)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Could not extract %s: %s", path, exc)
        return ""


def _plain(path: Path, strict: bool = False) -> str:
    data = path.read_bytes()
    if strict and b"\x00" in data[:4096]:
        return ""
    return data.decode("utf-8", errors="ignore")[:MAX_FILE_CHARS]


def _pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n\n".join(pages)[:MAX_FILE_CHARS * 4]


def _docx(path: Path) -> str:
    import docx

    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs]
    for table in d.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)[:MAX_FILE_CHARS * 4]


def _ipynb(path: Path) -> str:
    nb = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    out = []
    for cell in nb.get("cells", []):
        src = "".join(cell.get("source", []))
        out.append(f"### {cell.get('cell_type')} cell\n{src}")
    return "\n\n".join(out)[:MAX_FILE_CHARS]


def extract_zip(path: Path) -> str:
    """Treat a zip as a code base / document bundle and concatenate its text."""
    with zipfile.ZipFile(path) as zf:
        return _bundle_from_members(
            ((info.filename, lambda i=info: zf.read(i)) for info in zf.infolist() if not info.is_dir()),
        )


def extract_directory(root: Path) -> str:
    """Concatenate every readable text/code file under ``root`` (a repository checkout)."""
    def members():
        for p in sorted(root.rglob("*")):
            if p.is_dir():
                continue
            if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
                continue
            yield str(p.relative_to(root)), (lambda q=p: q.read_bytes())

    return _bundle_from_members(members())


def _bundle_from_members(members) -> str:
    """Shared logic for zip and directory bundles. Prioritises READMEs and docs."""
    collected: list[tuple[int, str, str]] = []  # (priority, name, text)
    for name, reader in members:
        parts = Path(name).parts
        if any(part in SKIP_DIRS for part in parts) or Path(name).name in SKIP_FILES:
            continue
        suffix = Path(name).suffix.lower()
        base = Path(name).name.lower()
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader

                text = "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(reader())).pages)
            except Exception:
                continue
        elif suffix == ".docx":
            try:
                import docx

                d = docx.Document(io.BytesIO(reader()))
                text = "\n".join(p.text for p in d.paragraphs)
            except Exception:
                continue
        elif suffix in TEXT_EXTENSIONS or suffix in CODE_EXTENSIONS or base in {"readme", "dockerfile", "makefile", "license"}:
            data = reader()
            if b"\x00" in data[:4096]:
                continue
            text = data.decode("utf-8", errors="ignore")
        else:
            continue
        text = text[:MAX_FILE_CHARS]
        if not text.strip():
            continue
        if base.startswith("readme"):
            prio = 0
        elif suffix in {".md", ".rst", ".txt", ".pdf", ".docx"}:
            prio = 1
        elif base in {"pyproject.toml", "package.json", "cargo.toml", "go.mod", "setup.py"}:
            prio = 2
        else:
            prio = 3
        collected.append((prio, name, text))
    collected.sort(key=lambda t: (t[0], t[1]))
    out: list[str] = []
    total = 0
    for _prio, name, text in collected:
        chunk = f"\n\n===== FILE: {name} =====\n{text}"
        if total + len(chunk) > MAX_REPO_CHARS:
            out.append(f"\n\n===== FILE: {name} ===== (omitted: bundle size limit reached)")
            continue
        out.append(chunk)
        total += len(chunk)
    return "".join(out)

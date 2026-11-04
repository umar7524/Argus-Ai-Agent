"""
Argus - Vault MCP Server (Hand #2)
An agentic deep-research assistant built on the Model Context Protocol (MCP).

Gives the brain access to ONE private folder: Argus\\vault
Security design (lessons from the troubleshooting journal):
  - Allow-list of one folder: every path is resolved and must stay inside it
    (blocks tricks like "..\\.env" or "C:\\Windows\\...")
  - Nothing the AI does is irreversible:
      * edits replace the file, but the old version goes to vault\\.trash first
        (or, if asked, the edit is saved as a separate copy in vault\\edited)
      * copy / move / rename never overwrite an existing file
      * delete moves things to vault\\.trash, from where they can be restored
      * there is NO tool to empty the trash: only the user can do that
  - No internet tool here: private files can never leak into a web search
  - Access log: records WHICH files were read and when (never their contents)
"""

import json
import os
import re
import shutil
import sys
from xml.sax.saxutils import escape as xml_escape
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from mcp.server import MCPServer as FastMCP

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VAULT_DIR = Path(os.environ.get("VAULT_DIR", PROJECT_ROOT / "vault")).resolve()
ACCESS_LOG = PROJECT_ROOT / "data" / "vault_access_log.jsonl"
EDITS_DIR = VAULT_DIR / "edited"
TRASH_DIR = VAULT_DIR / ".trash"
TRASH_INDEX = TRASH_DIR / "index.json"      # remembers where deleted items came from
TAILORED_DIR = VAULT_DIR / "tailored_cvs"
EDITABLE = {".txt", ".md", ".csv", ".docx", ".xlsx", ".pdf"}

# Plain-text formats: read as they are (notes, data, config and code files).
TEXT_TYPES = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".log", ".xml", ".yaml", ".yml",
    ".ini", ".cfg", ".toml", ".sql", ".py", ".js", ".ts", ".java", ".c", ".cpp",
    ".h", ".cs", ".php", ".rb", ".go", ".r", ".sh", ".bat", ".css", ".ipynb", ".tex",
}
HTML_TYPES = {".html", ".htm"}
OFFICE_TYPES = {".pdf", ".docx", ".xlsx", ".xlsm", ".xls", ".pptx", ".rtf"}
SUPPORTED = TEXT_TYPES | HTML_TYPES | OFFICE_TYPES
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"}
OLD_OFFICE = {".doc": ".docx", ".ppt": ".pptx"}   # old binary formats: ask to re-save
MAX_SHEET_ROWS = 500
MAX_READ_CHARS = 2500      # per read, keeps requests under free-tier limits
MAX_PDF_PAGES = 30

mcp = FastMCP("argus-vault")


# ------------------------------------------------------------------ safety
def _safe_path(relative: str) -> Path:
    """
    Turn a user/model supplied path into a real path INSIDE the vault.
    Raises ValueError for anything that points outside it.
    """
    candidate = (VAULT_DIR / relative).resolve()
    if candidate != VAULT_DIR and VAULT_DIR not in candidate.parents:
        raise ValueError("Access denied: that path is outside the vault folder.")
    if candidate == TRASH_DIR or TRASH_DIR in candidate.parents:
        raise ValueError("The trash can only be used through list_trash and restore_file.")
    return candidate


def _log_access(tool: str, target: str) -> None:
    try:
        ACCESS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ACCESS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "time": datetime.now().isoformat(timespec="seconds"),
                "tool": tool,
                "target": target,
            }, ensure_ascii=False) + "\n")
    except Exception as e:                      # logging must never break reading
        print(f"WARNING: could not write vault access log: {e}", file=sys.stderr)


# ------------------------------------------------------------------ reading
def _extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in TEXT_TYPES:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix in HTML_TYPES:
        from html.parser import HTMLParser

        class _TextOnly(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts, self._skip = [], 0
            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style"):
                    self._skip += 1
            def handle_endtag(self, tag):
                if tag in ("script", "style") and self._skip:
                    self._skip -= 1
            def handle_data(self, data):
                if not self._skip and data.strip():
                    self.parts.append(data.strip())

        parser = _TextOnly()
        parser.feed(path.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(parser.parts)
    if suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(str(path), read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            out.append(f"--- Sheet: {ws.title} ---")
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= MAX_SHEET_ROWS:
                    out.append(f"(sheet has more rows; first {MAX_SHEET_ROWS} shown)")
                    break
                if any(c is not None for c in row):
                    out.append(" | ".join("" if c is None else str(c) for c in row))
        wb.close()
        return "\n".join(out)
    if suffix == ".xls":
        import xlrd
        book = xlrd.open_workbook(str(path))
        out = []
        for sheet in book.sheets():
            out.append(f"--- Sheet: {sheet.name} ---")
            for r in range(min(sheet.nrows, MAX_SHEET_ROWS)):
                out.append(" | ".join(str(c) for c in sheet.row_values(r)))
        return "\n".join(out)
    if suffix == ".pptx":
        from pptx import Presentation
        out = []
        for n, slide in enumerate(Presentation(str(path)).slides, 1):
            out.append(f"--- Slide {n} ---")
            for shape in slide.shapes:
                if shape.has_text_frame and shape.text_frame.text.strip():
                    out.append(shape.text_frame.text.strip())
                if getattr(shape, "has_table", False) and shape.has_table:
                    for row in shape.table.rows:
                        out.append(" | ".join(c.text for c in row.cells))
        return "\n".join(out)
    if suffix == ".rtf":
        raw = path.read_text(encoding="utf-8", errors="replace")
        raw = re.sub(r"\\'[0-9a-fA-F]{2}", "", raw)        # escaped characters
        raw = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", raw)        # control words
        return re.sub(r"[{}]", "", raw).strip()
    if suffix == ".pdf":
        # PyMuPDF keeps the spaces between words; pypdf often glues them
        # ("datamanipulation", "KhanCERTIFICATIONS"), which confuses both the
        # model and the checks that compare a tailored CV with the original.
        # pypdf stays as the fallback.
        try:
            with _fitz().open(str(path)) as doc:
                # sort=True reads in visual order (top to bottom, left to right). Without it a
                # CV made in a design tool comes out scrambled (headings and dates detached).
                text = "\n".join(pg.get_text(sort=True) for pg in list(doc)[:MAX_PDF_PAGES])
            if text.strip():
                return text
        except Exception:
            pass
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        pages = reader.pages[:MAX_PDF_PAGES]
        return "\n".join((p.extract_text() or "") for p in pages)
    if suffix == ".docx":
        import docx
        document = docx.Document(str(path))
        return "\n".join(p.text for p in document.paragraphs)
    if _looks_like_text(path):
        return path.read_text(encoding="utf-8", errors="replace")
    raise ValueError(f"Unsupported file type: {suffix}")


def _looks_like_text(path: Path) -> bool:
    """
    Fallback for unknown extensions: peek at the first bytes. Text files have
    no zero bytes and decode as UTF-8, so any unknown text format still works.
    """
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return False
    if not chunk or b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _can_read(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in SUPPORTED:
        return True
    if suffix in IMAGE_TYPES or suffix in OLD_OFFICE:
        return False
    return _looks_like_text(path)


def _all_files() -> list[Path]:
    """Every file in the vault, whatever its type (trash excluded)."""
    if not VAULT_DIR.exists():
        return []
    return sorted(
        p for p in VAULT_DIR.rglob("*")
        if p.is_file()
        and VAULT_DIR in p.resolve().parents        # skip links pointing outside
        and TRASH_DIR not in p.parents              # deleted files stay out of sight
    )


def _vault_files() -> list[Path]:
    """Only the files whose text Vault can read (used for searching)."""
    return [p for p in _all_files() if _can_read(p)]


# ------------------------------------------------------------------ tools
@mcp.tool()
def list_vault_files() -> dict[str, Any]:
    """
    List ALL files in the user's private vault: name, type, size, and whether
    Vault can read the contents. Unreadable files (e.g. images) can still be
    copied, moved, renamed or deleted. Call this first.
    """
    _log_access("list_vault_files", "*")
    files = []
    for p in _all_files():
        suffix = p.suffix.lower()
        entry = {
            "path": str(p.relative_to(VAULT_DIR)),
            "type": suffix.lstrip(".") or "unknown",
            "size_kb": round(p.stat().st_size / 1024, 1),
            "can_read": _can_read(p),
            "modified": int(p.stat().st_mtime),
        }
        if suffix in IMAGE_TYPES:
            entry["note"] = "image: contents can't be read, but it can be organised"
        elif suffix in OLD_OFFICE:
            entry["note"] = f"old format: re-save it as {OLD_OFFICE[suffix]} to read it"
        elif not entry["can_read"]:
            entry["note"] = "unknown binary format: can be organised, not read"
        files.append(entry)
    if not files:
        return {"files": [], "note": f"The vault is empty. Put files in {VAULT_DIR}"}
    return {"files": files, "count": len(files)}


@mcp.tool()
def read_vault_files(paths: list) -> dict[str, Any]:
    """
    Read SEVERAL vault files in one call (e.g. the CV and profile.md
    together). Prefer this over calling read_vault_file multiple times in a
    row -- one batched call instead of several separate ones saves real time
    and API usage. Each file is truncated the same way as read_vault_file;
    for a file longer than that, read it individually with read_vault_file
    to page through the rest.

    Args:
        paths: list of file paths, exactly as shown by list_vault_files.
    """
    results = []
    for path in paths:
        try:
            file_path = _safe_path(path)
            if not file_path.is_file():
                results.append({"path": path, "error": f"File not found in vault: {path}"})
                continue
            suffix = file_path.suffix.lower()
            if suffix in IMAGE_TYPES:
                results.append({"path": path, "error": f"{path} is an image, can't read contents."})
                continue
            if suffix in OLD_OFFICE:
                results.append({"path": path, "error": f"{path} uses the old {suffix} format."})
                continue
            if not _can_read(file_path):
                results.append({"path": path, "error": f"Vault can't read {suffix or 'this'} files."})
                continue
            text = _extract_text(file_path)
        except Exception as e:
            results.append({"path": path, "error": str(e)})
            continue
        _log_access("read_vault_files", path)
        chunk = text[:MAX_READ_CHARS]
        results.append({
            "path": path,
            "text": chunk,
            "total_chars": len(text),
            "truncated": len(text) > MAX_READ_CHARS,
        })
    return {"files": results}


@mcp.tool()
def read_vault_file(path: str, start: int = 0) -> dict[str, Any]:
    """
    Read the text of one file from the private vault.

    Args:
        path: the file path exactly as shown by list_vault_files.
        start: character position to continue from, for long files
            (use the next_start value from a previous read).
    """
    try:
        file_path = _safe_path(path)
        if not file_path.is_file():
            return {"error": f"File not found in vault: {path}"}
        suffix = file_path.suffix.lower()
        if suffix in IMAGE_TYPES:
            return {"error": f"{path} is an image. Vault can't read what's inside "
                             "images yet, but it can copy, move, rename or delete it."}
        if suffix in OLD_OFFICE:
            return {"error": f"{path} uses the old {suffix} format. Open it and "
                             f"save it as {OLD_OFFICE[suffix]}, then Vault can read it."}
        if not _can_read(file_path):
            return {"error": f"Vault doesn't know how to read {suffix or 'this'} files. "
                             "It can still copy, move, rename or delete it."}
        text = _extract_text(file_path)
    except Exception as e:
        return {"error": str(e), "path": path}

    _log_access("read_vault_file", path)
    start = max(0, int(start))
    chunk = text[start:start + MAX_READ_CHARS]
    end = start + len(chunk)
    return {
        "path": path,
        "text": chunk,
        "total_chars": len(text),
        "next_start": end if end < len(text) else None,
    }


@mcp.tool()
def search_vault(query: str) -> dict[str, Any]:
    """
    Search all vault files for a word or phrase (case-insensitive).
    Returns matching files with the lines around each match.
    """
    _log_access("search_vault", query)
    needle = query.lower().strip()
    if not needle:
        return {"error": "Empty search query."}

    matches = []
    for p in _vault_files():
        try:
            lines = _extract_text(p).splitlines()
        except Exception:
            continue
        hits = [i for i, line in enumerate(lines) if needle in line.lower()]
        if hits:
            snippets = [
                " ".join(lines[max(0, i - 1): i + 2]).strip()[:300]
                for i in hits[:3]
            ]
            matches.append({"path": str(p.relative_to(VAULT_DIR)), "snippets": snippets})

    return {"query": query, "matches": matches[:10], "match_count": len(matches)}


# ------------------------------------------------------------------ editing
def _tidy(text: str) -> str:
    """Clean up leftovers after removing a word from a list, e.g. 'A, , B'."""
    text = re.sub(r"\s*,\s*,", ",", text)
    text = re.sub(r"(^|:)\s*,\s*", r"\1 ", text)
    text = re.sub(r"\s*,\s*$", "", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _replace_ci(text: str, find: str, replace: str) -> tuple[str, int]:
    return re.subn(re.escape(find), replace, text, flags=re.IGNORECASE)


def _sub_in_runs(para, pattern: str, repl: str) -> int:
    """
    Regex-replace across a Word paragraph WITHOUT flattening its formatting.
    Word splits text into "runs" (pieces with their own bold/italic/font); a
    phrase like "Advanced Excel" can be spread over two runs. We find the match
    in the joined text, then trim only the characters it covers in each run,
    putting the replacement into the first run it touches.
    """
    count, search_from = 0, 0
    while True:
        full = "".join(r.text for r in para.runs)
        m = re.compile(pattern, re.IGNORECASE).search(full, search_from)
        if not m or m.start() == m.end():
            return count
        start, end = m.span()
        pos, inserted = 0, False
        for run in para.runs:
            r_start, r_end = pos, pos + len(run.text)
            pos = r_end
            if r_end <= start or r_start >= end:
                continue
            cut_from = max(start, r_start) - r_start
            cut_to = min(end, r_end) - r_start
            run.text = run.text[:cut_from] + ("" if inserted else repl) + run.text[cut_to:]
            inserted = True
        count += 1
        search_from = start + len(repl)      # never re-match our own replacement


# Leftovers after deleting an item from a list, e.g. "SQL, , Power BI".
_TIDY_RULES = [
    (r"^\s*[,|]\s*", ""),               # list now starts with a separator
    (r"\s*,\s*,", ","),                 # "A, , B"
    (r"\s*\|\s*\|", " |"),              # "A |  | B"
    (r":\s*[,|]\s*", ": "),             # "Skills: , A"
    (r"\s+(and|or|&)\s*(?=[,.;:)])", ""), # "SQL and , focused" -> "SQL, focused"
    (r",\s*(and|or)\s*,", ","),          # "A, and , B"
    (r"\s+([,.;:)])", r"\1"),            # no space before punctuation
    (r"\s*[,|]\s*$", ""),               # list now ends with a separator
    (r"[ \t]{2,}", " "),                 # double spaces
]


def _edit_docx(src: Path, dst: Path, find: str, replace: str) -> int:
    import docx
    document = docx.Document(str(src))
    paragraphs = list(document.paragraphs)
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                paragraphs.extend(cell.paragraphs)

    changes = 0
    for para in paragraphs:
        if find.lower() not in para.text.lower():
            continue
        changes += _sub_in_runs(para, re.escape(find), replace)
        for pattern, repl in _TIDY_RULES:
            _sub_in_runs(para, pattern, repl)
        if not para.text.strip() or re.fullmatch(r"[\W_]+", para.text.strip()):
            para._element.getparent().remove(para._element)   # drop the empty line
    if changes:
        document.save(str(dst))
    return changes


def _edit_xlsx(src: Path, dst: Path, find: str, replace: str) -> int:
    """Replace text in string cells only; numbers and formulas are left alone."""
    from openpyxl import load_workbook
    wb = load_workbook(str(src))
    changes = 0
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type == "s" and isinstance(cell.value, str):
                    new_value, n = _replace_ci(cell.value, find, replace)
                    if n:
                        cell.value = _tidy(new_value) if not replace else new_value
                        changes += n
    if changes:
        wb.save(str(dst))
    return changes


FONT_INDEX_FILE = PROJECT_ROOT / "data" / "font_index.json"
_font_index = None


def _norm_font(name: str) -> str:
    """'Arial-BoldMT' / 'Arial Bold' -> 'arialbold'; 'Carlito-Regular' -> 'carlito'."""
    n = re.sub(r"[^a-z0-9]", "", name.split("+")[-1].lower())
    n = re.sub(r"(psmt|mt)$", "", n)
    return n.replace("regular", "")


def _system_fonts() -> dict:
    """
    Map font names to font files installed on this computer (built once, then
    cached in data\\font_index.json so later edits are fast).
    """
    global _font_index
    if _font_index is not None:
        return _font_index
    dirs = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts",
            Path("/usr/share/fonts"), Path("/Library/Fonts"), Path.home() / ".fonts"]
    signature = str([(str(d), d.stat().st_mtime) for d in dirs if d.exists()])
    try:
        cached = json.loads(FONT_INDEX_FILE.read_text(encoding="utf-8"))
        if cached.get("signature") == signature:
            _font_index = cached["fonts"]
            return _font_index
    except (OSError, ValueError):
        pass
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    index = {}
    for d in dirs:
        if not d.exists():
            continue
        for f in d.rglob("*"):
            if f.suffix.lower() in (".ttf", ".otf"):
                try:
                    index.setdefault(_norm_font(fitz.Font(fontfile=str(f)).name), str(f))
                except Exception:
                    continue
    _font_index = index
    try:
        FONT_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        FONT_INDEX_FILE.write_text(json.dumps({"signature": signature, "fonts": index}), encoding="utf-8")
    except OSError:
        pass
    return index


def _pdf_font_for(page, doc, span, cache: dict, fitz):
    """
    Find the SAME font the PDF uses, installed on this computer (PDFs made by
    Word keep only a cut-down copy of the font inside, which can't write new
    text). Returns (alias, Font) or None to fall back to Helvetica.
    """
    name = span["font"]
    if name not in cache:
        cache[name] = None
        path = _system_fonts().get(_norm_font(name))
        if path:
            try:
                alias = "argus" + re.sub(r"[^a-z0-9]", "", name.lower())[:20]
                page.insert_font(fontname=alias, fontfile=path)
                cache[name] = (alias, fitz.Font(fontfile=path))
            except Exception:
                pass
    return cache[name]


def _builtin_font(span) -> str:
    bold = "bold" in span["font"].lower() or span["flags"] & 16
    italic = "italic" in span["font"].lower() or "oblique" in span["font"].lower() or span["flags"] & 2
    return {(False, False): "helv", (True, False): "hebo",
            (False, True): "heit", (True, True): "hebi"}[(bool(bold), bool(italic))]


_BULLETS = set("\u2022\u00b7\u25aa\u25cf\u25e6\u2023-\u2013*>\uf0b7\uf0a7")


def _fitz():
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    return fitz


class _Pen:
    """Measures and writes text in one span's style, in the PDF's own font if possible."""

    def __init__(self, page, doc, span, cache, fitz, scale=1.0):
        self.fitz, self.size = fitz, span["size"] * scale
        c = span["color"]
        self.color = ((c >> 16) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255)
        self.own = _pdf_font_for(page, doc, span, cache, fitz)
        self.builtin = _builtin_font(span)

    def _own_ok(self, t):
        return self.own and all(self.own[1].has_glyph(ord(ch)) for ch in t if not ch.isspace())

    def width(self, t):
        if self._own_ok(t):
            return self.own[1].text_length(t, fontsize=self.size)
        return self.fitz.get_text_length(t, fontname=self.builtin, fontsize=self.size)

    def write(self, page, x, y, t):
        name = self.own[0] if self._own_ok(t) else self.builtin
        page.insert_text((x, y), t, fontname=name, fontsize=self.size, color=self.color)


def _split_paragraphs(block):
    """
    A PDF text "block" can hold several paragraphs or list items (Word often
    groups a whole bullet list into one block). Split it so each is reflowed
    on its own. Returns lists of (line_index, line).
    """
    raw = [(i, l) for i, l in enumerate(block.get("lines", [])) if l["spans"]]
    if not raw:
        return []

    # Word/LibreOffice often store a bullet as its own tiny "line" next to the
    # text. Glue such a bullet onto the text line beside it.
    def is_bullet_only(l):
        t = "".join(sp["text"] for sp in l["spans"]).strip()
        return bool(t) and all(ch in _BULLETS for ch in t)

    lines, k = [], 0
    while k < len(raw):
        li, l = raw[k]
        if is_bullet_only(l) and k + 1 < len(raw):
            li2, l2 = raw[k + 1]
            mid1 = (l["bbox"][1] + l["bbox"][3]) / 2
            if l2["bbox"][1] <= mid1 <= l2["bbox"][3]:
                merged = dict(l2)
                merged["spans"] = list(l["spans"]) + list(l2["spans"])
                merged["bbox"] = (min(l["bbox"][0], l2["bbox"][0]), min(l["bbox"][1], l2["bbox"][1]),
                                  max(l["bbox"][2], l2["bbox"][2]), max(l["bbox"][3], l2["bbox"][3]))
                lines.append(([li, li2], merged))
                k += 2
                continue
        lines.append(([li], l))
        k += 1
    widest = max(l["bbox"][2] - l["bbox"][0] for _, l in lines)
    groups, cur = [], []
    for i, (li, line) in enumerate(lines):
        starts_bullet = line["spans"][0]["text"].strip() and \
            all(ch in _BULLETS for ch in line["spans"][0]["text"].strip())
        if cur:
            prev = cur[-1][1]
            prev_h = prev["bbox"][3] - prev["bbox"][1]
            gap = line["bbox"][1] - prev["bbox"][3]
            short_prev = (prev["bbox"][2] - prev["bbox"][0]) < 0.6 * widest
            size_change = abs(line["spans"][0]["size"] - prev["spans"][-1]["size"]) > 0.6
            if starts_bullet or gap > 0.6 * prev_h or short_prev or size_change:
                groups.append(cur)
                cur = []
        cur.append((li, line))
    if cur:
        groups.append(cur)
    return groups


def _block_edit_plan(block, find, replace):
    """
    Work out the edited text of one paragraph (PDF text block), keeping which
    span style (font/size/colour) every piece of text had. Returns None if
    `find` isn't in this block.
    """
    lines = [l for l in block.get("lines", []) if l["spans"]]
    if not lines:
        return None
    # Match across line breaks and odd spacing: "Data Quality\nAssurance" still matches.
    pattern = r"\s+".join(re.escape(w) for w in find.split())
    full = " ".join("".join(sp["text"] for sp in l["spans"]) for l in lines)
    if not re.search(pattern, full, re.IGNORECASE):
        return None

    first = lines[0]["spans"]
    bullet = first[0] if (len(first) > 1 and first[0]["text"].strip()
                          and all(ch in _BULLETS for ch in first[0]["text"].strip())) else None
    runs = []
    for i, line in enumerate(lines):
        spans = line["spans"][1:] if (i == 0 and bullet) else line["spans"]
        for sp in spans:
            runs.append([sp["text"], sp])
        if i < len(lines) - 1 and runs:
            runs.append([" ", runs[-1][1]])

    holder = type("Para", (), {})()
    holder.runs = [type("Run", (), {"text": t})() for t, _ in runs]
    n = _sub_in_runs(holder, pattern, replace)
    if not n:
        return None
    for rule, repl in _TIDY_RULES:
        _sub_in_runs(holder, rule, repl)
    new_runs = [(r.text, sp) for r, (_, sp) in zip(holder.runs, runs)]

    # Split into words; a word may mix styles (e.g. half bold), so keep pieces.
    words, cur = [], []
    for text, sp in new_runs:
        for part in re.split(r"(\s+)", text):
            if not part:
                continue
            if part.isspace():
                if cur:
                    words.append(cur)
                    cur = []
            else:
                cur.append((part, sp))
    if cur:
        words.append(cur)

    ys = [l["spans"][0]["origin"][1] for l in lines]
    x_first = first[1]["origin"][0] if bullet else first[0]["origin"][0]
    x_rest = lines[1]["spans"][0]["origin"][0] if len(lines) > 1 else x_first
    right = max(l["bbox"][2] for l in lines)
    line_h = (ys[-1] - ys[0]) / (len(ys) - 1) if len(ys) > 1 else first[-1]["size"] * 1.2
    body = lines[:-1]
    justified = len(lines) >= 3 and sum(l["bbox"][2] >= right - 1.5 for l in body) >= 0.8 * len(body)

    empty = not words                     # the whole paragraph / bullet was removed
    redact = []
    for i, l in enumerate(lines):
        r = list(l["bbox"])
        if i == 0 and bullet and not empty:
            r[0] = x_first - 0.5          # keep the bullet itself
        redact.append(r)

    return {"count": n, "words": words, "lines": lines, "ys": ys, "x_first": x_first,
            "x_rest": x_rest, "right": right, "line_h": line_h, "justified": justified,
            "redact": redact, "descent": lines[-1]["bbox"][3] - ys[-1],
            "old_bottom": max(l["bbox"][3] for l in lines),
            "top": min(l["bbox"][1] for l in lines), "empty": empty}


def _layout(plan, pen_for, scale):
    """Word-wrap the paragraph to its original width, like Word would."""
    out, line, line_w = [], [], 0.0
    for w in plan["words"]:
        ww = sum(pen_for(sp, scale).width(t) for t, sp in w)
        sw = pen_for(w[0][1], scale).width(" ")
        limit = plan["right"] - (plan["x_first"] if not out else plan["x_rest"])
        if line and line_w + sw + ww > limit + 0.5:
            out.append((line, line_w))
            line, line_w = [w], ww
        else:
            line_w += (sw if line else 0) + ww
            line.append(w)
    if line:
        out.append((line, line_w))
    return out


def _page_is_single_column_near(page_text_lines, skip_ids, y0, y1):
    """True if no OTHER text sits beside the band y0..y1 (e.g. a second column)."""
    for (bid, lid), bbox in page_text_lines:
        if (bid, lid) in skip_ids:
            continue
        if bbox[1] < y1 - 1.5 and bbox[3] > y0 + 1.5:     # small overlaps are normal
            return False
    return True


def _rebuild_with_shifts(out, work, pno, cuts, fitz):
    """
    Copy page `pno` into `out`, closing each gap (cut_y, gap) by moving
    everything below it up. Text stays real text; links are moved too.
    """
    page = work[pno]
    W, H = page.rect.width, page.rect.height
    links = page.get_links()
    newp = out.new_page(width=W, height=H)
    y_src = y_dst = 0.0
    for cut, gap in sorted(cuts):
        if cut > y_src:
            newp.show_pdf_page(fitz.Rect(0, y_dst, W, y_dst + cut - y_src), work, pno,
                               clip=fitz.Rect(0, y_src, W, cut))
            y_dst += cut - y_src
        y_src = cut + gap
    if H > y_src:
        newp.show_pdf_page(fitz.Rect(0, y_dst, W, y_dst + H - y_src), work, pno,
                           clip=fitz.Rect(0, y_src, W, H))

    def moved(y):
        for cut, gap in sorted(cuts, reverse=True):
            if y >= cut + gap:
                y -= gap
            elif y > cut:
                y = cut
        return y
    for link in links:
        r = link["from"]
        link = {k: v for k, v in link.items() if k not in ("xref", "id")}
        link["from"] = fitz.Rect(r.x0, moved(r.y0), r.x1, moved(r.y1))
        try:
            newp.insert_link(link)
        except Exception:
            pass


def _edit_pdf(src: Path, dst: Path, find: str, replace: str) -> int:
    """
    Edit text in a PDF so that nobody can tell it was edited:
      1. every paragraph containing `find` is rewritten and word-wrapped again
         to its original width (words from the next line move up), in the same
         font, size and colour, keeping bold parts bold and bullets in place;
      2. if the paragraph got shorter, everything below it moves up to close
         the gap (only on single-column parts of the page, to be safe);
      3. a longer replacement is fitted into the same number of lines by
         shrinking the font slightly (max ~10%), otherwise it is refused.
    """
    fitz = _fitz()
    work = fitz.open(str(src))
    changes, page_cuts = 0, {}

    for pno, page in enumerate(work):
        data = page.get_text("dict")
        blocks = [b for b in data["blocks"] if b.get("type") == 0]
        text_lines = [((bi, li), l["bbox"]) for bi, b in enumerate(blocks)
                      for li, l in enumerate(b.get("lines", []))]
        plans = []
        for bi, block in enumerate(blocks):
            for group in _split_paragraphs(block):
                plan = _block_edit_plan({"lines": [l for _, l in group]}, find, replace)
                if plan:
                    plan["ids"] = {(bi, li) for lis, _ in group for li in lis}
                    plans.append(plan)
        if not plans:
            continue

        for plan in plans:
            for r in plan["redact"]:
                page.add_redact_annot(fitz.Rect(r), fill=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_NONE)

        cache, pens = {}, {}

        def pen_for(sp, scale):
            key = (sp["font"], sp["size"], sp["color"], sp["flags"], scale)
            if key not in pens:
                pens[key] = _Pen(page, work, sp, cache, fitz, scale)
            return pens[key]

        cuts = []
        for plan in plans:
            old_n = len(plan["lines"])
            for scale in (1.0, 0.97, 0.94, 0.91):
                laid = _layout(plan, pen_for, scale)
                if len(laid) <= old_n:
                    break
            else:
                work.close()
                raise ValueError("The new text is too long to fit in that spot. "
                                 "Try a shorter replacement.")
            limit_last = None
            for i, (words, natural_w) in enumerate(laid):
                y = plan["ys"][0] + i * plan["line_h"]
                x = plan["x_first"] if i == 0 else plan["x_rest"]
                limit = plan["right"] - x
                extra = 0.0
                if plan["justified"] and i < len(laid) - 1 and len(words) > 1:
                    extra = max(0.0, (limit - natural_w) / (len(words) - 1))
                for wi, w in enumerate(words):
                    if wi:
                        x += pen_for(w[0][1], scale).width(" ") + extra
                    for t, sp in w:
                        pen = pen_for(sp, scale)
                        pen.write(page, x, y, t)
                        x += pen.width(t)
            changes += plan["count"]

            lost = old_n - len(laid)
            if plan["empty"]:
                # Whole paragraph gone: remove its height plus the spacing above it,
                # so the text below keeps the spacing it had (e.g. before a heading).
                above = [bb[3] for key, bb in text_lines
                         if key not in plan["ids"] and bb[3] <= plan["top"] + 1.5]
                if above:
                    prev_bottom = max(above)
                    cut, gap = prev_bottom + 0.5, plan["old_bottom"] - prev_bottom
                else:
                    below = [bb[1] for key, bb in text_lines
                             if key not in plan["ids"] and bb[1] >= plan["old_bottom"] - 0.5]
                    nxt = min(below) if below else plan["old_bottom"]
                    cut, gap = plan["top"] - 0.5, nxt - plan["top"]
                if gap > 0 and _page_is_single_column_near(text_lines, plan["ids"], cut, cut + gap):
                    cuts.append((cut, gap))
            elif lost > 0:
                new_bottom = plan["ys"][0] + (len(laid) - 1) * plan["line_h"] + plan["descent"]
                cut, gap = new_bottom + 0.5, lost * plan["line_h"]
                if _page_is_single_column_near(text_lines, plan["ids"], cut, cut + gap):
                    cuts.append((cut, gap))
        if cuts:
            page_cuts[pno] = cuts

    if not changes:
        work.close()
        return 0

    out = fitz.open()
    for pno in range(len(work)):
        if pno in page_cuts:
            _rebuild_with_shifts(out, work, pno, page_cuts[pno], fitz)
        else:
            out.insert_pdf(work, from_page=pno, to_page=pno)
    out.set_metadata(work.metadata or {})
    try:
        out.subset_fonts()                        # keep only the letters used: small file
    except Exception:
        pass
    out.save(str(dst), garbage=3, deflate=True)
    out.close()
    work.close()
    return changes


@mcp.tool()
def edit_file(path: str, find: str, replace: str = "", save_as_copy: bool = False) -> dict[str, Any]:
    """
    Edit a vault file: replace every `find` (not case-sensitive) with
    `replace`; use an empty replace to remove text. Leftover separators like
    "|" or "," are cleaned up. Works for .txt, .md, .csv, .docx, .xlsx (text
    cells) and .pdf (lines are rewritten in the same font; the design stays).

    By default the edited file REPLACES the original under the same name, and
    the old version is moved to the vault trash (restorable). Set
    save_as_copy=true only if the user wants to keep both versions.

    Args:
        path: the file path exactly as shown by list_vault_files.
        find: the exact text to change, as it appears in the file.
        replace: the new text ("" to delete it).
        save_as_copy: keep the original and save the edit as a new file.
    """
    try:
        src = _safe_path(path)
        if not src.is_file():
            return {"error": f"File not found in vault: {path}"}
        suffix = src.suffix.lower()
        if suffix not in EDITABLE:
            return {"error": f"{suffix} files can't be edited safely. "
                             "Save the file as Word (.docx), PDF or text and try again."}
        if not find.strip():
            return {"error": "Nothing to find: 'find' is empty."}

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if save_as_copy:
            EDITS_DIR.mkdir(parents=True, exist_ok=True)
            dst = EDITS_DIR / f"{src.stem}_edited_{stamp}{suffix}"
        else:
            dst = src.with_name(f".argus_tmp_{stamp}{suffix}")   # finished below

        if suffix == ".docx":
            changes = _edit_docx(src, dst, find, replace)
        elif suffix == ".xlsx":
            changes = _edit_xlsx(src, dst, find, replace)
        elif suffix == ".pdf":
            changes = _edit_pdf(src, dst, find, replace)
        else:
            text = src.read_text(encoding="utf-8", errors="replace")
            new_text, changes = _replace_ci(text, find, replace)
            if changes:
                new_text = "\n".join(_tidy(l) if ("," in l or "|" in l) else l
                                     for l in new_text.splitlines())
                dst.write_text(new_text, encoding="utf-8")
    except Exception as e:
        return {"error": str(e), "path": path}

    if not changes:
        return {"error": f"'{find}' was not found in {path}. "
                         "Read the file and use the exact wording."}

    if save_as_copy:
        _log_access("edit_file(copy)", f"{path} -> {_rel(dst)}")
        return {"saved_as": _rel(dst), "changes": changes, "original_untouched": True}

    # Replace the original: old version to the trash first, then swap in the edit.
    try:
        trash_name = _move_to_trash(src)
        dst.rename(src)
    except Exception as e:
        return {"error": f"Edit made but could not replace the original: {e}"}
    _log_access("edit_file", path)
    return {
        "edited": path,
        "changes": changes,
        "previous_version_in_trash": trash_name,
        "undo": f"restore_file('{trash_name}') brings the old version back",
    }

# ------------------------------------------------------------------ file management
def _free_name(target: Path) -> Path:
    """Never overwrite: 'cv.docx' -> 'cv (2).docx' if the name is taken."""
    if not target.exists():
        return target
    n = 2
    while True:
        candidate = target.with_name(f"{target.stem} ({n}){target.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _destination(src: Path, dest: str) -> Path:
    """dest may be a folder (keep the name) or a full new path."""
    target = _safe_path(dest)
    if target.is_dir():
        target = target / src.name
    if target == VAULT_DIR:
        raise ValueError("Invalid destination.")
    target.parent.mkdir(parents=True, exist_ok=True)
    return _free_name(target)


def _rel(p: Path) -> str:
    return str(p.relative_to(VAULT_DIR))


@mcp.tool()
def copy_file(path: str, destination: str) -> dict[str, Any]:
    """
    Copy a file or folder inside the vault. Never overwrites: if the name is
    taken, a number is added, e.g. "cv (2).docx".

    Args:
        path: what to copy, as shown by list_vault_files.
        destination: a folder in the vault, or a new file path/name.
    """
    try:
        src = _safe_path(path)
        if not src.exists() or src == VAULT_DIR:
            return {"error": f"Not found in vault: {path}"}
        dst = _destination(src, destination)
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    except Exception as e:
        return {"error": str(e)}
    _log_access("copy_file", f"{path} -> {_rel(dst)}")
    return {"copied_to": _rel(dst)}


@mcp.tool()
def move_file(path: str, destination: str) -> dict[str, Any]:
    """
    Move or rename a file or folder inside the vault. Never overwrites.

    Args:
        path: what to move, as shown by list_vault_files.
        destination: a folder in the vault, or the new path/name
            (to rename, give the new name, e.g. "cv_2026.docx").
    """
    try:
        src = _safe_path(path)
        if not src.exists() or src == VAULT_DIR:
            return {"error": f"Not found in vault: {path}"}
        dst = _destination(src, destination)
        if src.is_dir() and (dst == src or src in dst.parents):
            return {"error": "Cannot move a folder into itself."}
        shutil.move(str(src), str(dst))
    except Exception as e:
        return {"error": str(e)}
    _log_access("move_file", f"{path} -> {_rel(dst)}")
    return {"moved_to": _rel(dst), "undo": f"move_file('{_rel(dst)}', '{path}')"}


@mcp.tool()
def create_folder(path: str) -> dict[str, Any]:
    """Create a new folder inside the vault (e.g. "job_applications/2026")."""
    try:
        folder = _safe_path(path)
        if folder == VAULT_DIR:
            return {"error": "Give a folder name."}
        folder.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return {"error": str(e)}
    _log_access("create_folder", path)
    return {"created": _rel(folder)}


def _read_trash_index() -> dict:
    try:
        return json.loads(TRASH_INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _move_to_trash(src: Path) -> str:
    """Move a vault item into the trash and remember where it came from."""
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    trash_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{src.name}"
    shutil.move(str(src), str(TRASH_DIR / trash_name))
    index = _read_trash_index()
    index[trash_name] = {"original": _rel(src), "deleted": datetime.now().isoformat(timespec="seconds")}
    TRASH_INDEX.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    return trash_name


@mcp.tool()
def delete_file(path: str) -> dict[str, Any]:
    """
    Delete a file or folder by moving it to the vault trash. It is NOT
    permanently deleted and can be brought back with restore_file.
    """
    try:
        src = _safe_path(path)
        if not src.exists() or src == VAULT_DIR:
            return {"error": f"Not found in vault: {path}"}
        trash_name = _move_to_trash(src)
    except Exception as e:
        return {"error": str(e)}
    _log_access("delete_file", path)
    return {"moved_to_trash": trash_name, "restorable": True}


@mcp.tool()
def list_trash() -> dict[str, Any]:
    """List items in the vault trash that can be restored."""
    index = _read_trash_index()
    items = [{"trash_name": k, **v} for k, v in index.items() if (TRASH_DIR / k).exists()]
    return {"items": items, "count": len(items)}


@mcp.tool()
def restore_file(trash_name: str, replace_current: bool = False) -> dict[str, Any]:
    """
    Bring an item back from the trash to where it was.

    Args:
        trash_name: the name shown by list_trash (or returned by edit_file).
        replace_current: use true to UNDO an edit. The current file at that
            spot goes to the trash and the old version takes its exact name.
            With false (default) nothing is overwritten; a number is added.
    """
    index = _read_trash_index()
    entry = index.get(trash_name)
    item = TRASH_DIR / trash_name
    if not entry or not item.exists() or TRASH_DIR not in item.resolve().parents:
        return {"error": f"Not in trash: {trash_name}"}
    try:
        target = _safe_path(entry["original"])
        swapped = None
        if target.exists():
            if replace_current:
                swapped = _move_to_trash(target)          # the edit is kept, just in the trash
                index = _read_trash_index()
            else:
                target = _free_name(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(item), str(target))
        index.pop(trash_name, None)
        TRASH_INDEX.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        return {"error": str(e)}
    _log_access("restore_file", f"{trash_name} -> {_rel(target)}")
    result = {"restored_to": _rel(target)}
    if swapped:
        result["replaced_version_in_trash"] = swapped
    return result


# ------------------------------------------------------------------ CV generation
# Some models write special Unicode dashes/spaces that the CV's PDF font has
# no glyph for, rendering as a solid black box. Clean every piece of CV text
# before it reaches the PDF (this mirrors argus_brain.clean_answer, kept
# duplicated here since this script runs as its own separate process).
_ODD_SPACES = re.compile(r"[\u00a0\u2000-\u200a\u202f\u205f\u3000]")
_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
_ODD_DASHES = re.compile(r"[\u2010\u2011\u2012\u2015]")
_MODEL_CITES = re.compile(r"\s*【[^】]*】")


def _clean(text) -> str:
    if not isinstance(text, str):
        return text
    text = _MODEL_CITES.sub("", text)
    text = _ODD_SPACES.sub(" ", text)
    text = _ZERO_WIDTH.sub("", text)
    text = _ODD_DASHES.sub("-", text)
    # Em/en dashes read as an "AI writing tell" on a CV -- humans typically
    # just type a plain hyphen, so normalise all dash variants to "-".
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    # reportlab's Paragraph reads text as a tiny XML dialect, where a literal
    # "&" (e.g. "S&P", "R&D") is parsed as the start of an XML entity and
    # corrupts the output (seen as a stray ";" appearing mid-word). Escaping
    # &/</> here makes them render as the plain literal characters they are.
    return xml_escape(text)


def _clean_cv_inputs(full_name, contact_line, summary, skills, experience, education, certifications):
    full_name, contact_line, summary = _clean(full_name), _clean(contact_line), _clean(summary)
    skills = [_clean(s) for s in (skills or [])]
    experience = [
        {**job, "title": _clean(job.get("title", "")), "company": _clean(job.get("company", "")),
         "dates": _clean(job.get("dates", "")), "bullets": [_clean(b) for b in (job.get("bullets") or [])]}
        for job in (experience or [])
    ]
    education = [
        {**e, "degree": _clean(e.get("degree", "")), "school": _clean(e.get("school", "")),
         "dates": _clean(e.get("dates", ""))}
        for e in (education or [])
    ]
    certifications = [_clean(c) for c in (certifications or [])] if certifications else certifications
    return full_name, contact_line, summary, skills, experience, education, certifications
_ACCENT = (0x1F / 255, 0x3A / 255, 0x5F / 255)   # a calm professional navy
_GRAY = (0.35, 0.35, 0.35)


def _cv_styles():
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_JUSTIFY

    return {
        "name": ParagraphStyle("name", fontName="Helvetica-Bold", fontSize=20,
                               leading=24, textColor=_ACCENT, spaceAfter=1),
        "headline": ParagraphStyle("headline", fontName="Helvetica", fontSize=11.5,
                                   leading=15, textColor=_GRAY, spaceAfter=4),
        "contact": ParagraphStyle("contact", fontName="Helvetica", fontSize=9.5,
                                  leading=13, textColor=_GRAY, spaceAfter=10),
        "heading": ParagraphStyle("heading", fontName="Helvetica-Bold", fontSize=11,
                                  textColor=_ACCENT, spaceBefore=8, spaceAfter=2),
        "body": ParagraphStyle("body", fontName="Helvetica", fontSize=10,
                               leading=13.2, alignment=TA_JUSTIFY),
        "job_title": ParagraphStyle("job_title", fontName="Helvetica-Bold", fontSize=10.5,
                                    leading=14),
        "job_meta": ParagraphStyle("job_meta", fontName="Helvetica-Oblique", fontSize=9.5,
                                   textColor=_GRAY, spaceAfter=3),
        "bullet": ParagraphStyle("bullet", fontName="Helvetica", fontSize=10,
                                 leading=12.6, leftIndent=14, bulletIndent=2),
    }


def _hr(width, weight=1, color=None, space_before=0, space_after=0):
    from reportlab.platypus import Flowable

    class Rule(Flowable):
        def __init__(self):
            super().__init__()
            self.spaceBefore, self.spaceAfter = space_before, space_after

        def draw(self):
            self.canv.setStrokeColorRGB(*(color or _ACCENT))
            self.canv.setLineWidth(weight)
            self.canv.line(0, 0, width, 0)

    return Rule()


_TOOL_NAME = re.compile(r"[A-Za-z][\w+#./-]*(?: [A-Z][\w+#./-]*)?")   # "n8n", "Docker", "Power BI"


def _place_learning(summary: str, skills: list, learning: list | None) -> tuple[str, list]:
    """
    Show what the job requires and the CV lacks INSIDE the CV, marked as learning and
    never as experience: a tool sits beside related skills ("Salesforce (currently
    learning)"), a knowledge area becomes one Profile sentence. Never a separate block
    such as "Currently strengthening", which makes a CV look machine-written.
    """
    tools, domain, seen = [], [], set()
    skills = list(skills or [])
    for raw in (learning or []):
        item = str(raw).strip(" .")
        key = item.lower()
        if not item or key in seen:
            continue
        seen.add(key)
        if key in (summary or "").lower() or any(key in str(x).lower() for x in skills):
            continue                                       # already mentioned in place
        (tools if _TOOL_NAME.fullmatch(item) else domain).append(item)
    # spread them between the real skills (after every 2nd or 3rd one), never as one stack at the end
    if tools:
        step = max(2, (len(skills) + len(tools)) // (len(tools) + 1))
        out, queue = [], [f"{t} (currently learning)" for t in tools]
        for i, sk in enumerate(skills, 1):
            out.append(sk)
            if queue and i % step == 0:
                out.append(queue.pop(0))
        skills = out + queue
    if domain:
        names = [re.sub(r"\s+experience$", "", d, flags=re.I) for d in domain]
        joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        summary = ((summary.rstrip() + " ") if summary else "") + \
            f"Currently building familiarity with {joined}."
    return summary, skills


def create_cv(filename: str, full_name: str, contact_line: str, summary: str,
             skills: list, experience: list, education: list,
             certifications: list | None = None, headline: str = "",
             projects: list | None = None, additional: list | None = None) -> dict:
    """
    Build a new, clean, single-column, ATS-friendly CV as a PDF.

    experience: list of {"title", "company", "dates", "bullets": [str, ...]}
    education: list of {"degree", "school", "dates"}
    headline: optional role line under the name, e.g. "Data Analyst".
    projects: optional list of {"name", "dates", "bullets": [str, ...]}
    additional: optional list of short lines, e.g. "Languages: English, Urdu".
    """
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    ListFlowable, ListItem, KeepTogether)

    full_name, contact_line, summary, skills, experience, education, certifications = \
        _clean_cv_inputs(full_name, contact_line, summary, skills, experience, education, certifications)
    headline = _clean(headline)
    clean_projects = []
    for proj in (projects or []):
        if isinstance(proj, str):
            proj = {"name": proj}
        if not isinstance(proj, dict) or not str(proj.get("name", "")).strip():
            continue
        clean_projects.append({
            "name": _clean(str(proj.get("name", ""))), "dates": _clean(str(proj.get("dates", "") or "")),
            "bullets": [_clean(str(b)) for b in (proj.get("bullets") or [])],
        })
    additional = [_clean(a) for a in (additional or []) if isinstance(a, str) and a.strip()]

    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    dst = _free_name(_safe_path(filename))
    dst.parent.mkdir(parents=True, exist_ok=True)
    st = _cv_styles()
    usable_width = LETTER[0] - 2 * 0.65 * inch

    story = [Paragraph(full_name, st["name"])]
    if headline.strip():
        story.append(Paragraph(headline, st["headline"]))
    story.append(Paragraph(contact_line, st["contact"]))
    story.append(_hr(usable_width, weight=1.4))

    def section(title, flow):
        head = [Paragraph(title.upper(), st["heading"]),
                _hr(usable_width, weight=0.5, color=(0.82, 0.85, 0.88), space_after=6)]
        story.append(KeepTogether(head + flow[:1]))          # never a heading alone at a page end
        story.extend(flow[1:])

    if summary.strip():
        section("Profile", [Paragraph(summary, st["body"])])

    if skills:
        section("Core Skills", [Paragraph(" | ".join(skills), st["body"])])

    if experience:
        rows = []
        for job in experience:
            head = f'{job["title"]} - {job["company"]}'
            rows.append(Paragraph(head, st["job_title"]))
            if job.get("dates"):
                rows.append(Paragraph(job["dates"], st["job_meta"]))
            bullets = job.get("bullets") or []
            if bullets:
                rows.append(ListFlowable(
                    [ListItem(Paragraph(b, st["bullet"]), leftIndent=14) for b in bullets],
                    bulletType="bullet", start="•", leftIndent=8, spaceBefore=2, spaceAfter=6,
                ))
            else:
                rows.append(Spacer(1, 6))
        section("Experience", rows)

    if clean_projects:
        rows = []
        for proj in clean_projects:
            rows.append(Paragraph(proj["name"], st["job_title"]))
            if proj["dates"]:
                rows.append(Paragraph(proj["dates"], st["job_meta"]))
            if proj["bullets"]:
                rows.append(ListFlowable(
                    [ListItem(Paragraph(b, st["bullet"]), leftIndent=14) for b in proj["bullets"]],
                    bulletType="bullet", start="\u2022", leftIndent=8, spaceBefore=2, spaceAfter=6,
                ))
            else:
                rows.append(Spacer(1, 6))
        section("Projects", rows)

    if education:
        rows = [Paragraph(f'{e["degree"]} - {e["school"]}'
                          + (f' ({e["dates"]})' if e.get("dates") else ""), st["body"])
               for e in education]
        section("Education", rows)

    if certifications:
        section("Certifications", [ListFlowable(
            [ListItem(Paragraph(c, st["bullet"])) for c in certifications],
            bulletType="bullet", start="•", leftIndent=8,
        )])

    if additional:
        section("Additional Information", [Paragraph(a, st["body"]) for a in additional])

    SimpleDocTemplate(
        str(dst), pagesize=LETTER,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
        leftMargin=0.65 * inch, rightMargin=0.65 * inch,
        title=full_name,
    ).build(story)

    return {"saved_as": _rel(dst)}


@mcp.tool()
def create_tailored_cv(filename: str, full_name: str, contact_line: str, summary: str,
                       skills: list, experience: list, education: list,
                       certifications: list | None = None, headline: str = "",
                       target_company: str = "", projects: list | None = None,
                       additional: list | None = None,
                       learning: list | None = None,
                       target_role: str = "") -> dict[str, Any]:
    """
    Create a NEW, professionally formatted CV as a PDF, tailored to a specific
    job. This does not touch the user's original CV -- it builds a fresh
    document from content you (the model) prepare, saved in
    vault/tailored_cvs/ (never overwrites an existing file).

    IMPORTANT: only reorganise, re-emphasise, and rephrase content that is
    ALREADY in the user's real CV (read it first with read_vault_file). Never
    invent a skill, job, or qualification the user doesn't actually have.
    Never write the target company's name anywhere in the CV body -- pass it
    as target_company below so this is checked in code, not just by you.

    Args:
        filename: e.g. "CompanyName_DataAnalyst.pdf" (a good, descriptive name).
        full_name: as it should appear on the CV.
        contact_line: e.g. "Lahore, Pakistan | you@email.com | +92 300 0000000".
        summary: 2-3 sentences, tailored to emphasise fit for THIS job. Must
            NOT contain target_company's name.
        skills: list of skill strings, most job-relevant ones first.
        experience: list of objects: {"title", "company", "dates",
            "bullets": [str, ...]}. Bullets should highlight achievements
            relevant to the target job, in the user's own real experience.
            "company" here is the person's OWN past employer, not the target.
        education: list of objects: {"degree", "school", "dates"}.
        certifications: optional list of strings.
        headline: optional role line under the name, e.g. "Data Analyst" or
            the exact job title being applied for. Must NOT contain
            target_company's name.
        target_company: the company this CV is being tailored for. NOT
            printed anywhere -- passed only so this tool can verify the
            company name doesn't leak into the CV body. Leave blank if the
            job posting didn't name a specific company.
        projects: optional list of {"name", "dates", "bullets": [str, ...]} for
            the user's real projects (e.g. a final year project). Include EVERY
            project that is in the original CV.
        additional: optional list of short lines from the original CV such as
            "Languages: English, Urdu (Native)" or "Activities: ...".
        learning: what the job requires but the person's CV does NOT show (a tool,
            industry knowledge, a kind of experience): 1-4 short noun phrases, e.g.
            "Salesforce", "customer service experience", "TMT industry knowledge". This
            tool places each one inside the CV as "currently learning". Never write
            such items in skills, summary or bullets yourself, and never write a
            "Currently strengthening" line.
        target_role: the role the posting hires for, in 2-5 words, without level words
            (Jr, Senior, Lead), places, job numbers or the company name, e.g.
            "AI Platform Engineer", "Client Operations", "Commercial Contract Support".
            The headline becomes "<the person's real title> | <target_role> Enthusiast".
    """
    if target_company.strip():
        haystack = " ".join([
            full_name, headline, summary,
            *skills, *(certifications or []),
            *(f'{j.get("title", "")} {j.get("dates", "")} {" ".join(j.get("bullets") or [])}'
              for j in experience),
            *(f'{e.get("degree", "")} {e.get("dates", "")}' for e in education),
            *(f'{pj.get("name", "")} {" ".join(str(b) for b in (pj.get("bullets") or []))}'
              for pj in (projects or []) if isinstance(pj, dict)),
            *(a for a in (additional or []) if isinstance(a, str)),
            *(str(x) for x in (learning or [])),
            target_role or "",
        ]).lower()
        if target_company.strip().lower() in haystack:
            return {"error": f"The CV body still mentions '{target_company}'. Rewrite the "
                             "Profile, Skills and Experience bullets so the company name "
                             "doesn't appear anywhere in the CV, then call this again."}
    if not (headline or "").strip() and (target_role or "").strip():
        headline = f"{target_role.strip()} Enthusiast"      # the app normally builds it from the CV's own title
    try:
        summary, skills = _place_learning(summary, skills, learning)
        result = create_cv(f"tailored_cvs/{filename}", full_name, contact_line, summary,
                           skills, experience, education, certifications, headline,
                           projects, additional)
    except Exception as e:
        return {"error": str(e)}
    _log_access("create_tailored_cv", result["saved_as"])
    return result


def _clean_letter_inputs(full_name, contact_line, company_name, role_title, salutation,
                         paragraphs, closing):
    full_name, contact_line = _clean(full_name), _clean(contact_line)
    company_name, role_title = _clean(company_name), _clean(role_title)
    salutation, closing = _clean(salutation), _clean(closing)
    paragraphs = [_clean(p) for p in (paragraphs or []) if isinstance(p, str) and p.strip()]
    return full_name, contact_line, company_name, role_title, salutation, paragraphs, closing


@mcp.tool()
def create_cover_letter(filename: str, full_name: str, contact_line: str, company_name: str,
                        role_title: str, paragraphs: list, salutation: str = "Dear Hiring Manager,",
                        closing: str = "Sincerely,", date_line: str = "") -> dict[str, Any]:
    """
    Build a one-page cover letter as a PDF, in the same visual style as create_tailored_cv.

    Unlike a CV, the company name and role BELONG in a cover letter -- do not omit them.

    Args:
        filename: e.g. "Cover_Letter_Company_Name.pdf" (saved under vault\\tailored_cvs).
        full_name: the applicant's name, from their CV.
        contact_line: e.g. "Lahore, Pakistan | name@email.com | +92 300 0000000".
        company_name: the company the letter is addressed to.
        role_title: the exact job title being applied for.
        paragraphs: 3-4 short paragraphs, in order: (1) opening, naming the role and company
            and one honest hook; (2) the strongest real match between the CV and the posting;
            (3) one more real match, or a real project, plus at most one short honest mention
            of what the person is still learning; (4) closing, enthusiasm and next step. Every
            claim must come from the person's real CV/profile; never invent duties or tools.
        salutation: default "Dear Hiring Manager,"; use a named person only if the user gave one.
        closing: default "Sincerely,".
        date_line: optional, e.g. "September 29, 2026"; left blank if not given.
    """
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

    full_name, contact_line, company_name, role_title, salutation, paragraphs, closing = \
        _clean_letter_inputs(full_name, contact_line, company_name, role_title, salutation,
                             paragraphs, closing)
    date_line = _clean(date_line)
    if not paragraphs:
        return {"error": "paragraphs cannot be empty."}
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    # Same folder as create_tailored_cv, so a cover letter is discoverable as an email attachment.
    dst = _free_name(_safe_path(f"tailored_cvs/{filename}"))
    dst.parent.mkdir(parents=True, exist_ok=True)
    st = _cv_styles()
    usable_width = LETTER[0] - 2 * 0.75 * inch

    story = [Paragraph(full_name, st["name"]), Paragraph(contact_line, st["contact"]),
             _hr(usable_width, weight=1.4, space_after=10)]
    if date_line:
        story += [Paragraph(date_line, st["body"]), Spacer(1, 10)]
    if company_name:
        story += [Paragraph(f"Re: Application for {role_title}" if role_title else "Re: Application",
                            st["job_title"]),
                   Paragraph(company_name, st["body"]), Spacer(1, 10)]
    story.append(Paragraph(salutation or "Dear Hiring Manager,", st["body"]))
    story.append(Spacer(1, 8))
    for para in paragraphs:
        story += [Paragraph(para, st["body"]), Spacer(1, 8)]
    story += [Spacer(1, 4), Paragraph(closing or "Sincerely,", st["body"]),
              Spacer(1, 18), Paragraph(full_name, st["body"])]

    SimpleDocTemplate(
        str(dst), pagesize=LETTER,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        title=f"Cover Letter - {full_name}",
    ).build(story)
    return {"saved_as": _rel(dst)}


if __name__ == "__main__":
    mcp.run(transport="stdio")

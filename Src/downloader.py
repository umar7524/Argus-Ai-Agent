"""
Argus - Downloader (web -> vault, only with the user's approval)

Flow:
  1. The Research agent finds a file and calls propose_download(url).
     This only INSPECTS the link (name, type, size) and queues a request.
  2. The app shows the request as a card. Nothing is downloaded yet.
  3. Only when the USER clicks "Download" does perform_download() fetch it.

Safety rules:
  - One-way valve: files flow web -> vault. The Research agent can't list or
    read the vault, so private data can never flow the other way.
  - Allowed types only (documents, data, images). Programs/scripts are refused,
    and a file that is secretly a Windows program is refused too.
  - Size limit, http(s) only, and no links to this computer or the home/office
    network (blocks "SSRF" tricks like http://localhost/... or 192.168.x.x).
  - Saved in vault\\downloads, never overwriting, with a log of where it came from.
"""

import hashlib
import ipaddress
import json
import os
import re
import socket
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VAULT_DIR = Path(os.environ.get("VAULT_DIR", PROJECT_ROOT / "vault")).resolve()
DOWNLOADS_DIR = VAULT_DIR / "downloads"
PENDING_FILE = PROJECT_ROOT / "data" / "pending_downloads.json"
DOWNLOAD_LOG = PROJECT_ROOT / "data" / "download_log.jsonl"

MAX_BYTES = 25 * 1024 * 1024            # 25 MB
TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (Argus research assistant)"

ALLOWED = {
    ".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".csv", ".txt", ".md", ".json",
    ".html", ".htm", ".rtf", ".jpg", ".jpeg", ".png", ".webp", ".gif",
}
TYPE_TO_EXT = {
    "application/pdf": ".pdf",
    "text/html": ".html",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/json": ".json",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-excel": ".xls",
}


# ------------------------------------------------------------------ checks
def _check_url(url: str) -> str:
    """Allow only public http(s) addresses. Returns the host name."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only http:// or https:// links can be downloaded.")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        raise ValueError(f"Can't find the website {parsed.hostname}.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("Blocked: that link points to this computer or a private network.")
    return parsed.hostname


def _name_from(url: str, headers, content_type: str) -> str:
    disposition = headers.get("Content-Disposition", "") if headers else ""
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition)
    name = unquote(match.group(1)) if match else unquote(Path(urlparse(url).path).name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "download"
    if Path(name).suffix.lower() not in ALLOWED:
        ext = TYPE_TO_EXT.get(content_type)
        if ext:
            name = f"{Path(name).stem or 'download'}{ext}"
    return name[:120]


def inspect(url: str) -> dict:
    """Look at a link without downloading the file."""
    site = _check_url(url)
    ext_in_url = Path(urlparse(url).path).suffix.lower()
    if ext_in_url and ext_in_url not in ALLOWED and ext_in_url not in (".php", ".aspx", ".asp", ".jsp"):
        raise ValueError(f"Blocked: {ext_in_url} files are not allowed in the vault.")

    headers, content_type, size = None, "", None
    for method in ("HEAD", "GET"):             # some servers refuse HEAD
        try:
            req = Request(url, method=method, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=TIMEOUT) as resp:
                headers = resp.headers
                content_type = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
                length = headers.get("Content-Length")
                size = int(length) if length and length.isdigit() else None
            break
        except Exception as e:
            if method == "GET":
                raise ValueError(f"Could not open the link: {e}")

    name = _name_from(url, headers, content_type)
    if Path(name).suffix.lower() not in ALLOWED:
        raise ValueError(f"Blocked: '{name}' ({content_type or 'unknown type'}) is not an allowed file type.")
    if size is not None and size > MAX_BYTES:
        raise ValueError(f"Too big: {size / 1_048_576:.1f} MB (limit {MAX_BYTES // 1_048_576} MB).")
    return {"url": url, "site": site, "filename": name,
            "content_type": content_type, "size_bytes": size}


# ------------------------------------------------------------------ pending queue
def _read_pending() -> list[dict]:
    try:
        return json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _write_pending(items: list[dict]) -> None:
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")


def propose(url: str, reason: str = "") -> dict:
    """Inspect a link and queue it for the user's approval (no download)."""
    info = inspect(url)
    item = {"id": uuid.uuid4().hex[:10], "reason": reason[:200],
            "requested": datetime.now().isoformat(timespec="seconds"), **info}
    _write_pending(_read_pending() + [item])
    return item


def list_pending() -> list[dict]:
    return _read_pending()


def cancel(request_id: str) -> None:
    _write_pending([p for p in _read_pending() if p["id"] != request_id])


# ------------------------------------------------------------------ download (user-approved)
def _free_name(target: Path) -> Path:
    n = 2
    stem = target.stem
    while target.exists():
        target = target.with_name(f"{stem} ({n}){target.suffix}")
        n += 1
    return target


def perform_download(request_id: str) -> Path:
    """Called by the APP when the user clicks Download. Returns the saved path."""
    item = next((p for p in _read_pending() if p["id"] == request_id), None)
    if item is None:
        raise ValueError("This download request no longer exists.")
    _check_url(item["url"])                    # re-check at download time

    req = Request(item["url"], headers={"User-Agent": USER_AGENT})
    data = bytearray()
    with urlopen(req, timeout=TIMEOUT) as resp:
        while chunk := resp.read(65536):
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise ValueError(f"Stopped: the file is bigger than {MAX_BYTES // 1_048_576} MB.")

    if bytes(data[:2]) == b"MZ":               # Windows program in disguise
        raise ValueError("Blocked: this file is actually a program, not a document.")

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    target = _free_name(DOWNLOADS_DIR / item["filename"])
    target.write_bytes(bytes(data))

    DOWNLOAD_LOG.parent.mkdir(parents=True, exist_ok=True)
    with DOWNLOAD_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "time": datetime.now().isoformat(timespec="seconds"),
            "url": item["url"], "saved_as": str(target.relative_to(VAULT_DIR)),
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        }, ensure_ascii=False) + "\n")
    cancel(request_id)
    return target

"""Client folders and client separation (PRD: files, client separation).

One directory per client on a mounted volume; client separation is a folder
permission checked in code, never by an instruction to the model. A run is
created with one client scope that cannot change, and every file access,
retrieval and tool call is checked against it. A claim that would cross into
another client's folder is refused and audited.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from personal_agent.db import Database


@dataclass(frozen=True)
class ClientScope:
    """Immutable per-run client scope."""

    name: str


class CrossClientAccess(Exception):
    pass


class InvalidClientName(Exception):
    pass


def _safe_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned or len(cleaned) > 80:
        raise InvalidClientName("client name must be 1-80 characters")
    for bad in ("/", "\\", "..", "\x00"):
        if bad in cleaned:
            raise InvalidClientName(f"client name may not contain {bad!r}")
    return cleaned


def parse_document(data: bytes, filename: str) -> dict:
    """Extract text without a model. PDF, Word and plain text today; PowerPoint
    and images join with creative review."""
    lower = filename.lower()
    if lower.endswith((".txt", ".md")):
        return {"kind": "text", "text": data.decode("utf-8", errors="replace")}
    if lower.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
        return {"kind": "pdf", "pages": len(pages), "text": "\n\n".join(pages)}
    if lower.endswith(".docx"):
        from docx import Document as Docx

        doc = Docx(io.BytesIO(data))
        return {"kind": "docx", "text": "\n".join(p.text for p in doc.paragraphs)}
    raise ValueError(f"unsupported file type: {filename}")


class ClientStore:
    def __init__(self, root: Path, db: Database):
        self.root = root
        self.db = db
        root.mkdir(parents=True, exist_ok=True)

    def ensure(self, name: str) -> ClientScope:
        scope = ClientScope(_safe_name(name))
        (self.root / scope.name).mkdir(parents=True, exist_ok=True)
        return scope

    def scope(self, name: str) -> ClientScope:
        scope = ClientScope(_safe_name(name))
        if not (self.root / scope.name).is_dir():
            raise KeyError(f"unknown client {name!r}")
        return scope

    def list_clients(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def folder(self, scope: ClientScope) -> Path:
        return self.root / scope.name

    # --- files -----------------------------------------------------------------
    def store_file(self, scope: ClientScope, filename: str, data: bytes) -> str:
        safe = _safe_name(filename)
        target = self._within_scope(scope, safe)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.db.audit(None, "file_intake", {
            "client": scope.name, "file": safe, "bytes": len(data),
        })
        return safe

    def read_bytes(self, scope: ClientScope, relpath: str) -> bytes:
        return self._within_scope(scope, relpath).read_bytes()

    def list_files(self, scope: ClientScope) -> list[dict]:
        folder = self.folder(scope)
        return sorted(
            (
                {"path": str(p.relative_to(folder)).replace("\\", "/"), "size": p.stat().st_size}
                for p in folder.rglob("*")
                if p.is_file()
            ),
            key=lambda f: f["path"],
        )

    def _within_scope(self, scope: ClientScope, relpath: str) -> Path:
        folder = self.folder(scope)
        candidate = (folder / relpath).resolve()
        if not candidate.is_relative_to(folder.resolve()):
            self.db.audit(None, "cross_client_refused", {
                "client": scope.name, "attempted": relpath,
                "note": "path escaped the client folder",
            })
            raise CrossClientAccess(f"path {relpath!r} is outside client {scope.name!r}")
        return candidate

    # --- retrieval (keyword now; vector index per folder comes with phase 2) ----
    def search(self, scope: ClientScope, query: str, max_results: int = 5) -> list[dict]:
        """A query never crosses folders: only this client's files are searched."""
        needle = (query or "").lower().strip()
        hits: list[dict] = []
        if not needle:
            return hits
        for entry in self.list_files(scope)[:200]:
            try:
                parsed = parse_document(self.read_bytes(scope, entry["path"]), entry["path"])
            except (ValueError, OSError):
                continue
            text = parsed.get("text", "")
            at = text.lower().find(needle)
            if at != -1:
                start = max(0, at - 120)
                hits.append({
                    "file": entry["path"],
                    "excerpt": text[start: at + len(needle) + 120].strip(),
                })
                if len(hits) >= max_results:
                    break
        return hits

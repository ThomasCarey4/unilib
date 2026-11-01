from __future__ import annotations

import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import pymupdf

from .bib import BibEntry, parse_bib, pdf_for
from .config import PAPERS, Module, PapersConfig, db_path, load_modules, load_papers, paper_rel
from .embed import Embedder
from .notebook import cell_count, sections
from .organize import parse_name, read_split_meta, read_split_skip, sha256
from .textnorm import fold

SCHEMA_VERSION = 4  # 4: docs.citekey/year/authors/reports for Zotero papers (module PAPERS)
# 3: FTS columns hold textnorm.fold(text); chunks.text stays original

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS docs(
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL DEFAULT 'pdf',
    module TEXT NOT NULL,
    type TEXT NOT NULL,
    num INTEGER,
    title TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    original_file TEXT,
    page_offset INTEGER NOT NULL DEFAULT 0,
    n_pages INTEGER NOT NULL,
    indexed_at TEXT NOT NULL,
    mtime REAL,
    size INTEGER,
    citekey TEXT,
    year INTEGER,
    authors TEXT,
    reports TEXT
);
CREATE TABLE IF NOT EXISTS chunks(
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES docs(id),
    page INTEGER NOT NULL,
    text TEXT NOT NULL,
    no_text INTEGER NOT NULL DEFAULT 0,
    embedding BLOB
);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, title, module UNINDEXED, tokenize='porter unicode61');
"""

# Content tables, dropped and recreated (inside the rebuild transaction) on a schema change.
_CONTENT_TABLES = ("chunks_fts", "chunks", "docs")

MIN_TAIL_WORDS = 50
DOC_SUFFIXES = (".pdf", ".ipynb")


@dataclass
class IndexStats:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    pages: int = 0
    errors: int = 0


@dataclass(frozen=True)
class DocStatus:
    path: str
    module: str
    type: str
    num: int | None
    title: str
    n_pages: int | None
    state: str  # "indexed" | "stale" | "new" | "deleted"
    kind: str = "pdf"  # "pdf" | "ipynb"


@dataclass(frozen=True)
class _PreparedDoc:
    kind: str
    type: str
    num: int | None
    title: str
    original_file: str | None
    page_offset: int
    n_pages: int
    rows: list[tuple[int, str, int, bytes | None]]  # (page, text, no_text, embedding)


@dataclass(frozen=True)
class PaperStatus:
    citekey: str
    title: str
    year: int | None
    n_pages: int | None
    reports: str
    state: str  # "indexed" | "stale" | "new" | "no pdf"


@dataclass(frozen=True)
class _PaperMeta:
    citekey: str
    year: int | None
    authors: str
    reports: str  # comma-joined report names

    def as_row(self) -> tuple:
        return (self.citekey, self.year, self.authors, self.reports)


# A heading line alone: "References", "7 REFERENCES", "Bibliography". Pages from there on are a reference list.
REFS_HEADING = re.compile(r"^\s*(?:\d+\.?\s*)?(?:References|REFERENCES|Bibliography|BIBLIOGRAPHY)\s*$", re.M)


def strip_references(texts: list[str]) -> list[str]:
    """Drop a paper's reference list (from the first heading after page 1) — those pages
    are dense with topical words and would otherwise match almost every query."""
    for i, text in enumerate(texts[1:], 1):
        if m := REFS_HEADING.search(text):
            return texts[:i] + [text[:m.start()]]
    return texts


def _paper_title(entry: BibEntry, pdf: Path) -> str:
    return entry.title or pdf.stem


def _paper_entries(papers: PapersConfig, log: Callable[[str], None]) -> tuple[list[BibEntry], dict[str, set[str]]] | None:
    """Entries from uni.bib ∪ report bibs (uni.bib wins per citekey) and citekey -> report names.
    None when uni.bib is missing (the caller keeps already-indexed papers)."""
    if not papers.bib.is_file():
        log(f"papers bib not found: {papers.bib} — papers skipped (already-indexed ones kept unless this is a full rebuild)")
        return None
    entries = {e.key: e for e in parse_bib(papers.bib)}
    tags: dict[str, set[str]] = defaultdict(set)
    for r in papers.reports():  # glob matches, so the files exist
        for e in parse_bib(r.bib):
            tags[e.key].add(r.name)
            entries.setdefault(e.key, e)
    return list(entries.values()), tags


def connect(root: Path, readonly: bool = False) -> sqlite3.Connection:
    """Open the index in autocommit mode; callers manage transactions explicitly."""
    p = db_path(root)
    if readonly:
        if not p.exists():
            raise FileNotFoundError(p)
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=10000")
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(p, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.executescript(SCHEMA)
        # Seeded from the clock so a deleted-and-rebuilt DB never reuses a generation
        # that a long-running reader has cached.
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('generation', ?)",
                     (str(time.time_ns() // 1000),))
    conn.row_factory = sqlite3.Row
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def _bump_generation(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'generation'")


def chunk_text(text: str, max_words: int = 300) -> list[str]:
    words = text.split()
    if not words:
        return []
    chunks = [words[i:i + max_words] for i in range(0, len(words), max_words)]
    if len(chunks) > 1 and len(chunks[-1]) < MIN_TAIL_WORDS:
        tail = chunks.pop()
        chunks[-1] = chunks[-1] + tail
    return [" ".join(c) for c in chunks]


def _kind(path: Path) -> str:
    return "ipynb" if path.suffix.lower() == ".ipynb" else "pdf"


def module_docs(root: Path, module: Module) -> list[Path]:
    """PDFs and notebooks directly in the module folder."""
    folder = root / module.folder
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in DOC_SUFFIXES and p.is_file())


module_pdfs = module_docs  # backwards-compatible alias


def _schema_statements() -> list[str]:
    return [s.strip() for s in SCHEMA.split(";") if s.strip()]


def _recreate_content_tables(conn: sqlite3.Connection) -> None:
    """Drop and recreate docs/chunks/chunks_fts so old DBs get new columns.
    Plain statements (not executescript) so this stays inside the caller's transaction."""
    for t in _CONTENT_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    for stmt in _schema_statements():
        conn.execute(stmt)


def _delete_doc(conn: sqlite3.Connection, doc_id: int) -> None:
    conn.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE doc_id=?)", (doc_id,))
    conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    conn.execute("DELETE FROM docs WHERE id=?", (doc_id,))


def _notebook_pages(path: Path) -> list[tuple[int, str, int]]:
    """One chunk per section (page = first cell id); oversized sections are split on the same page."""
    pages: list[tuple[int, str, int]] = []
    for sec in sections(path):
        if len(sec.text.split()) > 300:
            pages += [(sec.cell, chunk, 0) for chunk in chunk_text(sec.text)]
        else:
            pages.append((sec.cell, sec.text, 0))
    return pages


def _prepare_doc(embedder: Embedder, pdf: Path, entry: BibEntry | None = None) -> _PreparedDoc:
    """Extract text and compute embeddings without touching the DB. With `entry`, pdf is a paper."""
    if entry is not None:
        type_, num, title = "P", None, _paper_title(entry, pdf)
    else:
        type_, num, title = parse_name(pdf.name)
    kind = _kind(pdf)
    pages: list[tuple[int, str, int]] = []
    if kind == "ipynb":
        original, offset, n_pages = None, 0, cell_count(pdf)
        pages = _notebook_pages(pdf)
    else:
        with pymupdf.open(pdf) as doc:
            original, offset = read_split_meta(doc) if entry is None else (None, 0)
            skipped = read_split_skip(doc) if entry is None else frozenset()
            texts = ["" if i in skipped else page.get_text("text") for i, page in enumerate(doc, 1)]
        n_pages = len(texts)
        if entry is not None:
            texts = strip_references(texts)
        for page, text in enumerate(texts, 1):
            chunks = chunk_text(text)
            if chunks:
                pages += [(page, chunk, 0) for chunk in chunks]
            else:
                pages.append((page, "", 1))
    to_embed = [f"{title}\n{t}" for _, t, no_text in pages if not no_text]
    vectors = np.zeros((0, embedder.dim), np.float32)
    if to_embed:
        vectors = np.asarray(embedder.embed_documents(to_embed), dtype=np.float32)
        if vectors.shape != (len(to_embed), embedder.dim):
            raise ValueError(f"embedder returned shape {vectors.shape} for {len(to_embed)} texts "
                             f"(expected ({len(to_embed)}, {embedder.dim}))")
    it = iter(vectors)
    rows = [(page, text, no_text, None if no_text else next(it).tobytes()) for page, text, no_text in pages]
    return _PreparedDoc(kind, type_, num, title, original, offset, n_pages, rows)


def _write_doc(conn: sqlite3.Connection, module_code: str, rel: str, digest: str,
               st_mtime: float, st_size: int, prep: _PreparedDoc, meta: _PaperMeta | None = None) -> None:
    paper = meta.as_row() if meta else (None, None, None, None)
    doc_id = conn.execute(
        "INSERT INTO docs(path, kind, module, type, num, title, sha256, original_file, page_offset, n_pages,"
        " indexed_at, mtime, size, citekey, year, authors, reports)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rel, prep.kind, module_code, prep.type, prep.num, prep.title, digest, prep.original_file, prep.page_offset,
         prep.n_pages, datetime.now(timezone.utc).isoformat(), st_mtime, st_size, *paper),
    ).lastrowid
    for page, text, no_text, emb in prep.rows:
        cid = conn.execute(
            "INSERT INTO chunks(doc_id, page, text, no_text, embedding) VALUES (?, ?, ?, ?, ?)",
            (doc_id, page, text, no_text, emb),
        ).lastrowid
        if not no_text:
            conn.execute(
                "INSERT INTO chunks_fts(rowid, text, title, module) VALUES (?, ?, ?, ?)",
                (cid, fold(text), fold(prep.title), module_code),
            )


def index_library(root: Path, embedder: Embedder, full: bool = False,
                  log: Callable[[str], None] = print) -> IndexStats:
    modules = load_modules(root)
    conn = connect(root)
    try:
        stored = (get_meta(conn, "model"), get_meta(conn, "dim"))
        has_docs = conn.execute("SELECT 1 FROM docs LIMIT 1").fetchone() is not None
        if not full and stored != (embedder.model, str(embedder.dim)) and (has_docs or stored[0] is not None):
            log("embedding model changed → full rebuild")
            full = True
        stored_schema = get_meta(conn, "schema_version")
        has_kind = any(r[1] == "kind" for r in conn.execute("PRAGMA table_info(docs)"))
        # A brand-new DB was just created with the current schema: nothing to migrate.
        migrate = stored_schema != str(SCHEMA_VERSION) and (has_docs or stored_schema is not None or not has_kind)
        if migrate:
            log(f"index schema version {stored_schema or 'unset'} → {SCHEMA_VERSION}: full rebuild")
            full = True

        def in_savepoint(fn: Callable[[], object]) -> None:
            """Run fn's writes atomically; incremental mode commits each one."""
            if not full:
                conn.execute("BEGIN IMMEDIATE")
            conn.execute("SAVEPOINT doc")
            try:
                fn()
            except BaseException:
                conn.execute("ROLLBACK TO doc")
                conn.execute("RELEASE doc")
                if not full:
                    conn.execute("COMMIT")
                raise
            conn.execute("RELEASE doc")
            if not full:
                conn.execute("COMMIT")

        if full:
            # Wipe + rebuild in ONE transaction: WAL readers keep the old index until COMMIT.
            conn.execute("BEGIN IMMEDIATE")
            if migrate:
                # DDL is transactional in SQLite, so readers still see the old tables until COMMIT.
                _recreate_content_tables(conn)
            else:
                conn.execute("DELETE FROM chunks_fts")
                conn.execute("DELETE FROM chunks")
                conn.execute("DELETE FROM docs")
            _bump_generation(conn)

        existing = {r["path"]: r for r in conn.execute(
            "SELECT id, path, module, sha256, mtime, size, title, citekey, year, authors, reports FROM docs")}
        stats, seen = IndexStats(), set()

        def sync_meta(old: sqlite3.Row, meta: _PaperMeta | None) -> None:
            """Report tags etc. change without the PDF changing: update in place, no re-embedding."""
            if meta and (old["citekey"], old["year"], old["authors"], old["reports"]) != meta.as_row():
                in_savepoint(lambda: conn.execute(
                    "UPDATE docs SET citekey = ?, year = ?, authors = ?, reports = ? WHERE id = ?",
                    (*meta.as_row(), old["id"])))

        def index_one(path: Path, rel: str, module_code: str,
                      entry: BibEntry | None = None, meta: _PaperMeta | None = None) -> None:
            seen.add(rel)  # even if it fails below, so its previous entry is kept
            old = existing.get(rel)
            try:
                st = path.stat()
                same_title = entry is None or (old is not None and old["title"] == _paper_title(entry, path))
                if old is not None and same_title and (old["mtime"], old["size"]) == (st.st_mtime, st.st_size):
                    sync_meta(old, meta)
                    stats.unchanged += 1
                    return
                digest = sha256(path)
                if old is not None and same_title and old["sha256"] == digest:
                    in_savepoint(lambda: conn.execute(
                        "UPDATE docs SET mtime = ?, size = ? WHERE id = ?",
                        (st.st_mtime, st.st_size, old["id"])))
                    sync_meta(old, meta)
                    stats.unchanged += 1
                    return
                prep = _prepare_doc(embedder, path, entry)

                def write() -> None:
                    if old is not None:
                        _delete_doc(conn, old["id"])
                    _write_doc(conn, module_code, rel, digest, st.st_mtime, st.st_size, prep, meta)
                    _bump_generation(conn)

                in_savepoint(write)
            except Exception as e:
                if not full and conn.in_transaction:
                    conn.execute("ROLLBACK")
                log(f"error {rel}: {e}")
                stats.errors += 1
                return
            if old is None:
                stats.added += 1
            else:
                stats.updated += 1
            stats.pages += prep.n_pages
            log(f"indexed {rel}")

        for module in modules:
            for pdf in module_docs(root, module):
                index_one(pdf, pdf.relative_to(root).as_posix(), module.code)

        papers = load_papers(root)
        if papers is not None:
            found = _paper_entries(papers, log)
            if found is None:
                seen.update(rel for rel, row in existing.items() if row["module"] == PAPERS)
            else:
                entries, tags = found
                for entry in entries:
                    pdf = pdf_for(entry)
                    if pdf is None:
                        continue
                    rel = paper_rel(papers, pdf)  # zotero:storage/…, relative to zotero_dir
                    if rel in seen:
                        log(f"skipped {entry.key}: same PDF as another entry ({rel})")
                        continue
                    meta = _PaperMeta(entry.key, entry.year, "; ".join(entry.authors),
                                      ",".join(sorted(tags.get(entry.key, ()))))
                    index_one(pdf, rel, PAPERS, entry, meta)

        if not full:
            conn.execute("BEGIN IMMEDIATE")
        for rel, row in existing.items():
            if rel not in seen:
                _delete_doc(conn, row["id"])
                _bump_generation(conn)
                stats.removed += 1
                log(f"removed {rel}")
        _set_meta(conn, "model", embedder.model)
        _set_meta(conn, "dim", str(embedder.dim))
        _set_meta(conn, "schema_version", str(SCHEMA_VERSION))
        conn.execute("COMMIT")
        return stats
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()


def library_status(root: Path) -> list[DocStatus]:
    indexed: dict[str, sqlite3.Row] = {}
    if db_path(root).exists():
        conn = connect(root, readonly=True)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(docs)")}
            kind = "kind" if "kind" in cols else "'pdf' AS kind"  # pre-migration DBs lack the column
            indexed = {r["path"]: r for r in conn.execute(
                f"SELECT path, {kind}, module, type, num, title, n_pages, mtime, size FROM docs").fetchall()}
        finally:
            conn.close()
    out = []
    on_disk: set[str] = set()
    for module in load_modules(root):
        for pdf in module_docs(root, module):
            rel = pdf.relative_to(root).as_posix()
            type_, num, title = parse_name(pdf.name)
            row = indexed.get(rel)
            if row is None:
                state, n_pages = "new", None
            else:
                st = pdf.stat()
                state = "indexed" if (row["mtime"], row["size"]) == (st.st_mtime, st.st_size) else "stale"
                n_pages = row["n_pages"]
            on_disk.add(rel)
            out.append(DocStatus(rel, module.code, type_, num, title, n_pages, state, _kind(pdf)))
    for rel, row in indexed.items():
        if rel not in on_disk and row["module"] != PAPERS:
            out.append(DocStatus(rel, row["module"], row["type"], row["num"], row["title"],
                                 row["n_pages"], "deleted", row["kind"]))
    return out


def paper_status(root: Path) -> list[PaperStatus]:
    """Every paper the configured bibs list, with its index state ("no pdf" when Zotero has no PDF)."""
    papers = load_papers(root)
    if papers is None:
        return []
    found = _paper_entries(papers, lambda _: None)
    if found is None:
        return []
    entries, tags = found
    indexed: dict[str, sqlite3.Row] = {}
    if db_path(root).exists():
        conn = connect(root, readonly=True)
        try:
            if any(r[1] == "citekey" for r in conn.execute("PRAGMA table_info(docs)")):
                indexed = {r["citekey"]: r for r in conn.execute(
                    "SELECT citekey, n_pages, mtime, size FROM docs WHERE module = ?", (PAPERS,))}
        finally:
            conn.close()
    out = []
    for e in entries:
        pdf, row = pdf_for(e), indexed.get(e.key)
        if pdf is None:
            state, n_pages = "no pdf", None
        elif row is None:
            state, n_pages = "new", None
        else:
            st = pdf.stat()
            state = "indexed" if (row["mtime"], row["size"]) == (st.st_mtime, st.st_size) else "stale"
            n_pages = row["n_pages"]
        out.append(PaperStatus(e.key, e.title, e.year, n_pages, ",".join(sorted(tags.get(e.key, ()))), state))
    return out

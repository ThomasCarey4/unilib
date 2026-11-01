from __future__ import annotations

import sqlite3
import threading
import tomllib
from pathlib import Path

import pymupdf
from mcp.server.mcpserver import Image, MCPServer

from .config import PAPERS, db_path, library_root, load_modules, load_papers, paper_file
from .embed import Embedder
from .index import connect, library_status, paper_status
from .organize import parse_name
from .notebook import NotebookError, section_at
from .pdftools import render_page
from .rerank import Reranker
from .models import make_embedder, make_reranker
from . import search as search_mod
from .search import IndexMismatchError, last_rerank_error
from .textnorm import fold
from .search import search as run_search

NOT_INDEXED = "Library not indexed — run `unilib index`."
DB_BUSY = "Index is being built or is unavailable — try again shortly / run `unilib index`."

mcp = MCPServer(
    "unilib",
    instructions=(
        "Library of the user's university lecture slides and, under module PAPERS, the research papers "
        "in their Zotero 'Uni' collection. Call `search` first, cite results verbatim (`path p.N` for "
        "slides, `@citekey p.N` for papers), and call `get_page` to see equations and diagrams on the "
        "rendered page before relying on garbled text. To verify a LaTeX citation, search the claim with "
        "`citekey=`. Bibliography checks and Zotero actions are in the separate `unilib-local` server."
        " Reading-list textbook chapters (files B…) are excluded from search unless `books=True`."
        " To add files, follow the /ingest skill: ingest_list, ingest_peek, then ingest_place/ingest_split"
        " only after the user approves, then index."
    ),
)

_embedder: Embedder | None = None
_embedder_lock = threading.Lock()  # tools run in worker threads


def _get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        with _embedder_lock:
            if _embedder is None:
                _embedder = make_embedder()
    return _embedder


_reranker: Reranker | None = None
_reranker_lock = threading.Lock()


def _get_reranker() -> Reranker | None:
    """The cross-encoder when the default ranking reranks; its model loads on first use."""
    global _reranker
    if not search_mod.DEFAULT_RANK.rerank:
        return None
    if _reranker is None:
        with _reranker_lock:
            if _reranker is None:
                _reranker = make_reranker()
    return _reranker


@mcp.tool()
def search(query: str, module: str | None = None, k: int = 8,
           citekey: str | None = None, report: str | None = None, books: bool = False) -> str:
    """Hybrid keyword + semantic search over all lecture slides and Zotero papers (module PAPERS).

    Always returns the k best-ranked hits with no relevance cutoff, so judge
    relevance from the snippets yourself. Each hit starts with a citation
    `path p.N` (PDF slides) or `path cell N` (Jupyter notebooks) or, for papers, `@citekey p.N`; quote it
    verbatim. For a paper pass `@citekey` as get_page's `path`. citekey and report
    match case-sensitively. Snippets come from the PDF text layer and
    may garble maths, so call `get_page` for equations, formulas and diagrams.
    Use `list_library` to find module codes.

    Cost: each call takes about 2 s (up to ~6.5 s when candidates are long);
    the first call after the server has been idle for 15 min takes about 7 s
    while it starts and loads models (longer if they must be downloaded). Prefer one well-phrased query, with `module` when
    known, over many rapid variants. The top ~10 results are reranked by a
    cross-encoder, so the first few hits are the most reliable and hits beyond
    about 8 are in coarser order.

    Args:
        query: what to look for, in natural language or exact terms
        module: optional module code filter, e.g. MATH3001
        k: number of results (default 8, max 50)
        citekey: only this paper (module PAPERS), e.g. vaswani2017 — use to check a \\cite{…} claim
        report: only papers in this report's bibliography (names from `list_library`)
        books: include the modules' reading-list textbook chapters (B… files); off by default —
            use for background, derivations or worked examples when the slides are thin
    """
    if not search_mod._tokens(fold(query)):  # a lone symbol like ∇ folds to a keyword
        return "Empty query."
    root = library_root()
    if not db_path(root).exists():
        return NOT_INDEXED
    try:
        embedder = _get_embedder()
    except Exception as e:
        return (f"Embedding model unavailable ({type(e).__name__}: {' '.join(str(e).split())[:120]}) — "
                "run `unilib index` once with network access to download models.")
    try:
        hits = run_search(root, embedder, query, module=module, k=k, citekey=citekey,
                        report=report, books=books, reranker=_get_reranker())
    except IndexMismatchError as e:
        return f"Search unavailable: {e}."
    except FileNotFoundError:
        return NOT_INDEXED
    except sqlite3.DatabaseError:
        return DB_BUSY
    if not hits:
        return f"No results for {query!r}."
    out = "\n\n".join(
        f"[{i}] {h.cite()} — {h.module} · {h.title}\n{h.snippet}" for i, h in enumerate(hits, 1)
    )
    if err := last_rerank_error():
        out += f"\n\n(note: reranker unavailable — {err}; results in fused order)"
    return out


def _paper_path(citekey: str) -> Path | None:
    """The paper's PDF on this machine (docs.path is `zotero:…`, resolved against [papers] zotero_dir)."""
    root = library_root()
    try:
        papers = load_papers(root)
    except (tomllib.TOMLDecodeError, ValueError, KeyError, TypeError):
        return None
    if papers is None:
        return None
    try:
        conn = connect(root, readonly=True)
    except FileNotFoundError:
        return None
    try:
        row = conn.execute("SELECT path FROM docs WHERE module = ? AND citekey = ?", (PAPERS, citekey)).fetchone()
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()
    p = paper_file(papers, row[0]) if row else None
    return p if p is not None and p.is_file() else None


@mcp.tool()
def get_page(path: str, page: int, image: bool = True) -> list:
    """Full text of one slide page plus a PNG render, or one notebook section.

    `path` and `page` must be exactly the `path p.N` / `path cell N` citation
    from a search hit (not the "orig" page in parentheses). For PDFs the PNG
    is the reliable source for maths and diagrams; the text layer may garble
    them. For `.ipynb` notebooks `page` is the cell number N from the
    citation; the result is the text of the section starting at (or
    containing) that cell, with no image.

    Args:
        path: library-relative PDF/.ipynb path, or `@citekey` for a paper, exactly as in a search hit
        page: 1-based page number (PDF) or cell number (notebook) N from the same hit
        image: include the rendered PNG (default true, PDFs only); false returns text only
    """
    if path.startswith("@"):
        target = _paper_path(path[1:])
        if target is None:
            return [f"No indexed paper {path} — check `list_library` (module PAPERS)"]
    else:
        root = library_root().resolve()
        target = (root / path).resolve()
        if root not in target.parents or target.suffix.lower() not in (".pdf", ".ipynb") or not target.is_file():
            return [f"No such library PDF or notebook: {path}"]
    if target.suffix.lower() == ".ipynb":
        try:
            sec = section_at(target, page)
        except (NotebookError, ValueError) as e:
            return [f"Cannot read {path}: {e}"]
        return [f"{path} cell {sec.cell}\n\n{sec.text}"]
    try:
        with pymupdf.open(target) as doc:
            if not 1 <= page <= doc.page_count:
                return [f"Page {page} out of range 1-{doc.page_count} for {path}"]
            text = doc[page - 1].get_text("text").strip() or "[no text layer — see image]"
    except RuntimeError as e:  # includes pymupdf.FileDataError (corrupt or LFS-pointer file)
        return [f"Cannot read {path}: {e}"]
    out: list = [f"{path} p.{page}\n\n{text}"]
    if image:
        out.append(Image(data=render_page(target, page), format="png"))
    return out


@mcp.tool()
def list_library(module: str | None = None) -> str:
    """List modules (with their classification keywords) and their documents with page counts and
    index state; module PAPERS lists Zotero papers (by @citekey) and the report names usable as
    search's `report`.

    Args:
        module: optional module code filter, e.g. COMP3002
    """
    root = library_root()
    try:
        modules = load_modules(root)
        papers_cfg = load_papers(root)
    except FileNotFoundError:
        return f"No modules.toml in {root} — create it to define the modules."
    except (tomllib.TOMLDecodeError, ValueError, KeyError, TypeError) as e:
        return f"Invalid modules.toml: {e}"
    if module and module.upper() != PAPERS and not any(m.code.upper() == module.upper() for m in modules):
        known = [m.code for m in modules] + ([PAPERS] if papers_cfg else [])
        return f"Unknown module {module}; known: {', '.join(known)}"
    try:
        statuses = library_status(root)
    except sqlite3.DatabaseError:
        return DB_BUSY
    lines = [] if db_path(root).exists() else [NOT_INDEXED, ""]
    for m in modules:
        if module and m.code.upper() != module.upper():
            continue
        kw = f" · keywords: {', '.join(m.keywords)}" if m.keywords else ""
        lines.append(f"{m.code} — {m.name} (sem {m.semester}){kw}")
        docs = [s for s in statuses if s.module == m.code]
        for s in docs:
            name = s.path.rsplit("/", 1)[-1]
            if s.state == "new":
                lines.append(f"  {name} · not indexed")
            elif s.state == "deleted":
                lines.append(f"  {name} · deleted (re-run `unilib index`)")
            else:
                stale = " · stale (re-run `unilib index`)" if s.state == "stale" else ""
                unit = "cells" if s.kind == "ipynb" else "pages"
                book = " · reading list" if parse_name(name)[0] == "B" else ""
                lines.append(f"  {name} · {s.n_pages} {unit}{book}{stale}")
        if not docs:
            lines.append("  (empty)")
    if papers_cfg and (not module or module.upper() == PAPERS):
        lines.append(f"{PAPERS} — Zotero papers ({papers_cfg.bib})")
        if reports := papers_cfg.reports():
            lines.append("  reports: " + ", ".join(r.name for r in reports))
        try:
            rows = paper_status(root)
        except sqlite3.DatabaseError:
            return DB_BUSY
        except OSError as e:
            return f"Could not read a paper PDF ({e}); try again"
        for p in rows:
            head = f"  @{p.citekey} · {p.year or 'n.d.'} · {p.title}"
            if p.state == "no pdf":
                lines.append(f"{head} · no PDF")
            elif p.state == "new":
                lines.append(f"{head} · not indexed")
            else:
                stale = " · stale (re-run `unilib index`)" if p.state == "stale" else ""
                tags = f" · reports: {p.reports}" if p.reports else ""
                lines.append(f"{head} · {p.n_pages} pages{tags}{stale}")
        if not rows:
            lines.append("  (empty)")
    return "\n".join(lines)


from . import ingest_tools  # noqa: E402,F401  registers the ingest_* and index tools


def main(argv: list[str] | None = None) -> None:
    import argparse
    import os

    from .serve import DEFAULT_IDLE_EXIT_S, DEFAULT_PORT, serve_http

    p = argparse.ArgumentParser(prog="unilib-mcp", description="unilib MCP server (streamable HTTP at /mcp)")
    p.add_argument("--stdio", action="store_true", help="serve over stdio instead of HTTP")
    p.add_argument("--host", default="127.0.0.1", help="bind address without socket activation (default %(default)s)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="(default %(default)s)")
    p.add_argument("--idle-exit", type=float, metavar="S",
                   default=float(os.environ.get("UNILIB_IDLE_EXIT_S") or DEFAULT_IDLE_EXIT_S),
                   help="exit after S idle seconds; 0 never (default $UNILIB_IDLE_EXIT_S or %(default)s)")
    args = p.parse_args(argv)
    if args.stdio:
        mcp.run()
    else:
        serve_http(mcp, args.host, args.port, args.idle_exit or None)

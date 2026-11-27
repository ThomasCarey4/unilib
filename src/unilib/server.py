from __future__ import annotations

import functools
from dataclasses import replace
import sqlite3
import threading
import tomllib
from pathlib import Path
from typing import Callable

import pymupdf
from mcp.server.mcpserver import Image, MCPServer

from .config import (LESSON_PREFIX, LESSONS, PAPERS, Library, load_lessons, db_path, libraries, library_root, load_library_settings, load_modules,
                     load_papers, paper_file, using_library)
from .embed import Embedder
from .index import connect, lesson_status, library_status, paper_status
from .organize import parse_name
from .markdown import MarkdownError, read as md_read, section_at as md_section_at
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

UNI_INSTRUCTIONS = (
    "Library of the user's university lecture slides and, under module PAPERS, the research papers "
    "in their Zotero 'Uni' collection. Call `search` first, cite results verbatim (`path p.N` for "
    "slides, `@citekey p.N` for papers), and call `get_page` to see equations and diagrams on the "
    "rendered page before relying on garbled text. To verify a LaTeX citation, search the claim with "
    "`citekey=`. Bibliography checks and Zotero actions are in the separate `unilib-local` server."
    " Reading-list textbook chapters (files B…) are excluded from search unless `books=True`."
    " To add files, follow the /ingest skill: ingest_list, ingest_peek, then ingest_place/ingest_split"
    " only after the user approves, then index."
)

# The template server: tools register on it, `--stdio` serves it against $UNILIB_ROOT, and
# build_servers() copies its tools onto one server per library for HTTP.
mcp = MCPServer("unilib", instructions=UNI_INSTRUCTIONS)


def _others_note(others: list[Library]) -> str:
    if not others:
        return ""
    names = []
    for o in others:
        try:
            title = load_library_settings(o.root).title
        except (OSError, tomllib.TOMLDecodeError, ValueError):
            title = None
        names.append(f"{o.name} ({title})" if title else o.name)
    return (f" Other libraries on this server: {', '.join(names)}. They are not searched unless you pass "
            "`library=\"<name>\"` or `library=\"all\"`; their hits are cited with the library name as prefix.")


def library_instructions(lib: Library, others: list[Library] | None = None) -> str:
    """The endpoint's instructions: today's uni text for a library without a [library] table,
    otherwise generated from its modules.toml so the coverage it claims is what is indexed.
    `others` (the server's other libraries) are named so cross-library search is discoverable."""
    note = _others_note(others or [])
    try:
        settings = load_library_settings(lib.root)
        modules = load_modules(lib.root)
        papers = load_papers(lib.root)
    except (OSError, tomllib.TOMLDecodeError, ValueError, KeyError, TypeError):
        return (f"Library {lib.name!r} of reference documents. Call `list_library` to see what it holds, "
                "`search` before answering from memory, and cite hits verbatim." + note)
    if settings.title is None:
        return UNI_INSTRUCTIONS + note
    shelves = "; ".join(f"{m.code} — {m.name}" + (f" ({', '.join(m.keywords)})" if m.keywords else "")
                        for m in modules)
    if papers is not None:
        shelves += f"; {PAPERS} — research papers (cite `@citekey p.N`)"
    books = ("Book chapters (files B…) are included by default." if settings.books_default
             else "Book chapters (files B…) are excluded unless `books=True`.")
    return (
        f"{settings.title}. Modules: {shelves}. "
        "Call `search` before answering questions in these areas from memory, cite hits verbatim "
        "(`path p.N`, `@citekey p.N`), and call `get_page` to read the full page and see tables or diagrams. "
        f"{books} Use `list_library` for the full list of documents. "
        "Each hit's `[version]` says which version or edition it describes. When sources disagree, prefer the one "
        "matching the version the project runs, then the newest; use older books for how and why things work, "
        "and check current settings, APIs and tooling against version-matched docs. "
        "Quote at most a few lines of any source; paraphrase and cite instead. "
        "To add documents, follow the /ingest skill and change nothing before the user approves."
        + (" Project lessons (module LESSONS, cited `lesson:<id> §N`) are the user's own measured findings, "
           "incidents and decisions; filter them with `project=`." if _has_lessons(lib.root) else "")
        + note
    )


def _has_lessons(root: Path) -> bool:
    try:
        return bool(load_lessons(root))
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return False


def _bound(fn: Callable, root: Path) -> Callable:
    """`fn` run with `library_root()` = root (tools run in worker threads, so bind inside the call)."""
    @functools.wraps(fn)
    def run(*args, **kwargs):
        with using_library(root):
            return fn(*args, **kwargs)
    return run


def build_servers(libs: list[Library]) -> list[tuple[Library, MCPServer]]:
    """One MCP server per library with every template tool bound to that library's root.
    The first is named `unilib` (served at /mcp), the others `unilib-<name>`."""
    out = []
    for i, lib in enumerate(libs):
        others = [o for o in libs if o is not lib]
        m = MCPServer("unilib" if i == 0 else f"unilib-{lib.name}", instructions=library_instructions(lib, others))
        for tool in mcp._tool_manager.list_tools():
            m.add_tool(_bound(tool.fn, lib.root), name=tool.name, title=tool.title,
                       description=tool.description, annotations=tool.annotations)
        out.append((lib, m))
    return out


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
           citekey: str | None = None, report: str | None = None, books: bool | None = None,
           superseded: bool = False, project: str | None = None, library: str | None = None) -> str:
    """Hybrid keyword + semantic search over this library's documents (slides, book chapters, docs)
    and its Zotero papers (module PAPERS).

    Always returns the k best-ranked hits with no relevance cutoff, so judge
    relevance from the snippets yourself. Each hit starts with a citation
    `path p.N` (PDF), `path cell N` (Jupyter notebooks), `path §N` (markdown docs, section N),
    `@citekey p.N` (papers) or `lesson:<id> §N` (project lessons, module LESSONS), with the source's
    `[version]` when known; quote it
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
        module: optional module code filter, e.g. MATH3001, or several comma-separated, e.g. PG,TSDB
        k: number of results (default 8, max 50)
        citekey: only this paper (module PAPERS), e.g. vaswani2017 — use to check a \\cite{…} claim
        report: only papers in this report's bibliography (names from `list_library`)
        books: include book chapters (B… files). Default: the library's setting (see the server
            instructions); in the uni library they are off — turn on for background, derivations
            or worked examples when the slides are thin
        project: only lessons of this project (module LESSONS), e.g. serenlink
        superseded: also search documents marked superseded (older versions of a source); a hit's
            `[version]` tag says which version it describes
        library: another library served by this server, or "all" (default: this endpoint's own).
            Hits from another library are cited with its name as prefix, e.g. `eng:PG/… p.N`,
            which get_page accepts as is. Names are listed in the server instructions.
    """
    if not search_mod._tokens(fold(query)):  # a lone symbol like ∇ folds to a keyword
        return "Empty query."
    targets = _targets(library)
    if isinstance(targets, str):
        return targets
    try:
        embedder = _get_embedder()
    except Exception as e:
        return (f"Embedding model unavailable ({type(e).__name__}: {' '.join(str(e).split())[:120]}) — "
                "run `unilib index` once with network access to download models.")
    results, notes = [], []
    for label, lib_root in targets:
        name = f"library {label!r}: " if label else ""
        if not db_path(lib_root).exists():
            notes.append(f"{name}{NOT_INDEXED}")
            continue
        try:
            with using_library(lib_root):
                hits = run_search(lib_root, embedder, query, module=module, k=k, citekey=citekey,
                                  report=report, books=books, superseded=superseded, project=project,
                                  reranker=_get_reranker())
        except IndexMismatchError as e:
            notes.append(f"{name}Search unavailable: {e}.")
            continue
        except FileNotFoundError:
            notes.append(f"{name}{NOT_INDEXED}")
            continue
        except sqlite3.DatabaseError:
            notes.append(f"{name}{DB_BUSY}")
            continue
        if err := last_rerank_error():
            notes.append(f"(note: {name}reranker unavailable — {err}; results in fused order)")
        results.append((label, hits))
    if len(targets) == 1 and not results:
        return notes[0]
    if len(targets) == 1:
        label, hits = results[0]
        hits = [replace(h, library=label) for h in hits] if label else hits
    else:
        hits = search_mod.merge_hits(results, k)
    if not hits:
        return "\n".join([f"No results for {query!r}.", *notes])
    out = "\n\n".join(
        f"[{i}] {h.cite()} — {h.module} · {h.title}\n{h.snippet}" for i, h in enumerate(hits, 1)
    )
    if notes:
        out += "\n\n" + "\n".join(notes)
    return out


def _current_library() -> Library:
    """The library this endpoint serves (the root bound for this call)."""
    root = library_root()
    try:
        libs = libraries()
    except ValueError:
        libs = []
    return next((lib for lib in libs if lib.root == root), Library("uni", root))


def _targets(library: str | None) -> list[tuple[str | None, Path]] | str:
    """(citation prefix or None for this endpoint's own library, root) per library to search, or an error."""
    current = _current_library()
    if library is None or library == current.name:
        return [(None, current.root)]
    try:
        libs = libraries()
    except ValueError as e:
        return f"Cannot read UNILIB_LIBRARIES: {e}"
    others = [(lib.name, lib.root) for lib in libs if lib.root != current.root]
    if library == "all":
        return [(None, current.root), *others]
    for name, root in others:
        if name == library:
            return [(name, root)]
    names = ", ".join(lib.name for lib in libs)
    return f"Unknown library {library!r}; known: {names} (or 'all')."


def _split_library(path: str) -> tuple[Path | None, str, str]:
    """`eng:PG/x.pdf` -> (eng's root, 'PG/x.pdf', 'eng:') when `eng` is another served library;
    otherwise (None, path, '')."""
    head, sep, rest = path.partition(":")
    if not sep or "/" in head:
        return None, path, ""
    try:
        libs = libraries()
    except ValueError:
        return None, path, ""
    for lib in libs:
        if lib.name == head:
            return lib.root, rest, f"{head}:"
    return None, path, ""


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


def _lesson_path(lesson_id: str) -> Path | None:
    """The lesson's markdown file (docs.path is its absolute path on the server)."""
    try:
        conn = connect(library_root(), readonly=True)
    except FileNotFoundError:
        return None
    try:
        row = conn.execute("SELECT path FROM docs WHERE module = ? AND citekey = ?", (LESSONS, lesson_id)).fetchone()
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()
    p = Path(row[0]) if row else None
    return p if p is not None and p.is_file() else None


@mcp.tool()
def get_page(path: str, page: int, image: bool = True) -> list:
    """Full text of one PDF page plus a PNG render, or one notebook or markdown section.

    `path` and `page` must be exactly the `path p.N` / `path cell N` citation
    from a search hit (not the "orig" page in parentheses). For PDFs the PNG
    is the reliable source for maths and diagrams; the text layer may garble
    them. For `.ipynb` notebooks `page` is the cell number N from the
    citation; the result is the text of the section starting at (or
    containing) that cell, with no image. For `.md` documents `page` is the section number N
    of `path §N`; the result is that section's text and its source URL when known, with no image.

    Args:
        path: library-relative PDF/.ipynb/.md path, `@citekey` for a paper or `lesson:<id>` for a
            lesson, exactly as in a search hit (with its `<library>:` prefix if it has one)
        page: 1-based page number (PDF), cell number (notebook) or section number (markdown) N from the same hit
        image: include the rendered PNG (default true, PDFs only); false returns text only
    """
    other, rest, prefix = _split_library(path)
    if other is not None and other != library_root():
        with using_library(other):
            out = get_page(rest, page, image)
        if out and isinstance(out[0], str) and out[0].startswith(rest):
            out[0] = prefix + out[0]
        return out
    if path.startswith(LESSON_PREFIX):
        target = _lesson_path(path.removeprefix(LESSON_PREFIX))
        if target is None:
            return [f"No indexed lesson {path} — check `list_library` (module LESSONS)"]
        try:
            sec = md_section_at(target, page)
        except (MarkdownError, ValueError) as e:
            return [f"Cannot read {path}: {e}"]
        return [f"{path} §{sec.num}\n\n{sec.text}\n\n(file: {target})"]
    if path.startswith("@"):
        target = _paper_path(path[1:])
        if target is None:
            return [f"No indexed paper {path} — check `list_library` (module PAPERS)"]
    else:
        root = library_root().resolve()
        target = (root / path).resolve()
        if root not in target.parents or target.suffix.lower() not in (".pdf", ".ipynb", ".md") or not target.is_file():
            return [f"No such library PDF, notebook or markdown document: {path}"]
    if target.suffix.lower() == ".md":
        try:
            meta, _ = md_read(target)
            sec = md_section_at(target, page)
        except (MarkdownError, ValueError) as e:
            return [f"Cannot read {path}: {e}"]
        url = meta.get("source_url")
        src = f"\n\n(source: {url}{'#' + sec.anchor if sec.anchor else ''})" if url else ""
        return [f"{path} §{sec.num}\n\n{sec.text}{src}"]
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
def list_library(module: str | None = None, library: str | None = None) -> str:
    """List modules (with their classification keywords) and their documents with page counts and
    index state; module PAPERS lists Zotero papers (by @citekey) and the report names usable as
    search's `report`; module LESSONS lists project lessons (by lesson:<id>).

    Args:
        module: optional module code filter, e.g. COMP3002
        library: another library served by this server, or "all" (default: this endpoint's own)
    """
    if library is not None and library != _current_library().name:
        targets = _targets(library)
        if isinstance(targets, str):
            return targets
        if library != "all":
            with using_library(targets[0][1]):
                return list_library(module)
        parts = [list_library(module)]
        for name, root in targets[1:]:
            with using_library(root):
                parts.append(f"== library {name} ==\n" + list_library(module))
        return "\n\n".join(parts)
    root = library_root()
    try:
        modules = load_modules(root)
        papers_cfg = load_papers(root)
    except FileNotFoundError:
        return f"No modules.toml in {root} — create it to define the modules."
    except (tomllib.TOMLDecodeError, ValueError, KeyError, TypeError) as e:
        return f"Invalid modules.toml: {e}"
    try:
        has_lessons = bool(load_lessons(root))
    except ValueError as e:
        return f"Invalid modules.toml: {e}"
    if module and module.upper() not in (PAPERS, LESSONS) and not any(m.code.upper() == module.upper() for m in modules):
        known = [m.code for m in modules] + ([PAPERS] if papers_cfg else []) + ([LESSONS] if has_lessons else [])
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
        sem = f" (sem {m.semester})" if m.semester is not None else ""
        lines.append(f"{m.code} — {m.name}{sem}{kw}")
        docs = [s for s in statuses if s.module == m.code]
        for s in docs:
            name = s.path.rsplit("/", 1)[-1]
            if s.state == "new":
                lines.append(f"  {name} · not indexed")
            elif s.state == "deleted":
                lines.append(f"  {name} · deleted (re-run `unilib index`)")
            else:
                stale = " · stale (re-run `unilib index`)" if s.state == "stale" else ""
                unit = {"ipynb": "cells", "md": "sections"}.get(s.kind, "pages")
                book = " · reading list" if parse_name(name)[0] == "B" else ""
                ver = f" · {s.version}" if s.version else ""
                sup = " · superseded" if s.superseded else ""
                lines.append(f"  {name} · {s.n_pages} {unit}{book}{ver}{sup}{stale}")
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
    if has_lessons and (not module or module.upper() == LESSONS):
        lines.append(f"{LESSONS} — project lessons (cite `lesson:<id> §N`; filter with search's `project`)")
        try:
            les = lesson_status(root)
        except sqlite3.DatabaseError:
            return DB_BUSY
        for s in les:
            if s.state == "invalid":
                lines.append(f"  {s.title} · invalid, not indexed: {s.error}")
                continue
            head = f"  {LESSON_PREFIX}{s.id} · {s.project} · {s.kind} · {s.date} · {s.title}"
            state = {"new": " · not indexed", "stale": " · stale (re-run `unilib index`)"}.get(s.state, "")
            sup = " · superseded" if s.superseded else ""
            lines.append(f"{head}{sup}{state}")
    return "\n".join(lines)


from . import ingest_tools  # noqa: E402,F401  registers the ingest_* and index tools


def main(argv: list[str] | None = None) -> None:
    import argparse
    import os

    from .serve import DEFAULT_IDLE_EXIT_S, DEFAULT_PORT, serve_http

    p = argparse.ArgumentParser(
        prog="unilib-mcp",
        description="unilib MCP server (streamable HTTP: the first library of $UNILIB_LIBRARIES at /mcp, "
                    "the others at /<name>/mcp; without it, $UNILIB_ROOT at /mcp)")
    p.add_argument("--stdio", action="store_true", help="serve over stdio instead of HTTP")
    p.add_argument("--host", default="127.0.0.1", help="bind address without socket activation (default %(default)s)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="(default %(default)s)")
    p.add_argument("--idle-exit", type=float, metavar="S",
                   default=float(os.environ.get("UNILIB_IDLE_EXIT_S") or DEFAULT_IDLE_EXIT_S),
                   help="exit after S idle seconds; 0 never (default $UNILIB_IDLE_EXIT_S or %(default)s)")
    args = p.parse_args(argv)
    if args.stdio:
        mcp.run()  # one library: $UNILIB_ROOT
    else:
        try:
            libs = libraries()
        except ValueError as e:
            raise SystemExit(f"unilib-mcp: {e}")
        serve_http([m for _, m in build_servers(libs)], args.host, args.port, args.idle_exit or None,
                   names=[lib.name for lib in libs])

"""Ingest tools for the MCP server: the /ingest workflow run against the server's library.

Files arrive in `<library>/_ingest/` (over WebDAV). Each tool takes a file name relative to
`_ingest/` and runs the matching `unilib` CLI command in-process, returning its output.
Tools that write run one at a time under a lock; `stdout` is captured under the same lock.
"""
from __future__ import annotations

import contextlib
import io
import shutil
import threading
from pathlib import Path

from mcp.server.mcpserver import Image

from . import cli
from .config import library_root
from .index import index_library
from .notebook import cell_count, sections
from .organize import unique_dest
from .pdftools import peek, render_page
from .server import _get_embedder, mcp

_lock = threading.Lock()


def _ingest_dir() -> Path:
    return library_root().resolve() / "_ingest"


def _file(name: str) -> Path:
    """`name` under _ingest/, refusing anything that resolves outside it."""
    base = _ingest_dir()
    p = (base / name).resolve()
    if base not in p.parents or not p.is_file():
        raise FileNotFoundError(f"no file {name!r} in _ingest/ — see ingest_list")
    return p


def _cli(*argv: str) -> str:
    out = io.StringIO()
    with _lock, contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.main(list(argv))
    text = out.getvalue().strip()
    return text if code == 0 else f"{text}\n(exit {code})"


@mcp.tool()
def ingest_list() -> str:
    """List the files waiting in the library's drop zone `_ingest/` (and any in `_unsorted/`).

    Start of the /ingest workflow: users drop PDF, PPTX and .ipynb files there over WebDAV.
    """
    root = library_root().resolve()
    lines = []
    for d in ("_ingest", "_unsorted"):
        files = sorted(p for p in (root / d).rglob("*") if p.is_file() and p.name != ".gitkeep")
        lines.append(f"{d}/: " + ("empty" if not files else f"{len(files)} files"))
        lines += [f"  {p.relative_to(root / d)}  ({p.stat().st_size // 1024} KiB)" for p in files]
    return "\n".join(lines)


@mcp.tool()
def ingest_peek(file: str, pages: int = 3, render: int = 1) -> list:
    """First pages' text of a file in _ingest/ plus a PNG of one page, to classify it.

    For a .ipynb: cell count and the first 3 sections, no image.

    Args:
        file: name relative to _ingest/, as listed by ingest_list
        pages: how many pages of text (default 3)
        render: page to render (default 1)
    """
    try:
        p = _file(file)
        if p.suffix.lower() == ".ipynb":
            secs = sections(p)
            body = "\n".join(f"--- cell {s.cell} ---\n{s.text}" for s in secs[:3])
            return [f"# {p.name} ({cell_count(p)} cells)\n{body}"]
        return [peek(p, pages=pages), Image(data=render_page(p, render), format="png")]
    except (OSError, ValueError, RuntimeError) as e:
        return [f"Cannot peek {file}: {e}"]


@mcp.tool()
def ingest_outline(file: str, book: bool = False, author: str | None = None, start: int = 1) -> str:
    """Per-page map of a PDF in _ingest/ for choosing split points (decks over ~60 pages).

    With book=True, proposes B parts (one per chapter) and skip ranges for exercise pages,
    printed as a `unilib split` command: pass its --part values to ingest_split as `parts`
    and its --skip values as `skip`.

    Args:
        file: name relative to _ingest/
        book: propose a textbook split
        author: surname for part titles when the PDF metadata is wrong (book only)
        start: first B number, when the module already has B parts (book only)
    """
    try:
        p = _file(file)
    except FileNotFoundError as e:
        return str(e)
    argv = ["outline", str(p)] + (["--book", "--start", str(start)] if book else [])
    return _cli(*argv, *(["--author", author] if book and author else []))


@mcp.tool()
def ingest_convert(file: str) -> str:
    """Convert a .pptx in _ingest/ to PDF next to it (LibreOffice). On failure it moves to _unsorted/."""
    try:
        p = _file(file)
    except FileNotFoundError as e:
        return str(e)
    return _cli("convert", str(p))


@mcp.tool()
def ingest_place(file: str, module: str, type_num: str, title: str) -> str:
    """Move a PDF or notebook from _ingest/ into a module folder, renamed `<TYPE><NN>_<Title>`.

    A sibling .pptx goes to the module's _originals/. Exact duplicates of a library file are dropped.
    Run only after the user has approved the ingest table.

    Args:
        file: name relative to _ingest/
        module: module code, e.g. MATH3001
        type_num: type and number, e.g. L03, T02, CW1, E09, B01, X
        title: short title, e.g. "Newton methods"
    """
    try:
        p = _file(file)
    except FileNotFoundError as e:
        return str(e)
    return _cli("place", str(p), module, type_num, title)


@mcp.tool()
def ingest_split(file: str, module: str, parts: list[str], skip: list[str] | None = None) -> str:
    """Cut a PDF in _ingest/ into parts in a module folder; the source is archived in _originals/.

    Run only after the user has approved the ingest table.

    Args:
        file: name relative to _ingest/
        module: module code, e.g. COMP3002
        parts: "START-END:TYPE:NN:Title" each, e.g. "1-34:L:01:Intro"
        skip: "START-END" source page ranges kept in the part but not indexed (book exercise sets)
    """
    try:
        p = _file(file)
    except FileNotFoundError as e:
        return str(e)
    argv = ["split", str(p), module]
    for part in parts:
        argv += ["--part", part]
    for s in skip or []:
        argv += ["--skip", s]
    return _cli(*argv)


@mcp.tool()
def ingest_unsort(file: str) -> str:
    """Move a low-confidence file from _ingest/ to _unsorted/ (after the user approved the table).

    Args:
        file: name relative to _ingest/
    """
    try:
        p = _file(file)
    except FileNotFoundError as e:
        return str(e)
    with _lock:
        dest = unique_dest(library_root().resolve() / "_unsorted", p.name)
        dest.parent.mkdir(exist_ok=True)
        shutil.move(p, dest)
    return f"moved {file} -> _unsorted/{dest.name}"


@mcp.tool()
def index(full: bool = False) -> str:
    """Index new and changed library files so search sees them (after ingest_place/ingest_split).

    Args:
        full: rebuild the whole index (slow; only when asked)
    """
    log: list[str] = []
    try:
        embedder = _get_embedder()
        with _lock:
            s = index_library(library_root(), embedder, full=full, log=log.append)
    except Exception as e:
        return f"Index failed: {type(e).__name__}: {e}"
    notes = [ln for ln in log if not ln.startswith("indexed ")]  # errors, removals, rebuilds, skips
    out = (f"added {s.added}, updated {s.updated}, removed {s.removed}, unchanged {s.unchanged}, "
           f"pages indexed {s.pages}, errors {s.errors}")
    return "\n".join([out, *notes[:30]] + ([f"… {len(notes) - 30} more"] if len(notes) > 30 else []))

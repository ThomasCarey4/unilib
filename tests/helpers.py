import hashlib
import html
import json
import re
from pathlib import Path

import numpy as np
import pymupdf

MODULES_TOML = """
[[module]]
code = "MATH3001"
name = "Scientific Computation"
semester = 1
folder = "MATH3001_Scientific-Computation"

[[module]]
code = "COMP3002"
name = "Knowledge Representation & Reasoning"
semester = 1
folder = "COMP3002_Knowledge-Representation-Reasoning"
keywords = ["logic"]
"""

SCI = "MATH3001_Scientific-Computation"
KRR = "COMP3002_Knowledge-Representation-Reasoning"


def make_pdf(path: Path, pages: list[str], title_pages: tuple[int, ...] = (), toc=None) -> Path:
    """Create a PDF with one page per string. Pages whose 0-based index is in
    title_pages use a large font (keep their text short). Non-ASCII pages are
    rendered as HTML so Greek and maths symbols survive text extraction."""
    doc = pymupdf.open()
    for i, text in enumerate(pages):
        page = doc.new_page()
        if text:
            size = 28 if i in title_pages else 11
            rect = pymupdf.Rect(50, 50, 550, 800)
            if text.isascii():
                page.insert_textbox(rect, text, fontsize=size)
            else:
                page.insert_htmlbox(rect, f'<p style="font-size:{size}px">{html.escape(text)}</p>')
    if toc:
        doc.set_toc(toc)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


class FakeEmbedder:
    """Deterministic bag-of-words hashing embedder for fast tests."""

    model = "fake"
    dim = 64

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, np.float32)
        for w in re.findall(r"\w+", text.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return np.array([self._vec(t) for t in texts], np.float32).reshape(len(texts), self.dim)

    def embed_query(self, text: str) -> np.ndarray:
        return self._vec(text)


def make_notebook(path: Path, cells: list[tuple[str, str]] | list[tuple[str, str, list]]) -> Path:
    """cells: (cell_type, source[, outputs]) with cell_type 'markdown' | 'code'."""
    nb = {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": []}
    for c in cells:
        cell = {"cell_type": c[0], "metadata": {}, "source": c[1]}
        if c[0] == "code":
            cell["outputs"] = c[2] if len(c) > 2 else []
            cell["execution_count"] = None
        nb["cells"].append(cell)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(nb))
    return path


def make_bib(path: Path, entries: list[dict]) -> Path:
    """Better-BibLaTeX-style .bib. Each dict: 'key', optional 'type' (default article), other keys are fields."""
    out = []
    for e in entries:
        e = dict(e)
        key, typ = e.pop("key"), e.pop("type", "article")
        fields = ",\n".join(f"  {k} = {{{v}}}" for k, v in e.items())
        out.append(f"@{typ}{{{key},\n{fields}\n}}\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out))
    return path


def make_report_bib(path: Path, entries: list[dict]) -> Path:
    """A report's refs.bib plus the marker sync-report writes, so [papers] reports picks it up."""
    from unilib.config import REPORT_MARKER
    make_bib(path, entries)
    (path.parent / REPORT_MARKER).write_text("")
    return path


def add_papers(root: Path, bib: Path, reports: str | None = None, zotero_dir: Path | None = None) -> None:
    """Append a [papers] section to the library's modules.toml. `reports` is a glob of report bibs;
    zotero_dir defaults to the bib's folder (tests put paper PDFs under it)."""
    text = f"\n[papers]\nbib = {json.dumps(str(bib))}\nzotero_dir = {json.dumps(str(zotero_dir or bib.parent))}\n"
    if reports:
        text += f"reports = {json.dumps(reports)}\n"
    with open(root / "modules.toml", "a") as f:
        f.write(text)

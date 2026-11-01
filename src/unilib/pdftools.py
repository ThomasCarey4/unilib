from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from .bib import surname

TITLE_RE = re.compile(r"\b(lecture|week|session|part|chapter|topic)\s*\d+", re.I)


def peek(path: Path, pages: int = 3) -> str:
    path = Path(path)
    with pymupdf.open(path) as doc:
        out = [f"# {path.name} ({doc.page_count} pages)"]
        title = (doc.metadata or {}).get("title")
        if title:
            out.append(f"PDF title: {title}")
        for i in range(min(pages, doc.page_count)):
            out.append(f"--- page {i + 1} ---")
            out.append(doc[i].get_text("text").strip() or "[no text layer]")
    return "\n".join(out)


def render_page(path: Path, page: int, dpi: int = 110) -> bytes:
    path = Path(path)
    with pymupdf.open(path) as doc:
        if not 1 <= page <= doc.page_count:
            raise ValueError(f"page {page} out of range 1-{doc.page_count}")
        return doc[page - 1].get_pixmap(dpi=dpi).tobytes("png")


def _page_stats(page) -> tuple[float, int, list[tuple[float, int]]]:
    sizes, words, spans = [], 0, []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if span["text"].strip():
                    sizes.append(span["size"])
                    words += len(span["text"].split())
                    spans.append((span["size"], len(span["text"].strip())))
    return max(sizes, default=0.0), words, spans


def _weighted_median(spans: list[tuple[float, int]]) -> float:
    total = sum(w for _, w in spans)
    if not total:
        return 0.0
    acc = 0
    for size, w in sorted(spans):
        acc += w
        if acc * 2 >= total:
            return size
    return 0.0


def outline(path: Path) -> list[str]:
    """Compact per-page map used to choose split points."""
    path = Path(path)
    with pymupdf.open(path) as doc:
        lines = [f"# {path.name} ({doc.page_count} pages)"]
        toc = doc.get_toc()
        if toc:
            lines.append("## bookmarks")
            lines += [f"{'  ' * (lvl - 1)}p{"---" if pg < 1 else format(pg, "03d")} {title}" for lvl, title, pg in toc]
        pages = []
        weighted: list[tuple[float, int]] = []
        for page in doc:
            size, words, spans = _page_stats(page)
            weighted += spans
            text = page.get_text("text").strip()
            first = text.splitlines()[0][:90] if text else "[no text]"
            pages.append((first, size, words, text))
    body = _weighted_median(weighted)
    lines.append("## pages")
    for i, (first, size, words, text) in enumerate(pages, 1):
        head = text.splitlines()[:2]
        big_and_sparse = bool(words) and words <= 25 and size >= 1.4 * body
        title_like = big_and_sparse or any(len(ln.split()) <= 8 and TITLE_RE.search(ln) for ln in head)
        lines.append(f"p{i:03d} {'*TITLE* ' if title_like else ''}| {first}")
    return lines


CHAPTER_RE = re.compile(r"^(?:ch(?:apter)?\.?\s*)?(\d+)(?:[.:]\s*|\s+)(.+)$", re.I)
DEFAULT_SKIP = r"Exercise Set|Exercises|Problems"


@dataclass(frozen=True)
class BookPlan:
    parts: list[str]    # `split --part` specs, one per chapter
    skips: list[str]    # `split --skip` specs (exercise pages: kept in the PDF, not indexed)
    unsplit: list[str]  # level-1 entries with no part (front/back matter): "Title (pA-B)"


def book_plan(path: Path, author: str | None = None, start: int = 1,
              skip_pattern: str = DEFAULT_SKIP) -> BookPlan:
    """Propose `split` args for a textbook from its outline: one B part per level-1 chapter
    entry, skips for level-2 entries matching skip_pattern (from the page after the entry
    starts to the page before the next entry), nothing for front/back matter."""
    with pymupdf.open(path) as doc:
        n = doc.page_count
        toc = [(lvl, " ".join(title.replace("\x00", "").split()), pg) for lvl, title, pg in doc.get_toc() if pg >= 1]
        meta_author = ((doc.metadata or {}).get("author") or "").replace("\x00", "")
    first = re.split(r",| and |;", meta_author)[0].strip()
    author = author or (surname(first) if first else "") or "Book"
    level1 = [(title, pg) for lvl, title, pg in toc if lvl == 1]
    parts, unsplit, chapters = [], [], []
    num = start
    for j, (title, pg) in enumerate(level1):
        end = level1[j + 1][1] - 1 if j + 1 < len(level1) else n
        m = CHAPTER_RE.match(title)
        if m and pg <= end:
            parts.append(f"{pg}-{end}:B:{num:02d}:{author} Ch{int(m[1])} {m[2]}")
            chapters.append((pg, end))
            num += 1
        else:
            unsplit.append(f"{title} (p{pg}-{end})")
    skip_re = re.compile(skip_pattern, re.I)
    skips = []
    for i, (lvl, title, pg) in enumerate(toc):
        if lvl < 2 or not skip_re.search(title):
            continue
        chapter = next(((s, e) for s, e in chapters if s <= pg <= e), None)
        if chapter is None:
            continue
        nxt = toc[i + 1][2] if i + 1 < len(toc) else n + 1
        a, b = pg + 1, min(nxt - 1, chapter[1])
        if a <= b:
            skips.append(f"{a}-{b}")
    return BookPlan(parts, skips, unsplit)

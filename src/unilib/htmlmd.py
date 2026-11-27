"""A small HTML → Markdown converter for snapshotting documentation pages (stdlib only).

It keeps what retrieval needs: headings, paragraphs, lists, code blocks, inline code, emphasis,
tables (as pipe rows) and link targets. Navigation, scripts and styles are dropped, and only the
page's <main> or <article> is kept when there is one.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

_SKIP = {"script", "style", "nav", "header", "footer", "aside", "noscript", "svg", "form", "button"}
_BLOCK = {"p", "div", "section", "br", "hr", "dl", "dt", "dd", "figure", "figcaption", "blockquote"}


class _Converter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.pre = 0
        self.lists: list[str] = []   # "ul" | "ol"
        self.counters: list[int] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.table_rows: list[list[str]] = []
        self.href: list[str | None] = []

    def _emit(self, s: str) -> None:
        if self.cell is not None:
            self.cell.append(s)
        else:
            self.out.append(s)

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self._emit("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "pre":
            self.pre += 1
            self._emit("\n\n```\n")
        elif tag == "code" and not self.pre:
            self._emit("`")
        elif tag in ("b", "strong"):
            self._emit("**")
        elif tag in ("i", "em"):
            self._emit("*")
        elif tag in ("ul", "ol"):
            self.lists.append(tag)
            self.counters.append(0)
            self._emit("\n")
        elif tag == "li":
            depth = "  " * (len(self.lists) - 1)
            if self.lists and self.lists[-1] == "ol":
                self.counters[-1] += 1
                self._emit(f"\n{depth}{self.counters[-1]}. ")
            else:
                self._emit(f"\n{depth}- ")
        elif tag == "a":
            self.href.append(dict(attrs).get("href"))
        elif tag == "table":
            self.table_rows = []
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []
        elif tag in _BLOCK:
            self._emit("\n\n")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self._emit("\n\n")
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self._emit("\n```\n\n")
        elif tag == "code" and not self.pre:
            self._emit("`")
        elif tag in ("b", "strong"):
            self._emit("**")
        elif tag in ("i", "em"):
            self._emit("*")
        elif tag in ("ul", "ol") and self.lists:
            self.lists.pop()
            self.counters.pop()
            self._emit("\n\n")
        elif tag == "a" and self.href:
            href = self.href.pop()
            if href and href.startswith(("http://", "https://")):
                self._emit(f" ({href})")
        elif tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()).replace("|", "\\|"))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.table_rows.append(self.row)
            self.row = None
        elif tag == "table":
            lines = []
            for i, r in enumerate(self.table_rows):
                lines.append("| " + " | ".join(r) + " |")
                if i == 0:
                    lines.append("|" + "---|" * len(r))
            self._emit("\n\n" + "\n".join(lines) + "\n\n")
            self.table_rows = []
        elif tag in _BLOCK:
            self._emit("\n\n")

    def handle_data(self, data):
        if self.skip:
            return
        self._emit(data if self.pre else re.sub(r"\s+", " ", data))


def main_content(html: str) -> str:
    """The <main> or <article> element's inner HTML when there is one, else the whole page."""
    for tag in ("main", "article"):
        m = re.search(rf"<{tag}\b[^>]*>(.*)</{tag}>", html, re.S | re.I)
        if m:
            return m.group(1)
    return html


def html_to_markdown(html: str) -> str:
    c = _Converter()
    c.feed(main_content(html))
    c.close()
    text = "".join(c.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() if not line.startswith(" ") else line.rstrip() for line in text.split("\n")).strip() + "\n"

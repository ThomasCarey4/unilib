"""Markdown documents (reference docs, specs, web snapshots): YAML frontmatter plus one
section per h1–h3 heading. A section's 1-based ordinal is its `page`, cited as `path §N`."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

MAX_SECTION_CHARS = 20000
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)


class MarkdownError(ValueError):
    pass


@dataclass(frozen=True)
class MdSection:
    num: int      # 1-based ordinal: the citation's §N and the index's page
    heading: str  # heading trail, e.g. "Compression › Order by" ("" before the first heading)
    anchor: str   # GitHub-style anchor of the last heading ("" if none)
    text: str     # the trail as the first line, then the body


def frontmatter(text: str) -> tuple[dict, str]:
    """(metadata, body). No frontmatter gives ({}, text)."""
    m = _FRONT.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise MarkdownError(f"bad frontmatter: {' '.join(str(e).split())[:120]}") from e
    if not isinstance(meta, dict):
        raise MarkdownError("frontmatter must be a mapping")
    return meta, text[m.end():]


def anchor(heading: str) -> str:
    """GitHub's heading anchor: lower case, punctuation dropped, spaces to '-'."""
    s = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return s.replace(" ", "-")


def read(path: Path) -> tuple[dict, str]:
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise MarkdownError(f"{Path(path).name}: cannot read ({e})") from e
    try:
        return frontmatter(text)
    except MarkdownError as e:
        raise MarkdownError(f"{Path(path).name}: {e}") from e


def _cap(text: str) -> str:
    return text[:MAX_SECTION_CHARS] + "\n…[truncated]" if len(text) > MAX_SECTION_CHARS else text


def sections(path: Path) -> list[MdSection]:
    """Sections of the body. A heading with no body of its own (followed directly by a
    subheading) does not make a section; its line stays in the next section's text."""
    _, body = read(path)
    trail: list[str] = []          # heading text per level 1..3
    raw: list[tuple[str, str, list[str]]] = []  # (trail, anchor, lines)
    cur: list[str] = []
    cur_trail, cur_anchor = "", ""
    carry: list[str] = []          # lines of heading-only sections, kept for the next one
    in_fence = False

    def flush() -> None:
        nonlocal cur, carry
        if any(line.strip() for line in cur):
            raw.append((cur_trail, cur_anchor, carry + cur))
            carry = []
        elif cur_trail:  # heading without body
            carry = carry + [f"{cur_trail.rsplit(' › ', 1)[-1]}"]
        cur = []

    for line in body.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line)
        if m is None:
            cur.append(line)
            continue
        flush()
        level, title = len(m.group(1)), m.group(2).strip()
        trail = trail[:level - 1] + [""] * max(0, level - 1 - len(trail)) + [title]
        cur_trail = " › ".join(t for t in trail if t)
        cur_anchor = anchor(title)
    flush()
    out = []
    for i, (t, a, lines) in enumerate(raw, 1):
        bodytext = "\n".join(lines).strip()
        out.append(MdSection(i, t, a, _cap(f"{t}\n{bodytext}" if t else bodytext)))
    return out


def section_count(path: Path) -> int:
    return len(sections(path))


def section_at(path: Path, num: int) -> MdSection:
    secs = sections(path)
    if not 1 <= num <= len(secs):
        raise ValueError(f"section {num} out of range 1-{len(secs)}")
    return secs[num - 1]

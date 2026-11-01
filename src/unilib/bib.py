"""Minimal BibTeX/BibLaTeX reader for Better BibTeX exports."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

_HEAD = re.compile(r"@(\w+)\s*\{")
_FIELD = re.compile(r"\s*([\w:.+-]+)\s*=\s*")
_SKIP_TYPES = {"comment", "string", "preamble"}
_ACCENTS = {"'": "\u0301", "`": "\u0300", "^": "\u0302", '"': "\u0308", "~": "\u0303", "=": "\u0304",
            ".": "\u0307", "c": "\u0327", "u": "\u0306", "v": "\u030c", "H": "\u030b"}
_FILE_DESC = re.compile(r"(.*?)(?<!\\):(.+)(?<!\\):([\w.+-]+/[\w.+-]+)")  # Zotero "desc:path:mime" form


@dataclass(frozen=True)
class BibEntry:
    key: str
    type: str
    fields: dict[str, str] = field(default_factory=dict)  # lower-case names, raw TeX values

    @property
    def title(self) -> str:
        return clean_tex(self.fields.get("title", ""))

    @property
    def year(self) -> int | None:
        m = re.search(r"\d{4}", self.fields.get("date") or self.fields.get("year") or "")
        return int(m[0]) if m else None

    @property
    def authors(self) -> list[str]:
        return [clean_tex(a) for a in _split_and(self.fields.get("author", "")) if a.strip()]

    @property
    def doi(self) -> str | None:
        return clean_tex(self.fields["doi"]) if self.fields.get("doi") else None


def _close(text: str, open_idx: int) -> int | None:
    """Index of the brace closing text[open_idx] == '{', honouring backslash escapes."""
    depth, i = 0, open_idx
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _fields(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    i = 0
    while i < len(body):
        while i < len(body) and body[i] in " \t\r\n,":
            i += 1
        m = _FIELD.match(body, i)
        if not m:
            break
        name, i = m[1].lower(), m.end()
        if i < len(body) and body[i] == "{":
            end = _close(body, i)
            if end is None:
                break
            value, i = body[i + 1:end], end + 1
        elif i < len(body) and body[i] == '"':
            j = i + 1
            depth = 0
            while j < len(body) and not (body[j] == '"' and depth == 0 and body[j - 1] != "\\"):
                depth += {"{": 1, "}": -1}.get(body[j], 0)
                j += 1
            value, i = body[i + 1:j], j + 1
        else:
            j = body.find(",", i)
            j = len(body) if j == -1 else j
            value, i = body[i:j].strip(), j
        out[name] = value
    return out


def parse_bib_text(text: str) -> list[BibEntry]:
    entries: list[BibEntry] = []
    i = 0
    while (m := _HEAD.search(text, i)) is not None:
        end = _close(text, m.end() - 1)
        if end is None:
            break
        i = end + 1
        typ = m[1].lower()
        if typ in _SKIP_TYPES:
            continue
        key, _, rest = text[m.end():end].partition(",")
        entries.append(BibEntry(key.strip(), typ, _fields(rest)))
    return entries


def parse_bib(path: Path) -> list[BibEntry]:
    return parse_bib_text(path.read_text(encoding="utf-8", errors="replace"))


def _split_and(raw: str) -> list[str]:
    """Split an author list on top-level ' and ' (not inside braces)."""
    parts, depth, start, i = [], 0, 0, 0
    while i < len(raw):
        ch = raw[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif depth == 0 and raw.startswith(" and ", i):
            parts.append(raw[start:i])
            start = i = i + 5
            continue
        i += 1
    parts.append(raw[start:])
    return [p.strip() for p in parts]


def _accent(m: re.Match) -> str:
    return unicodedata.normalize("NFC", m[2] + _ACCENTS[m[1]])


def clean_tex(s: str) -> str:
    """TeX value -> display text: accents to Unicode, drop formatting macros and braces."""
    s = re.sub(r"\\([`'^\"~=.])\s*\{?(\w)\}?", _accent, s)
    s = re.sub(r"\\([cuvH])\s*\{(\w)\}", _accent, s)
    s = re.sub(r"\\(?:emph|textit|textbf|textsc|textrm|mkbibquote|mathrm)\b\s*", "", s)
    for a, b in (("\\&", "&"), ("\\%", "%"), ("\\_", "_"), ("\\$", "$"), ("\\#", "#")):
        s = s.replace(a, b)
    s = re.sub(r"(?<!\\)[{}]", "", s)
    return " ".join(s.split())


def surname(author: str) -> str:
    """'Last, First' -> Last; 'First Last' -> Last."""
    a = author.strip()
    return a.split(",", 1)[0].strip() if "," in a else (a.split() or [""])[-1]


def files(entry: BibEntry) -> list[Path]:
    """Paths in BBT's `file` field: `a.pdf;b.pdf` or `desc:path:mime;…`, with `\\:`/`\\;` escapes."""
    out = []
    for part in re.split(r"(?<!\\);", entry.fields.get("file", "")):
        part = part.strip()
        if not part:
            continue
        if m := _FILE_DESC.fullmatch(part):
            part = m[2]
        out.append(Path(re.sub(r"\\([:;\\])", r"\1", part)).expanduser())
    return out


def pdf_for(entry: BibEntry) -> Path | None:
    return next((p for p in files(entry) if p.suffix.lower() == ".pdf" and p.is_file()), None)

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

MAX_SECTION_WORDS = 300
MAX_OUTPUT_CHARS = 1000
MAX_SECTION_CHARS = 20000


class NotebookError(ValueError):
    pass


@dataclass(frozen=True)
class Section:
    cell: int   # 1-based index of the section's first cell
    text: str


def _src(x) -> str:
    return "".join(x) if isinstance(x, list) else (x or "")


def _load(path: Path) -> list[dict]:
    try:
        nb = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if "cells" not in nb and "worksheets" in nb:
            raise NotebookError(f"{Path(path).name}: nbformat 3 notebooks are not supported; re-save in Jupyter")
        cells = nb["cells"]
        if not isinstance(cells, list) or not all(isinstance(c, dict) for c in cells):
            raise TypeError("cells must be a list of objects")
        return cells
    except NotebookError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise NotebookError(f"{Path(path).name}: not a valid notebook ({e})") from e


def _outputs(cell: dict) -> str:
    parts = []
    for o in cell.get("outputs") or []:
        if not isinstance(o, dict):
            continue
        t = o.get("output_type")
        if t == "stream":
            parts.append(_src(o.get("text")))
        elif t in ("execute_result", "display_data"):
            parts.append(_src(o.get("data", {}).get("text/plain")))
        elif t == "error":
            parts.append(f"{o.get('ename', '')}: {o.get('evalue', '')}")
    out = "\n".join(p for p in parts if p.strip())
    return out[:MAX_OUTPUT_CHARS]


def _cell_text(cell: dict) -> str:
    src = re.sub(r"data:[^\"')\s]+", "data:…", _src(cell.get("source"))).strip()
    if cell.get("cell_type") == "code":
        text = f"```python\n{src}\n```" if src else ""
        out = _outputs(cell)
        return f"{text}\n[output]\n{out}" if out else text
    return src


def cell_count(path: Path) -> int:
    return len(_load(path))


def _cap(text: str) -> str:
    return text[:MAX_SECTION_CHARS] + "\n…[truncated]" if len(text) > MAX_SECTION_CHARS else text


def sections(path: Path) -> list[Section]:
    try:
        return _sections(path)
    except (TypeError, AttributeError) as e:
        raise NotebookError(f"{Path(path).name}: not a valid notebook ({e})") from e


def _sections(path: Path) -> list[Section]:
    out: list[Section] = []
    start, buf, words = None, [], 0
    for i, cell in enumerate(_load(path), 1):
        text = _cell_text(cell)
        n = len(text.split())
        new = start is None or cell.get("cell_type") == "markdown" or (words and words + n > MAX_SECTION_WORDS)
        if new and start is not None:
            out.append(Section(start, _cap("\n\n".join(t for t in buf if t))))
            start, buf, words = None, [], 0
        if start is None:
            start = i
        buf.append(text)
        words += n
    if start is not None:
        out.append(Section(start, _cap("\n\n".join(t for t in buf if t))))
    return [s for s in out if s.text.strip()]


def section_at(path: Path, cell: int) -> Section:
    """The section starting at, or containing, the given 1-based cell."""
    n = cell_count(path)
    if not 1 <= cell <= n:
        raise ValueError(f"cell {cell} out of range 1-{n}")
    best = None
    for s in sections(path):
        if s.cell <= cell:
            best = s
    if best is None:
        raise ValueError(f"no content at or before cell {cell}")
    return best

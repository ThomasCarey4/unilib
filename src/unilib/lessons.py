"""Project lessons: measured findings, incidents and decisions written as markdown notes in the
projects' own repos (`docs/lessons/*.md`) and indexed as module LESSONS.

Frontmatter (YAML):
    id: <project>-<yyyy-mm-dd>-<slug>     lower case, digits, '.', '_', '-'
    title: one line
    project: serenlink                     lower case
    kind: incident | measurement | decision
    date: 2026-09-25
    versions: {postgresql: "16.10", ...}   optional; shown in citations like a source version
    tags: [...]                            optional
    refs: [...]                            optional; unilib citations backing the note
    status: current | superseded-by:<id>   optional, default current
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from .markdown import MarkdownError, read

KINDS = ("incident", "measurement", "decision")
_ID = re.compile(r"[a-z0-9][a-z0-9._-]*")
_PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]*")


@dataclass(frozen=True)
class Lesson:
    id: str
    title: str
    project: str
    kind: str
    date: str          # ISO date
    versions: str      # "postgresql 16.10, timescaledb 2.22.0" ('' if none)
    status: str        # "current" | "superseded-by:<id>"

    @property
    def superseded(self) -> bool:
        return self.status != "current"

    @property
    def year(self) -> int | None:
        return int(self.date[:4]) if self.date[:4].isdigit() else None


def parse_lesson(path: Path) -> Lesson:
    """The note's validated frontmatter; ValueError says what is wrong."""
    try:
        meta, body = read(path)
    except MarkdownError as e:
        raise ValueError(f"bad frontmatter: {e}") from e
    if not meta:
        raise ValueError("no frontmatter (see lessons.py for the format)")
    missing = [k for k in ("id", "title", "project", "kind", "date") if not meta.get(k)]
    if missing:
        raise ValueError(f"frontmatter lacks {', '.join(missing)}")
    lid, project, kind = str(meta["id"]), str(meta["project"]), str(meta["kind"])
    if not _ID.fullmatch(lid):
        raise ValueError(f"bad id {lid!r} (lower case, digits, '.', '_', '-')")
    if not _PROJECT.fullmatch(project):
        raise ValueError(f"bad project {project!r}")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
    date = meta["date"]
    if isinstance(date, (dt.date, dt.datetime)):
        date = date.isoformat()[:10]
    elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date)):
        raise ValueError(f"date must be YYYY-MM-DD, got {date!r}")
    versions = meta.get("versions") or {}
    if not isinstance(versions, dict):
        raise ValueError("versions must be a mapping, e.g. {postgresql: \"16.10\"}")
    status = str(meta.get("status") or "current")
    if status != "current" and not re.fullmatch(r"superseded-by:" + _ID.pattern, status):
        raise ValueError(f"status must be current or superseded-by:<id>, got {status!r}")
    if not body.strip():
        raise ValueError("empty note")
    return Lesson(lid, str(meta["title"]).strip(), project, kind, str(date),
                  ", ".join(f"{k} {v}" for k, v in versions.items()), status)

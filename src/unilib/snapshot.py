"""Snapshot web and git sources into the drop zone, for the normal /ingest flow.

`[[snapshot]]` tables in <library>/sources.toml say what to fetch:

    [[snapshot]]
    id = "tsdb-2.22-docs"          # output goes to _ingest/<id>/
    mode = "git"                   # pdf | git | html
    url = "https://github.com/timescale/docs"
    ref = "<tag or commit>"        # git only
    paths = ["use-timescale/compression/**/*.md"]   # git only: globs relative to the repo
    group = 1                      # git only: one markdown file per first-N-directories group
    urls = ["https://…", …]        # html only
    version = "TimescaleDB 2.22"   # written to each markdown file's frontmatter
    licence = "…"
    source_url = "https://github.com/timescale/docs/blob/<ref>"   # git: base for per-file Source: lines

Snapshots are private copies for this library: they stay on the server, outside any git repo.
The fetch date and each file's sha256 are recorded in <library>/.index/snapshots.json; a fetched
snapshot is not overwritten without refresh=True.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .htmlmd import html_to_markdown
from .markdown import MarkdownError, frontmatter
from .organize import kebab

MODES = ("pdf", "git", "html")
MAX_FILES = 99  # D01…D99 per module
_ID = re.compile(r"[a-z0-9][a-z0-9._-]*")
_KEYS = {"id", "mode", "url", "urls", "ref", "paths", "group", "version", "licence", "source_url", "notes"}
USER_AGENT = "unilib-snapshot/1 (personal reference library)"


@dataclass(frozen=True)
class Snapshot:
    id: str
    mode: str
    url: str | None = None
    urls: tuple[str, ...] = ()
    ref: str | None = None
    paths: tuple[str, ...] = ()
    group: int = 1
    version: str | None = None
    licence: str | None = None
    source_url: str | None = None


def load_snapshots(root: Path) -> list[Snapshot]:
    path = root / "sources.toml"
    if not path.is_file():
        return []
    out: list[Snapshot] = []
    for i, e in enumerate(tomllib.loads(path.read_text()).get("snapshot", []), 1):
        where = f"sources.toml [[snapshot]] #{i}"
        if unknown := set(e) - _KEYS:
            raise ValueError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
        sid, mode = str(e.get("id", "")), e.get("mode")
        if not _ID.fullmatch(sid):
            raise ValueError(f"{where}: bad id {sid!r} (lower case, digits, '.', '_', '-')")
        if mode not in MODES:
            raise ValueError(f"{where}: mode must be one of {', '.join(MODES)}")
        if mode in ("pdf", "git") and not e.get("url"):
            raise ValueError(f"{where}: mode {mode} needs url")
        if mode == "git" and not (e.get("ref") and e.get("paths")):
            raise ValueError(f"{where}: mode git needs ref and paths")
        if mode == "html" and not e.get("urls"):
            raise ValueError(f"{where}: mode html needs urls = [\"https://…\", …]")
        group = e.get("group", 1)
        if not isinstance(group, int) or group < 0:
            raise ValueError(f"{where}: group must be a non-negative integer")
        if any(s.id == sid for s in out):
            raise ValueError(f"{where}: duplicate id {sid!r}")
        opt = lambda k: str(e[k]) if e.get(k) is not None else None
        out.append(Snapshot(sid, mode, opt("url"), tuple(map(str, e.get("urls", ()))), opt("ref"),
                            tuple(map(str, e.get("paths", ()))), group, opt("version"), opt("licence"),
                            opt("source_url")))
    return out


def _state_path(root: Path) -> Path:
    return root / ".index" / "snapshots.json"


def load_state(root: Path) -> dict:
    p = _state_path(root)
    return json.loads(p.read_text()) if p.is_file() else {}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 — URLs come from the library owner's sources.toml
        return r.read()


def _front(meta: dict) -> str:
    lines = ["---"]
    for k, v in meta.items():
        if v is not None:
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")  # JSON strings are valid YAML
    return "\n".join(lines + ["---", ""])


_MDX_LINE = re.compile(r"^\s*(import\s.+\sfrom\s.+|export\s.+)$")
_JSX_TAG = re.compile(r"</?[A-Z][A-Za-z0-9.]*(\s[^<>]*)?/?>")


def clean_mdx(text: str) -> str:
    """Drop MDX import/export lines and JSX component tags, keeping their inner text."""
    out, fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        if not fence:
            if _MDX_LINE.match(line):
                continue
            line = _JSX_TAG.sub("", line)
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def _demote(text: str) -> str:
    """Shift markdown headings down one level (outside code fences), so a file's title can be the h1."""
    out, fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        if not fence and re.match(r"#{1,5}\s", line):
            line = "#" + line
        out.append(line)
    return "\n".join(out)


def _git_snapshot(s: Snapshot, dest: Path, fetched: str, log: Callable[[str], None]) -> list[Path]:
    with tempfile.TemporaryDirectory(prefix="unilib-snap-") as tmp:
        repo = Path(tmp) / "repo"
        run = lambda *a: subprocess.run(["git", *a], check=True, capture_output=True, text=True)
        log(f"cloning {s.url} @ {s.ref}")
        run("clone", "-q", "--filter=blob:none", "--no-checkout", s.url, str(repo))
        run("-C", str(repo), "-c", "advice.detachedHead=false", "checkout", "-q", s.ref)
        commit = run("-C", str(repo), "rev-parse", "HEAD").stdout.strip()
        files = sorted({p for g in s.paths for p in repo.glob(g)
                        if p.is_file() and p.suffix.lower() in (".md", ".mdx")})
        groups: dict[tuple[str, ...], list[Path]] = {}
        for f in files:
            parts = f.relative_to(repo).parts
            key = parts[:min(s.group, len(parts) - 1)] or (Path(parts[-1]).stem,)
            groups.setdefault(key, []).append(f)
        if len(groups) > MAX_FILES:
            raise ValueError(f"{len(groups)} groups > {MAX_FILES}: raise `group` or narrow `paths`")
        base = (s.source_url or s.url).rstrip("/")
        out = []
        for key, members in sorted(groups.items()):
            title = " ".join(key).replace("-", " ").replace("_", " ").strip().title()
            parts = []
            for f in members:
                rel = f.relative_to(repo).as_posix()
                try:
                    meta, body = frontmatter(f.read_text(encoding="utf-8", errors="replace"))
                except MarkdownError:
                    meta, body = {}, f.read_text(encoding="utf-8", errors="replace")
                ftitle = str(meta.get("title") or Path(rel).stem.replace("-", " ").title())
                parts.append(f"# {ftitle}\n\nSource: {base}/{rel}\n\n{_demote(clean_mdx(body)).strip()}\n")
            doc = _front({"title": f"{title}", "version": s.version, "licence": s.licence, "fetched": fetched,
                          "snapshot": s.id, "ref": s.ref, "commit": commit}) + "\n".join(parts)
            path = dest / f"{kebab(title) or 'Index'}.md"
            path.write_text(doc)
            out.append(path)
        log(f"{len(files)} files from {commit[:12]} → {len(out)} markdown documents")
        return out


def _html_snapshot(s: Snapshot, dest: Path, fetched: str, log: Callable[[str], None]) -> list[Path]:
    out = []
    for url in s.urls:
        log(f"fetching {url}")
        html = _fetch(url).decode("utf-8", errors="replace")
        md = html_to_markdown(html)
        m = re.search(r"^# (.+)$", md, re.M)
        title = m.group(1).strip() if m else Path(url.rstrip("/")).stem
        path = dest / f"{kebab(title) or 'Page'}.md"
        n = 2
        while path.exists():
            path = dest / f"{kebab(title)}-{n}.md"
            n += 1
        path.write_text(_front({"title": title, "source_url": url, "version": s.version,
                                "licence": s.licence, "fetched": fetched, "snapshot": s.id}) + "\n" + md)
        out.append(path)
    return out


def _pdf_snapshot(s: Snapshot, dest: Path, fetched: str, log: Callable[[str], None]) -> list[Path]:
    log(f"fetching {s.url}")
    data = _fetch(s.url)
    if not data.startswith(b"%PDF"):
        raise ValueError(f"{s.url} did not return a PDF")
    path = dest / f"{s.id}.pdf"
    path.write_bytes(data)
    return [path]


def run_snapshot(root: Path, sid: str, refresh: bool = False, log: Callable[[str], None] = print) -> list[Path]:
    """Fetch one snapshot into <root>/_ingest/<id>/ and record it; returns the files written."""
    snaps = {s.id: s for s in load_snapshots(root)}
    if sid not in snaps:
        raise KeyError(f"no [[snapshot]] {sid!r} in sources.toml (known: {', '.join(snaps) or 'none'})")
    s = snaps[sid]
    dest = root / "_ingest" / sid
    state = load_state(root)
    if (sid in state or (dest.exists() and any(dest.iterdir()))) and not refresh:
        raise FileExistsError(f"snapshot {sid} was already fetched ({state.get(sid, {}).get('fetched', '?')}); "
                              "pass --refresh (refresh=True) to fetch it again")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    fetched = dt.date.today().isoformat()
    try:
        files = {"pdf": _pdf_snapshot, "git": _git_snapshot, "html": _html_snapshot}[s.mode](s, dest, fetched, log)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    state[sid] = {"fetched": fetched, "mode": s.mode, "files": {p.name: _sha(p) for p in files}}
    _state_path(root).parent.mkdir(parents=True, exist_ok=True)
    _state_path(root).write_text(json.dumps(state, indent=2, sort_keys=True))
    return files

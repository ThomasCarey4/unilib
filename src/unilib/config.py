from __future__ import annotations

import fnmatch
import glob
import os
import re
import tomllib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class Module:
    code: str
    name: str
    semester: int | None
    folder: str
    keywords: tuple[str, ...] = ()


# Set per request by the server when it serves several libraries (one endpoint each).
_current_root: ContextVar[Path | None] = ContextVar("unilib_root", default=None)


def library_root() -> Path:
    """The library this call acts on: the request's library, else $UNILIB_ROOT, else ~/Documents/UniLibrary."""
    if (root := _current_root.get()) is not None:
        return root
    default = Path.home() / "Documents" / "UniLibrary"
    return Path(os.environ.get("UNILIB_ROOT") or default).expanduser().resolve()


@contextmanager
def using_library(root: Path) -> Iterator[Path]:
    """Make `library_root()` return `root` in this context (and contexts copied from it)."""
    root = Path(root).expanduser().resolve()
    token = _current_root.set(root)
    try:
        yield root
    finally:
        _current_root.reset(token)


@dataclass(frozen=True)
class Library:
    name: str  # endpoint name: the first library is served at /mcp, the others at /<name>/mcp
    root: Path


_LIBRARY_NAME = re.compile(r"[a-z][a-z0-9-]*")
RESERVED_LIBRARY_NAMES = frozenset({"all", "lesson", "zotero"})  # "all": every library; the others are path prefixes


def libraries() -> list[Library]:
    """$UNILIB_LIBRARIES (`name=path,name=path`, first one is the default), else one library
    named `uni` at `library_root()`."""
    spec = os.environ.get("UNILIB_LIBRARIES", "").strip()
    if not spec:
        return [Library("uni", library_root())]
    out: list[Library] = []
    for item in (s.strip() for s in spec.split(",")):
        name, sep, path = item.partition("=")
        name, path = name.strip(), path.strip()
        if not sep:
            raise ValueError(f"UNILIB_LIBRARIES: expected name=path, got {item!r}")
        if not _LIBRARY_NAME.fullmatch(name):
            raise ValueError(f"UNILIB_LIBRARIES: bad library name {name!r} (lowercase letters, digits, '-')")
        if name in RESERVED_LIBRARY_NAMES:
            raise ValueError(f"UNILIB_LIBRARIES: {name!r} is reserved")
        if not path:
            raise ValueError(f"UNILIB_LIBRARIES: library {name!r} has no path")
        if any(lib.name == name for lib in out):
            raise ValueError(f"UNILIB_LIBRARIES: duplicate library {name!r}")
        out.append(Library(name, Path(path).expanduser().resolve()))
    return out


@dataclass(frozen=True)
class LibrarySettings:
    title: str | None = None     # shown in the endpoint's instructions
    books_default: bool = False  # search includes B files unless told otherwise (True where books are primary)


def load_library_settings(root: Path) -> LibrarySettings:
    """The optional [library] table of modules.toml."""
    t = tomllib.loads((root / "modules.toml").read_text()).get("library", {})
    books = t.get("books_default", False)
    if not isinstance(books, bool):
        raise ValueError(f"modules.toml: [library] books_default must be true or false, got {books!r}")
    title = t.get("title")
    return LibrarySettings(title=str(title) if title is not None else None, books_default=books)


def model_cache_dir() -> Path:
    default = Path.home() / ".cache" / "unilib" / "models"
    return Path(os.environ.get("UNILIB_MODEL_CACHE") or default).expanduser()


def cache_dir() -> Path:
    """Small client-side caches (bibcheck.json)."""
    return Path(os.environ.get("UNILIB_CACHE") or Path.home() / ".cache" / "unilib").expanduser()


def client_config_path() -> Path:
    default = Path.home() / ".config" / "unilib" / "client.toml"
    return Path(os.environ.get("UNILIB_CLIENT_CONFIG") or default).expanduser()


def db_path(root: Path) -> Path:
    return root / ".index" / "library.db"


def load_modules(root: Path) -> list[Module]:
    data = tomllib.loads((root / "modules.toml").read_text())
    entries = data.get("module")
    if not entries:
        raise ValueError(f"{root / 'modules.toml'}: no [[module]] table found")
    mods = [
        Module(
            code=m["code"],
            name=m["name"],
            semester=int(m["semester"]) if m.get("semester") is not None else None,
            folder=m["folder"],
            keywords=tuple(m.get("keywords", [])),
        )
        for m in entries
    ]
    seen_codes: set[str] = set()
    seen_folders: set[str] = set()
    for m in mods:
        if m.code.upper() in seen_codes:
            raise ValueError(f"modules.toml: duplicate code {m.code!r}")
        if m.folder in seen_folders:
            raise ValueError(f"modules.toml: duplicate folder {m.folder!r}")
        seen_codes.add(m.code.upper())
        seen_folders.add(m.folder)
    return mods


def module_by_code(root: Path, code: str) -> Module:
    for m in load_modules(root):
        if m.code.upper() == code.upper():
            return m
    raise KeyError(f"unknown module {code!r}")


@dataclass(frozen=True)
class SourceMeta:
    """Where a library file came from, from <library>/sources.toml (or a markdown file's frontmatter)."""
    version: str | None = None     # e.g. "PostgreSQL 16.10": shown in every citation of the file
    source_url: str | None = None
    licence: str | None = None
    superseded: bool = False       # an older version kept for reference: excluded from search by default


_SOURCE_KEYS = {"files", "version", "source_url", "licence", "fetched", "sha256", "superseded", "notes"}


def load_sources(root: Path) -> list[tuple[tuple[str, ...], SourceMeta]]:
    """[[source]] entries of <root>/sources.toml as (globs relative to root, meta); [] without the file.
    `fetched`, `sha256` and `notes` are bookkeeping for people and the snapshot tool."""
    path = root / "sources.toml"
    if not path.is_file():
        return []
    out = []
    for i, e in enumerate(tomllib.loads(path.read_text()).get("source", []), 1):
        where = f"sources.toml [[source]] #{i}"
        if not isinstance(e, dict):
            raise ValueError(f"{where}: not a table")
        if unknown := set(e) - _SOURCE_KEYS:
            raise ValueError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
        files = e.get("files")
        if isinstance(files, str):
            files = [files]
        if not files or not all(isinstance(f, str) and f for f in files):
            raise ValueError(f"{where}: needs files = [\"<glob relative to the library>\", …]")
        if not isinstance(e.get("superseded", False), bool):
            raise ValueError(f"{where}: superseded must be true or false")
        meta = SourceMeta(*(str(e[k]) if e.get(k) is not None else None for k in ("version", "source_url", "licence")),
                          superseded=e.get("superseded", False))
        out.append((tuple(files), meta))
    return out


def source_meta(entries: list[tuple[tuple[str, ...], SourceMeta]], rel: str) -> SourceMeta:
    """The first entry whose glob matches the library-relative path, else empty metadata."""
    for globs, meta in entries:
        if any(fnmatch.fnmatchcase(rel, g) for g in globs):
            return meta
    return SourceMeta()


LESSONS = "LESSONS"  # pseudo-module code under which project lessons are indexed
LESSON_PREFIX = "lesson:"  # citation and get_page path prefix: lesson:<id>


def load_lessons(root: Path) -> tuple[Path, ...]:
    """Markdown files matching [lessons] globs in modules.toml right now (none without the table)."""
    mt = root / "modules.toml"
    if not mt.is_file():
        return ()
    t = tomllib.loads(mt.read_text()).get("lessons")
    if not t:
        return ()
    globs = t.get("globs")
    if isinstance(globs, str):
        globs = [globs]
    if not globs or not all(isinstance(g, str) and g for g in globs):
        raise ValueError("modules.toml: [lessons] needs globs = [\"~/repo/docs/lessons/*.md\", …]")
    found: dict[Path, None] = {}
    for g in globs:
        for p in sorted(glob.glob(os.path.expanduser(g))):
            path = Path(p)
            if path.suffix.lower() == ".md" and path.is_file():
                found.setdefault(path.resolve(), None)
    return tuple(found)


PAPERS = "PAPERS"  # pseudo-module code under which Zotero papers are indexed
_REPORT_NAME = re.compile(r"[\w.-]+")  # stored comma-joined in docs.reports, and used as a Zotero collection name


REPORT_MARKER = ".unilib-report"  # sync-report writes it: only marked folders' refs.bib count as reports
ZOTERO_PREFIX = "zotero:"  # docs.path prefix for PDFs under the Zotero data dir, resolved per machine


@dataclass(frozen=True)
class Report:
    name: str  # the report folder's name
    bib: Path


@dataclass(frozen=True)
class PapersConfig:
    bib: Path
    reports_glob: str | None = None  # e.g. "~/Documents/LaTeX/*/refs.bib"
    zotero_dir: Path = Path("~/Zotero").expanduser()

    def reports(self) -> tuple[Report, ...]:
        """Report bibs matching the glob right now whose folder holds REPORT_MARKER (written by sync-report,
        so a stray refs.bib is never a report), by folder name; folders with bad names are skipped."""
        if not self.reports_glob:
            return ()
        found: dict[str, Report] = {}
        for p in sorted(glob.glob(os.path.expanduser(self.reports_glob))):
            name = Path(p).parent.name
            if _REPORT_NAME.fullmatch(name) and name not in found and (Path(p).parent / REPORT_MARKER).is_file():
                found[name] = Report(name, Path(p))
        return tuple(found.values())


DEFAULT_UNI_BIB = "~/Zotero/exports/uni.bib"  # Better BibTeX auto-export of the Uni collection
DEFAULT_REPORTS = "~/Documents/LaTeX/*/refs.bib"


def _papers(p: dict, source: str) -> PapersConfig:
    if not p.get("bib"):
        raise ValueError(f"{source}: [papers] needs bib")
    return PapersConfig(Path(p["bib"]).expanduser(), p.get("reports"),
                        Path(p.get("zotero_dir", "~/Zotero")).expanduser())


def load_papers(root: Path) -> PapersConfig | None:
    """The optional [papers] section of the server's modules.toml (what the index reads), or None."""
    p = tomllib.loads((root / "modules.toml").read_text()).get("papers")
    return None if p is None else _papers(p, "modules.toml")


def _client_toml(path: Path | None) -> tuple[dict, Path]:
    path = path or client_config_path()
    return (tomllib.loads(path.read_text()) if path.is_file() else {}), path


def client_papers(path: Path | None = None) -> PapersConfig:
    """This machine's Zotero layout: [papers] of the client config, else the defaults."""
    data, path = _client_toml(path)
    return _papers({"bib": DEFAULT_UNI_BIB, "reports": DEFAULT_REPORTS, **data.get("papers", {})}, str(path))


@dataclass(frozen=True)
class WebDav:
    url: str  # library root on the server's WebDAV, e.g. http://host:8091
    user: str
    password_file: Path


def client_webdav(path: Path | None = None) -> WebDav:
    data, path = _client_toml(path)
    w = data.get("webdav")
    if not w or not all(w.get(k) for k in ("url", "user", "password_file")):
        raise ValueError(f"{path}: [webdav] needs url, user and password_file")
    return WebDav(w["url"].rstrip("/"), w["user"], Path(w["password_file"]).expanduser())


def valid_report_name(name: str) -> bool:
    return _REPORT_NAME.fullmatch(name) is not None


def paper_rel(papers: PapersConfig, pdf: Path) -> str:
    """docs.path for a paper: `zotero:<path under zotero_dir>` (same on every machine), else absolute."""
    pdf = pdf.resolve()
    try:
        return ZOTERO_PREFIX + pdf.relative_to(papers.zotero_dir.resolve()).as_posix()
    except ValueError:
        return str(pdf)


def paper_file(papers: PapersConfig, rel: str) -> Path:
    """Inverse of paper_rel on this machine."""
    if rel.startswith(ZOTERO_PREFIX):
        return papers.zotero_dir / rel.removeprefix(ZOTERO_PREFIX)
    return Path(rel)

from __future__ import annotations

import glob
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Module:
    code: str
    name: str
    semester: int
    folder: str
    keywords: tuple[str, ...] = ()


def library_root() -> Path:
    default = Path.home() / "Documents" / "UniLibrary"
    return Path(os.environ.get("UNILIB_ROOT") or default).expanduser().resolve()


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
            semester=int(m["semester"]),
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

"""Check .bib entries against Crossref (by DOI) or OpenAlex (by title)."""
from __future__ import annotations

import difflib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .bib import BibEntry, clean_tex, parse_bib, pdf_for, surname
from .config import PapersConfig, cache_dir
from .textnorm import fold

CROSSREF = "https://api.crossref.org/works/"
OPENALEX = "https://api.openalex.org/works"
DATACITE = "https://api.datacite.org/dois/"  # arXiv (10.48550/…) and other non-Crossref DOIs
TITLE_RATIO = 0.9  # normalised-title similarity at or above which bib and remote titles count as the same
NO_LOOKUP_TYPES = {"online", "software", "dataset", "misc", "manual"}  # usually no DOI; checked by hand

Fetch = Callable[[str], dict | None]


@dataclass(frozen=True)
class Check:
    key: str
    status: str  # "OK" | "MISMATCH" | "NOT FOUND" | "SKIPPED" | "ERROR"
    notes: tuple[str, ...] = ()
    pdf: bool | None = None  # local PDF in Zotero; None when unknown


@dataclass(frozen=True)
class _Remote:
    title: str
    year: int | None
    surname: str
    doi: str | None


def fetch_json(url: str, timeout: float = 15) -> dict | None:
    """GET JSON; None on HTTP 404. Other failures raise."""
    agent = "unilib-bibcheck/0.1"
    if mailto := os.environ.get("UNILIB_MAILTO"):  # Crossref "polite pool"; unset by default
        agent += f" (mailto:{mailto})"
    req = urllib.request.Request(url, headers={"User-Agent": agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _norm(s: str) -> str:
    return " ".join(re.findall(r"\w+", fold(clean_tex(s)).lower()))


def title_ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _crossref(msg: dict) -> _Remote:
    year = ((msg.get("issued") or {}).get("date-parts") or [[None]])[0][0]
    return _Remote((msg.get("title") or [""])[0], year, ((msg.get("author") or [{}])[0]).get("family", ""),
                   msg.get("DOI"))


def _datacite(data: dict) -> _Remote:
    a = (data.get("data") or {}).get("attributes") or {}
    creator = (a.get("creators") or [{}])[0]
    year = a.get("publicationYear")
    return _Remote(((a.get("titles") or [{}])[0]).get("title", ""), int(year) if year else None,
                   creator.get("familyName") or surname(creator.get("name", "")), a.get("doi"))


def _openalex(work: dict) -> _Remote:
    name = (((work.get("authorships") or [{}])[0]).get("author") or {}).get("display_name", "")
    doi = (work.get("doi") or "").removeprefix("https://doi.org/") or None
    return _Remote(work.get("title") or "", work.get("publication_year"), surname(name), doi)


def _compare(entry: BibEntry, remote: _Remote) -> list[str]:
    notes = []
    if title_ratio(entry.title, remote.title) < TITLE_RATIO:
        notes.append(f"title: {entry.title!r} vs {remote.title!r}")
    if entry.year and remote.year and entry.year != remote.year:
        notes.append(f"year: {entry.year} vs {remote.year}")
    mine = surname(entry.authors[0]) if entry.authors else ""
    if mine and remote.surname and _norm(mine) != _norm(remote.surname):
        notes.append(f"first author: {mine} vs {remote.surname}")
    return notes


def check_entry(entry: BibEntry, fetch: Fetch) -> Check:
    try:
        if entry.doi:
            doi = urllib.parse.quote(entry.doi, safe="/")
            if (data := fetch(CROSSREF + doi)) is not None:
                remote = _crossref(data.get("message") or {})
            elif (data := fetch(DATACITE + doi)) is not None:
                remote = _datacite(data)
            else:
                return Check(entry.key, "NOT FOUND", (f"DOI {entry.doi} does not resolve on Crossref or DataCite",))
            notes = _compare(entry, remote)
            return Check(entry.key, "MISMATCH" if notes else "OK", tuple(notes))
        if entry.type in NO_LOOKUP_TYPES:
            return Check(entry.key, "SKIPPED", (f"@{entry.type} without DOI",))
        if not entry.title:
            return Check(entry.key, "NOT FOUND", ("no DOI and no title",))
        data = fetch(OPENALEX + "?" + urllib.parse.urlencode({"search": entry.title, "per-page": 1}))
        results = (data or {}).get("results") or []
        if not results:
            return Check(entry.key, "NOT FOUND", ("no DOI; no OpenAlex match for the title",))
        remote = _openalex(results[0])
        if title_ratio(entry.title, remote.title) < TITLE_RATIO:
            return Check(entry.key, "NOT FOUND", (f"no DOI; closest OpenAlex match: {remote.title!r}",))
        notes = _compare(entry, remote)
        status = "MISMATCH" if notes else "OK"
        if remote.doi:
            notes.append(f"suggest doi = {remote.doi}")
        return Check(entry.key, status, tuple(notes))
    except Exception as e:  # network/JSON trouble is per entry, never fatal
        return Check(entry.key, "ERROR", (" ".join(f"{type(e).__name__}: {e}".split())[:160],))


def _negative(data: dict | None) -> bool:
    """A miss (404 or empty OpenAlex search). Never cached: the DOI or title may be registered later."""
    return data is None or (isinstance(data, dict) and "results" in data and not data["results"])


def _cached(fetch: Fetch, path: Path) -> tuple[Fetch, Callable[[], None]]:
    try:
        cache = {k: v for k, v in json.loads(path.read_text()).items() if not _negative(v)}
    except (OSError, ValueError, AttributeError):
        cache = {}

    def cached(url: str) -> dict | None:
        if url in cache:
            return cache[url]
        data = fetch(url)  # exceptions propagate; misses and failures are re-fetched on the next run
        if not _negative(data):
            cache[url] = data
        return data

    def save() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache))
        tmp.replace(path)

    return cached, save


def _local_pdfs(papers: PapersConfig | None) -> dict[str, bool] | None:
    if papers is None or not papers.bib.is_file():
        return None
    return {e.key: pdf_for(e) is not None for e in parse_bib(papers.bib)}


def check_bib(papers: PapersConfig | None, bib: Path, fetch: Fetch | None = None,
              cache: Path | None = None) -> list[Check]:
    """`papers` (this machine's Zotero export) marks entries without a local PDF; None skips that."""
    cached, save = _cached(fetch or fetch_json, cache or cache_dir() / "bibcheck.json")
    local = _local_pdfs(papers)
    out = []
    try:
        for entry in parse_bib(bib):
            c = check_entry(entry, cached)
            if local is not None and local.get(entry.key):
                has_pdf = True
            elif "file" in entry.fields:  # e.g. a report's refs.bib citing a paper that is not in Uni
                has_pdf = pdf_for(entry) is not None
            else:
                has_pdf = False if local is not None else None
            out.append(Check(c.key, c.status, c.notes, has_pdf))
    finally:
        save()
    return out


def format_report(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        pdf = "  [no PDF]" if c.pdf is False else ""
        notes = "; ".join(c.notes)
        lines.append(f"{c.status:<9} @{c.key}{pdf}" + (f"  — {notes}" if notes else ""))
    counts = {s: sum(c.status == s for c in checks) for s in ("OK", "MISMATCH", "NOT FOUND", "SKIPPED", "ERROR")}
    no_pdf = sum(c.pdf is False for c in checks)
    lines.append(f"\n{len(checks)} entries: " + ", ".join(f"{n} {s}" for s, n in counts.items() if n)
                 + (f"; {no_pdf} without local PDF" if no_pdf else ""))
    return "\n".join(lines)

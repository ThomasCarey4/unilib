"""Drive Zotero through Better BibTeX's local JSON-RPC (Zotero must be running)."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .bib import parse_bib, pdf_for
from .config import REPORT_MARKER, PapersConfig, valid_report_name

RPC_URL = "http://127.0.0.1:23119/better-bibtex/json-rpc"
TRANSLATOR = "Better BibLaTeX"
UNI = "//Uni"  # BBT collection path: empty library name = My Library
_AUX = re.compile(r"(\\citation|\\abx@aux@cite|\\@input)\s*\{(.*?)\}(?:\{(.*?)\})?")  # same commands BBT's AUX scanner reads

Rpc = Callable[..., object]


class ZoteroError(RuntimeError):
    pass


NOT_RUNNING = "Zotero is not running (or Better BibTeX is not installed) — start Zotero and retry"


def rpc(method: str, *params, timeout: float = 120) -> object:
    url = os.environ.get("UNILIB_ZOTERO_URL") or RPC_URL
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": list(params), "id": 1}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    slow = f"{method}: Zotero did not answer within {timeout:.0f} s"
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except TimeoutError as e:
        raise ZoteroError(slow) from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise ZoteroError(slow) from e
        raise ZoteroError(NOT_RUNNING) from e
    except OSError as e:
        raise ZoteroError(NOT_RUNNING) from e
    try:
        reply = json.loads(raw)
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        raise ZoteroError(f"{method}: HTTP {status}" if status >= 400
                          else f"{method}: unexpected reply (HTTP {status}, not a JSON object)")
    if reply.get("error"):
        raise ZoteroError(f"{method}: {reply['error'].get('message', reply['error'])}")
    if status >= 400:
        raise ZoteroError(f"{method}: HTTP {status}")
    if "result" not in reply:
        raise ZoteroError(f"{method}: unexpected reply (no result)")
    return reply["result"]


def aux_keys(aux: Path) -> list[str]:
    """Cited (incl. \\nocite) keys in an .aux file and the .aux files it \\@inputs, in order, deduplicated."""
    keys: list[str] = []
    for cmd, a, b in _AUX.findall(aux.read_text(errors="replace")):
        if cmd == "\\@input":
            if (sub := aux.parent / a).is_file():
                keys += aux_keys(sub)
            continue
        if cmd == "\\abx@aux@cite" and a.isdigit() and b:
            a = b
        keys += [k.strip() for k in a.split(",") if k.strip() and k.strip() != "*"]
    return list(dict.fromkeys(keys))


def find_aux(report_dir: Path) -> Path:
    """Newest .aux (anywhere under report_dir, e.g. build/) belonging to a top-level .tex with \\documentclass."""
    mains = [t.stem for t in report_dir.glob("*.tex") if "\\documentclass" in t.read_text(errors="replace")]
    auxes = [a for stem in mains for a in report_dir.rglob(f"{stem}.aux")]
    if not auxes:
        raise ZoteroError(f"no .aux for {', '.join(m + '.tex' for m in mains) or 'a main .tex'} in {report_dir}"
                          " — compile the report once (e.g. latexmk) and retry")
    return max(auxes, key=lambda p: p.stat().st_mtime)


@dataclass(frozen=True)
class SyncResult:
    name: str
    aux: Path
    bib: Path
    cited: list[str]
    missing: list[str]
    tagged: bool  # refs.bib matches [papers] reports under this name, so the index tags its papers


def sync_report(papers: PapersConfig | None, report_dir: Path, name: str | None = None, rpc: Rpc = rpc) -> SyncResult:
    """Make Zotero collection Uni/<name> hold exactly the report's cited keys, keep <report>/refs.bib
    auto-exported from it (on this machine: BBT auto-exports are per machine), write refs.bib now, and
    opt the folder in as a report (REPORT_MARKER, unless name differs from the folder). Runs where the report and Zotero are (unilib-local / unilib-client)."""
    report_dir = report_dir.expanduser().resolve()
    name = name or report_dir.name
    if not valid_report_name(name):
        raise ValueError(f"bad report name {name!r} (letters, digits, _ . - only)")
    aux = find_aux(report_dir)
    keys = aux_keys(aux)
    coll, bib = f"{UNI}/{name}", report_dir / "refs.bib"
    rpc("collection.scanAUX", coll, str(aux))
    rpc("autoexport.add", coll, TRANSLATOR, str(bib), {}, False)
    missing: list[str] = []
    text = ""
    if keys:
        try:
            text = rpc("item.export", keys, TRANSLATOR)
        except ZoteroError as e:
            m = re.search(r"not found: ([^\n]*)", str(e))
            if not m:
                raise
            missing = [k.strip() for k in m[1].split(",") if k.strip()]
            found = [k for k in keys if k not in missing]
            text = rpc("item.export", found, TRANSLATOR) if found else ""
    if not isinstance(text, str):
        raise ZoteroError(f"item.export: unexpected reply type {type(text).__name__}")
    bib.write_text(text)  # immediate; BBT's auto-export rewrites the same content shortly after
    if name == report_dir.name:  # the index tags by folder name, so a renamed report is never marked
        (report_dir / REPORT_MARKER).write_text(
            "Written by `unilib zotero sync-report`: refs.bib here is a unilib report. Commit it with the "
            "report. To opt out, delete it and stop running sync-report here (each run rewrites it).\n")
    tagged = papers is not None and any(r.name == name and r.bib.resolve() == bib for r in papers.reports())
    return SyncResult(name, aux, bib, keys, missing, tagged)


def setup(papers: PapersConfig, rpc: Rpc = rpc) -> Path:
    """Once per machine: keep-updated Better BibLaTeX export of Uni to `papers.bib` (BBT auto-exports
    are per machine). Returns the export path."""
    papers.bib.parent.mkdir(parents=True, exist_ok=True)  # BBT writes the export file on autoexport.add
    rpc("autoexport.add", UNI, TRANSLATOR, str(papers.bib), {}, False)
    return papers.bib


@dataclass(frozen=True)
class Found:
    citekey: str
    title: str
    year: int | None
    in_uni: bool
    has_pdf: bool


def find(papers: PapersConfig | None, terms: str, rpc: Rpc = rpc, limit: int = 20) -> list[Found]:
    """Zotero quick search (title/creator/year/citekey) marked with Uni membership and local PDF."""
    uni = {e.key: e for e in parse_bib(papers.bib)} if papers and papers.bib.is_file() else {}
    out = []
    # a condition list, not the bare string: BBT 9.0.74 on Zotero 9 fails string quick search with
    # "Invalid condition 'groupStart'"; this condition is the same quick search (also matches citekeys)
    for item in (rpc("item.search", [["quicksearch-titleCreatorYear", "contains", terms]]) or [])[:limit]:
        key = item.get("citekey", "")
        year = (((item.get("issued") or {}).get("date-parts") or [[None]])[0] or [None])[0]
        e = uni.get(key)
        out.append(Found(key, item.get("title", ""), int(year) if year else None,
                         e is not None, e is not None and pdf_for(e) is not None))
    return out

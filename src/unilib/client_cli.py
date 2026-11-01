"""unilib-client: commands that act on this machine (Zotero, report .bib files, WebDAV uploads).

Imports nothing heavy, so it installs without the `server` extra. Paths come from the client
config (`~/.config/unilib/client.toml`, or $UNILIB_CLIENT_CONFIG): [papers] for Zotero, [webdav]
for `drop`.
"""
from __future__ import annotations

import argparse
import base64
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import zotero
from .bibcheck import check_bib, format_report
from .config import client_papers, client_webdav

DROP_SUFFIXES = {".pdf", ".pptx", ".ipynb"}


def cmd_check_bib(args) -> int:
    print(format_report(check_bib(client_papers(), args.file)))
    return 0


def cmd_zotero_setup(args) -> int:
    bib = zotero.setup(client_papers(), rpc=zotero.rpc)
    print(f"Zotero collection Uni auto-exports to {bib} (on this machine)")
    return 0


def cmd_zotero_sync(args) -> int:
    res = zotero.sync_report(client_papers(), args.dir, args.name, rpc=zotero.rpc)
    print(f"Uni/{res.name}: {len(res.cited)} cited keys from {res.aux}; refs.bib → {res.bib}")
    if not res.tagged:
        print("note: refs.bib is outside [papers] reports (or --name differs from the folder): "
              "the index will not tag papers with this report")
    else:
        print("commit and push refs.bib and .unilib-report with the report so the server's index tags its papers")
    if res.missing:
        print("Not in Zotero: " + ", ".join(res.missing))
        return 1
    return 0


def cmd_zotero_find(args) -> int:
    for r in zotero.find(client_papers(), args.terms, rpc=zotero.rpc):
        print(f"@{r.citekey} · {r.year or 'n.d.'} · {r.title} · "
              f"{'in Uni' if r.in_uni else 'not in Uni'} · {'PDF' if r.has_pdf else 'no PDF'}")
    return 0


def cmd_zotero_status(args) -> int:
    v = zotero.rpc("api.ready")
    print(f"Zotero {v.get('zotero')} · Better BibTeX {v.get('betterbibtex')}")
    papers = client_papers()
    print(f"Uni export: {papers.bib} ({'present' if papers.bib.is_file() else 'MISSING'})")
    print(f"reports ({papers.reports_glob or 'no glob'}): " + (", ".join(r.name for r in papers.reports()) or "none"))
    return 0


def put(url: str, data: bytes, user: str, password: str, timeout: float = 300) -> int:
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    req = urllib.request.Request(url, data, method="PUT", headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def cmd_drop(args) -> int:
    """Upload files to the server's _ingest/ for the /ingest workflow."""
    dav = client_webdav()
    password = dav.password_file.read_text().strip()
    errors = 0
    for f in args.files:
        if not f.is_file() or f.suffix.lower() not in DROP_SUFFIXES:
            print(f"skipped {f}: not a PDF, PPTX or .ipynb file", file=sys.stderr)
            errors += 1
            continue
        url = f"{dav.url}/_ingest/{urllib.parse.quote(f.name)}"
        try:
            put(url, f.read_bytes(), dav.user, password)
        except (urllib.error.URLError, OSError) as e:
            print(f"failed {f.name}: {e}", file=sys.stderr)
            errors += 1
            continue
        print(f"dropped {f.name}")
    if errors < len(args.files):
        print("now run /ingest")
    return 1 if errors else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="unilib-client", description="unilib tools that act on this machine")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("drop", help="upload PDF/PPTX/.ipynb files to the server's _ingest/ (WebDAV)")
    s.add_argument("files", nargs="+", type=Path)
    s.set_defaults(func=cmd_drop)

    s = sub.add_parser("check-bib", help="check a .bib against Crossref/OpenAlex")
    s.add_argument("file", type=Path)
    s.set_defaults(func=cmd_check_bib)

    z = sub.add_parser("zotero", help="drive Zotero via Better BibTeX (Zotero must be running)")
    zsub = z.add_subparsers(dest="zcmd", required=True)
    s = zsub.add_parser("setup", help="once per machine: auto-export the Uni collection to [papers] bib")
    s.set_defaults(func=cmd_zotero_setup)
    s = zsub.add_parser("sync-report", help="Uni/<name> := keys cited in the report's .aux; refresh refs.bib")
    s.add_argument("dir", type=Path)
    s.add_argument("--name", help="report/collection name (default: folder name)")
    s.set_defaults(func=cmd_zotero_sync)
    s = zsub.add_parser("find", help="search the Zotero library")
    s.add_argument("terms")
    s.set_defaults(func=cmd_zotero_find)
    s = zsub.add_parser("status", help="Zotero/Better BibTeX versions and the Uni export")
    s.set_defaults(func=cmd_zotero_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (KeyError, ValueError, RuntimeError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

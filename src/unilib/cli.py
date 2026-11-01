from __future__ import annotations

import argparse
import itertools
import json
import re
import shlex
import shutil
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

from .config import db_path, library_root, load_modules, module_by_code
from .models import make_embedder, make_reranker
from .evaluate import evaluate, load_golden
from .index import index_library
from .organize import DuplicateError, build_name, convert_pptx, parse_part, parse_range, place, split, unique_dest
from .notebook import cell_count, sections
from .pdftools import DEFAULT_SKIP, book_plan, outline, peek, render_page
from .search import DEFAULT_RANK, RankConfig, last_rerank_error, search


def _type_num(code: str) -> tuple[str, int | None]:
    """'L03' -> ('L', 3); 'X' -> ('X', None)."""
    m = re.fullmatch(r"(CW|L|T|N|X|E|B)(\d+)?", code.upper())
    if not m:
        raise ValueError(f"bad type/number {code!r}, expected e.g. L03, CW1, B01, X")
    return m[1], int(m[2]) if m[2] else None


def cmd_peek(args, root: Path) -> int:
    if args.file.suffix.lower() == ".ipynb":
        secs = sections(args.file)
        print(f"# {args.file.name} ({cell_count(args.file)} cells)")
        for s in secs[:3]:
            print(f"--- cell {s.cell} ---\n{s.text}")
        print("\n[notebooks are not rendered]")
        return 0
    print(peek(args.file, pages=args.pages))
    out = root / ".index" / "peek" / f"{args.file.stem}-p{args.render}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(render_page(args.file, args.render))
    print(f"\n[page {args.render} render: {out}]")
    return 0


def _require_pdf(path: Path) -> None:
    if path.suffix.lower() == ".ipynb":
        raise ValueError(f"{path.name}: PDF only (notebooks cannot be outlined or split)")


def cmd_outline(args, root: Path) -> int:
    _require_pdf(args.file)
    if not args.book:
        print("\n".join(outline(args.file)))
        return 0
    plan = book_plan(args.file, author=args.author, start=args.start, skip_pattern=args.skip_pattern)
    lines = [f"unilib split {shlex.quote(str(args.file))} MODULE"]
    lines += [f"  --part {shlex.quote(p)}" for p in plan.parts]
    lines += [f"  --skip {s}" for s in plan.skips]
    print("# proposed book split — review, set MODULE, then run")
    print(" \\\n".join(lines))
    skipped = sum(b - a + 1 for a, b in (parse_range(s) for s in plan.skips))
    print(f"# {len(plan.parts)} parts, {len(plan.skips)} skip ranges ({skipped} pages)")
    for u in plan.unsplit:
        print(f"# not split (stays in the original): {u}")
    return 0


def cmd_convert(args, root: Path) -> int:
    existing = args.file.with_suffix(".pdf")
    if existing.exists():
        print(f"already converted: {existing}")
        return 0
    try:
        print(convert_pptx(args.file))
        return 0
    except RuntimeError as e:
        unsorted = root / "_unsorted"
        unsorted.mkdir(exist_ok=True)
        dest = unique_dest(unsorted, args.file.name)
        shutil.move(args.file, dest)
        print(f"{e}\nmoved to {dest}", file=sys.stderr)
        return 1


def cmd_place(args, root: Path) -> int:
    type_, num = _type_num(args.type_num)
    res = place(root, args.file, module_by_code(root, args.module), build_name(type_, num, args.title, ext=args.file.suffix.lower()))
    rel = res.path.relative_to(root)
    print(f"{res.status}: {args.file.name} -> {rel}" if res.status == "placed"
          else f"duplicate: {args.file.name} == {rel} (dropped)")
    return 0


def cmd_split(args, root: Path) -> int:
    _require_pdf(args.file)
    parts = [parse_part(p) for p in args.part]
    skip = [parse_range(s) for s in args.skip or []]
    try:
        paths = split(root, args.file, module_by_code(root, args.module), parts, skip=skip)
    except DuplicateError as e:
        print(f"duplicate: {args.file.name} == {e.existing.relative_to(root)} (dropped)")
        return 0
    for path in paths:
        print(path.relative_to(root))
    return 0


def cmd_index(args, root: Path) -> int:
    s = index_library(root, make_embedder(), full=args.full)
    print(f"added {s.added}, updated {s.updated}, removed {s.removed}, unchanged {s.unchanged}, "
          f"pages indexed {s.pages}, errors {s.errors}")
    return 1 if s.errors else 0


def cmd_search(args, root: Path) -> int:
    if not db_path(root).exists():
        print("Library not indexed — run `unilib index`.", file=sys.stderr)
        return 1
    reranker = make_reranker() if DEFAULT_RANK.rerank and not args.no_rerank else None
    hits = search(root, make_embedder(), args.query, module=args.module, k=args.k, reranker=reranker,
                  citekey=args.citekey, report=args.report, books=args.books)
    for i, h in enumerate(hits, 1):
        print(f"[{i}] {h.cite()} — {h.module} · {h.title}\n    {h.snippet}\n")
    if last_rerank_error():
        print(f"(note: reranker unavailable — {last_rerank_error()}; results in fused order)", file=sys.stderr)
    return 0


def cmd_eval(args, root: Path) -> int:
    if not db_path(root).exists():
        print("Library not indexed — run `unilib index`.", file=sys.stderr)
        return 1
    golden = load_golden(args.golden or root / "eval" / "golden.toml")
    embedder = make_embedder()
    rerank = DEFAULT_RANK.rerank and not args.no_rerank
    reranker = make_reranker() if rerank else None
    opts = dict(k=args.k, reranker=reranker, rerank_n=args.rerank_n)
    if not args.compare:
        print(evaluate(root, embedder, golden, config=replace(DEFAULT_RANK, rerank=rerank), **opts).format())
        return 0
    for a, b, c in itertools.product((False, True), repeat=3):
        rep = evaluate(root, embedder, golden, config=RankConfig(a, b, c, rerank), **opts)
        print(f"A={int(a)} B={int(b)} C={int(c)} E={int(rerank)}  {rep.summary()}")
    return 0


def cmd_webdav_config(args, root: Path) -> int:
    """Copy a WebDAV server config (JSON), giving its users read access to each module folder
    in modules.toml: the module's NixOS service runs this before WebDAV starts."""
    cfg = json.loads(args.base.read_text())
    read = [{"path": f"/{m.folder}/", "permissions": "R"} for m in load_modules(root)]
    for user in cfg.get("users", []):
        # webdav applies rules last to first, so these go first: later rules (e.g. _ingest/ CRUD) win.
        user["rules"] = read + user.get("rules", [])
    tmp = args.out.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(args.out)
    print(f"{args.out}: read access to {len(read)} module folders")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="unilib", description="Lecture-slide library tools")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("peek", help="first pages' text + a page render")
    s.add_argument("file", type=Path)
    s.add_argument("--pages", type=int, default=3)
    s.add_argument("--render", type=int, default=1, help="page to render as PNG")
    s.set_defaults(func=cmd_peek)

    s = sub.add_parser("outline", help="per-page map for choosing split points")
    s.add_argument("file", type=Path)
    s.add_argument("--book", action="store_true", help="propose B parts/skips from the outline (textbooks)")
    s.add_argument("--author", help="surname used in part titles (default: from PDF metadata)")
    s.add_argument("--start", type=int, default=1, help="first B number (default 1)")
    s.add_argument("--skip-pattern", default=DEFAULT_SKIP, help="regex for level-2 entries to skip (default %(default)s)")
    s.set_defaults(func=cmd_outline)

    s = sub.add_parser("convert", help="PPTX -> PDF (next to the PPTX)")
    s.add_argument("file", type=Path)
    s.set_defaults(func=cmd_convert)

    s = sub.add_parser("place", help="move + rename into a module folder")
    s.add_argument("file", type=Path)
    s.add_argument("module", help="module code, e.g. MATH3001")
    s.add_argument("type_num", help="type and number, e.g. L03, CW1, X")
    s.add_argument("title")
    s.set_defaults(func=cmd_place)

    s = sub.add_parser("split", help="cut a deck into parts in a module folder")
    s.add_argument("file", type=Path)
    s.add_argument("module")
    s.add_argument("--part", action="append", required=True, help="START-END:TYPE:NN:Title")
    s.add_argument("--skip", action="append", help="START-END source pages kept in the part but not indexed "
                                                   "(book exercise sets)")
    s.set_defaults(func=cmd_split)

    s = sub.add_parser("index", help="(re)index the library")
    s.add_argument("--full", action="store_true")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("search", help="search the library")
    s.add_argument("query")
    s.add_argument("--module")
    s.add_argument("-k", type=int, default=8)
    s.add_argument("--citekey", help="only papers with this BibTeX citekey")
    s.add_argument("--report", help="only papers cited by this report")
    s.add_argument("--books", action="store_true", help="include reading-list textbook chapters (type B)")
    s.add_argument("--no-rerank", action="store_true", help="skip the cross-encoder (no model load)")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("eval", help="score search against a golden query file")
    s.add_argument("--golden", type=Path, help="query file (default <library>/eval/golden.toml)")
    s.add_argument("-k", type=int, default=10)
    s.add_argument("--compare", action="store_true", help="score all 8 A/B/C ranking configs")
    s.add_argument("--rerank-n", type=int, metavar="N", help="fused candidates to rerank")
    s.add_argument("--no-rerank", action="store_true", help="turn reranking off")
    s.set_defaults(func=cmd_eval)

    s = sub.add_parser("webdav-config", help="add read rules for every module folder to a WebDAV config (JSON)")
    s.add_argument("base", type=Path)
    s.add_argument("out", type=Path)
    s.set_defaults(func=cmd_webdav_config)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args, library_root())
    except sqlite3.Error as e:
        print(f"error: database busy or unavailable: {e}", file=sys.stderr)
        return 3
    except (KeyError, ValueError, RuntimeError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

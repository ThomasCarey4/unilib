from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from .config import Module, load_modules

TYPES = ("L", "T", "CW", "N", "X", "E", "B")
EXTS = (".pdf", ".ipynb")
NAME_RE = re.compile(r"^(?P<type>CW|L|T|N|X|E|B)(?P<num>\d{2})?_(?P<title>.+)\.(?:pdf|ipynb)$")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def kebab(title: str) -> str:
    title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    words = re.findall(r"[A-Za-z0-9]+", title)
    return "-".join(w if w.isupper() else w[:1].upper() + w[1:] for w in words)


def build_name(type_: str, num: int | None, title: str, ext: str = ".pdf") -> str:
    if ext not in EXTS:
        raise ValueError(f"ext must be one of {EXTS}, got {ext!r}")
    if type_ == "E" and ext != ".ipynb":
        raise ValueError("type E is notebook-only (.ipynb)")
    if type_ == "B" and ext != ".pdf":
        raise ValueError("type B (book part) is PDF-only")
    if type_ not in TYPES:
        raise ValueError(f"type must be one of {TYPES}, got {type_!r}")
    if num is not None and not 0 <= num <= 99:
        raise ValueError(f"num must be in 0-99, got {num!r}")
    k = kebab(title)
    if not k:
        raise ValueError(f"title {title!r} has no usable characters")
    n = f"{num:02d}" if num is not None else ""
    return f"{type_}{n}_{k}{ext}"


def parse_name(filename: str) -> tuple[str, int | None, str]:
    m = NAME_RE.match(filename)
    if not m:
        return "X", None, Path(filename).stem
    num = int(m["num"]) if m["num"] else None
    title = re.sub(r"-[b-z]$", "", m["title"])
    return m["type"], num, title.replace("-", " ")


def unique_dest(folder: Path, filename: str) -> Path:
    dest = folder / filename
    stem, suffix = Path(filename).stem, Path(filename).suffix
    letter = ord("b")
    while dest.exists():
        if letter > ord("z"):
            raise RuntimeError(f"too many name collisions for {filename} in {folder}")
        dest = folder / f"{stem}-{chr(letter)}{suffix}"
        letter += 1
    return dest


@dataclass(frozen=True)
class PlaceResult:
    status: str  # "placed" | "duplicate"
    path: Path   # destination, or the existing copy for duplicates


def _by_suffix(folder: Path, suffixes: tuple[str, ...]) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in suffixes and p.is_file())


def library_files(root: Path) -> list[Path]:
    """All PDFs and notebooks in module folders (plus PDFs in _originals/ and _originals/books/)."""
    out: list[Path] = []
    for m in load_modules(root):
        folder = root / m.folder
        out += (_by_suffix(folder, EXTS) + _by_suffix(folder / "_originals", (".pdf",))
                + _by_suffix(folder / "_originals" / "books", (".pdf",)))
    return out


library_pdfs = library_files


def find_duplicate(root: Path, digest: str, exclude: Path | None = None) -> Path | None:
    skip = exclude.resolve() if exclude is not None else None
    for pdf in library_files(root):
        if skip is not None and pdf.resolve() == skip:
            continue
        if sha256(pdf) == digest:
            return pdf
    return None


def place(root: Path, src: Path, module: Module, name: str) -> PlaceResult:
    """Move src into the module folder as `name`. Exact duplicates of a file
    already in the library are deleted from the drop zone and reported."""
    ext = src.suffix.lower()
    if Path(name).name != name or Path(name).suffix.lower() != ext or ext not in EXTS:
        raise ValueError(f"invalid destination name {name!r}")
    resolved = src.resolve()
    for m in load_modules(root):
        if resolved.is_relative_to((root / m.folder).resolve()):
            raise ValueError(f"{src} is already in the library")
    pptx = src.with_suffix(".pptx")
    has_pptx = ext == ".pdf" and pptx.exists()
    dup = find_duplicate(root, sha256(src), exclude=src)
    if dup is not None:
        src.unlink()
        if has_pptx:
            unsorted = root / "_unsorted"
            unsorted.mkdir(parents=True, exist_ok=True)
            shutil.move(pptx, unique_dest(unsorted, pptx.name))
        return PlaceResult("duplicate", dup)
    folder = root / module.folder
    folder.mkdir(parents=True, exist_ok=True)
    dest = unique_dest(folder, Path(name).stem + ext)
    shutil.move(src, dest)
    if has_pptx:
        originals = folder / "_originals"
        originals.mkdir(exist_ok=True)
        shutil.move(pptx, unique_dest(originals, dest.stem + ".pptx"))
    return PlaceResult("placed", dest)


def convert_pptx(src: Path) -> Path:
    """Convert a PPTX to a PDF next to it. The PPTX stays where it is;
    `place` later moves it into the module's _originals/."""
    final = src.with_suffix(".pdf")
    if final.exists():
        raise RuntimeError(f"cannot convert {src.name}: {final.name} already exists")
    # LD_LIBRARY_PATH set for Python's native libs loads an incompatible glib into soffice.
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    with tempfile.TemporaryDirectory() as tmp:
        try:
            r = subprocess.run(
                ["soffice", f"-env:UserInstallation=file://{tmp}/lo-profile",
                 "--headless", "--convert-to", "pdf", "--outdir", tmp, str(src)],
                capture_output=True, text=True, timeout=300, env=env,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            raise RuntimeError(f"soffice failed for {src.name}: {e}") from e
        out = Path(tmp) / (src.stem + ".pdf")
        if r.returncode != 0 or not out.exists():
            msg = (r.stderr or r.stdout).strip() or "no output produced"
            raise RuntimeError(f"soffice failed for {src.name}: {msg}")
        shutil.move(out, final)
    return final


META_RE = re.compile(r"^unilib-original=(?P<orig>[^;]+);unilib-offset=(?P<off>\d+)"
                     r"(?:;unilib-skip=(?P<skip>[\d,-]+))?$")


@dataclass(frozen=True)
class Part:
    start: int  # 1-based, inclusive
    end: int    # 1-based, inclusive
    type: str
    num: int | None
    title: str


def parse_part(spec: str) -> Part:
    """'START-END:TYPE:NN:Title' (NN may be empty; Title may contain ':')."""
    try:
        rng, type_, num, title = spec.split(":", 3)
        start, end = (int(x) for x in rng.split("-"))
        n = int(num) if num else None
        if type_ not in TYPES:
            raise ValueError(f"bad type {type_!r}")
    except ValueError as e:
        raise ValueError(f"bad part spec {spec!r}, expected START-END:TYPE:NN:Title") from e
    return Part(start, end, type_, n, title)


def read_split_meta(doc) -> tuple[str | None, int]:
    m = META_RE.search((doc.metadata or {}).get("keywords") or "")
    return (urllib.parse.unquote(m["orig"]), int(m["off"])) if m else (None, 0)


def parse_range(spec: str) -> tuple[int, int]:
    """'29-31' or '29' -> (29, 31) / (29, 29); 1-based, inclusive."""
    try:
        a, _, b = spec.partition("-")
        start, end = int(a), int(b or a)
    except ValueError as e:
        raise ValueError(f"bad page range {spec!r}, expected START-END") from e
    if not 1 <= start <= end:
        raise ValueError(f"bad page range {spec!r}, expected START-END")
    return start, end


def _ranges(pages: list[int]) -> str:
    """[3, 4, 5, 11] -> '3-5,11'."""
    runs: list[list[int]] = []
    for p in sorted(pages):
        if runs and p == runs[-1][1] + 1:
            runs[-1][1] = p
        else:
            runs.append([p, p])
    return ",".join(f"{a}-{b}" if a != b else str(a) for a, b in runs)


def read_split_skip(doc) -> frozenset[int]:
    """Part-relative pages that split marked as kept-but-not-indexed (book exercise sets)."""
    m = META_RE.search((doc.metadata or {}).get("keywords") or "")
    if not m or not m["skip"]:
        return frozenset()
    pages: set[int] = set()
    for r in m["skip"].split(","):
        a, b = parse_range(r)
        pages.update(range(a, b + 1))
    return frozenset(pages)


class DuplicateError(ValueError):
    """src is byte-identical to a PDF already in the library; src was dropped."""

    def __init__(self, existing: Path) -> None:
        super().__init__(f"duplicate of {existing}")
        self.existing = existing


def split(root: Path, src: Path, module: Module, parts: list[Part],
          skip: list[tuple[int, int]] = ()) -> list[Path]:
    """Cut src into parts inside the module folder; archive src (and a sibling
    PPTX) to _originals/ (_originals/books/ for book parts). Each part records its
    original file, page offset and skipped pages in PDF metadata. `skip` ranges are
    source pages kept in the part but not indexed. Raises DuplicateError (after
    dropping src) for exact duplicates."""
    books = [p.type == "B" for p in parts]
    if any(books) and not all(books):
        raise ValueError("book parts (B) cannot be mixed with other types in one split")
    for a, b in skip:
        if not any(p.start <= a and b <= p.end for p in parts):
            raise ValueError(f"skip {a}-{b} is not inside one part")
    resolved = src.resolve()
    for m in load_modules(root):
        if resolved.is_relative_to((root / m.folder).resolve()):
            raise ValueError(f"{src} is already in the library")
    pptx = src.with_suffix(".pptx")
    dup = find_duplicate(root, sha256(src), exclude=src)
    if dup is not None:
        src.unlink()
        if pptx.exists():
            unsorted = root / "_unsorted"
            unsorted.mkdir(parents=True, exist_ok=True)
            shutil.move(pptx, unique_dest(unsorted, pptx.name))
        raise DuplicateError(dup)
    folder = root / module.folder
    originals = folder / "_originals" / "books" if parts and all(books) else folder / "_originals"
    with pymupdf.open(src) as doc:
        n = doc.page_count
        prev_end = 0
        for p in parts:
            if not (1 <= p.start <= p.end <= n) or p.start <= prev_end:
                raise ValueError(f"invalid or overlapping range {p.start}-{p.end} (document has {n} pages)")
            prev_end = p.end
        names = [build_name(p.type, p.num, p.title) for p in parts]
        originals.mkdir(parents=True, exist_ok=True)
        orig_dest = unique_dest(originals, src.name)
        rel_orig = orig_dest.relative_to(root).as_posix()
        folder.mkdir(parents=True, exist_ok=True)
        temps: list[Path] = []
        out: list[Path] = []
        try:
            for i, (p, name) in enumerate(zip(parts, names)):
                tmp = folder / f".{i}_{name}.partial"
                temps.append(tmp)
                part_doc = pymupdf.open()
                try:
                    part_doc.insert_pdf(doc, from_page=p.start - 1, to_page=p.end - 1)
                    keywords = (f"unilib-original={urllib.parse.quote(rel_orig, safe='/')};"
                                f"unilib-offset={p.start - 1}")
                    rel_skip = [pg - p.start + 1 for a, b in skip for pg in range(a, b + 1) if p.start <= pg <= p.end]
                    if rel_skip:
                        keywords += f";unilib-skip={_ranges(rel_skip)}"
                    part_doc.set_metadata({"title": p.title, "keywords": keywords})
                    part_doc.save(tmp)
                finally:
                    part_doc.close()
            for tmp, name in zip(temps, names):
                dest = unique_dest(folder, name)
                tmp.rename(dest)
                out.append(dest)
            shutil.move(src, orig_dest)
            if pptx.exists():
                shutil.move(pptx, unique_dest(originals, orig_dest.stem + ".pptx"))
        except BaseException:
            for f in temps + out:
                f.unlink(missing_ok=True)
            raise
    return out

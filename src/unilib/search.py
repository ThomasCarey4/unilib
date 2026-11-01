from __future__ import annotations

import difflib
import re
import threading
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from .config import db_path
from .embed import Embedder
from .index import connect, get_meta
from .rerank import Reranker
from .textnorm import fold, fold_with_offsets

RRF_K = 60
MAX_K = 50
BM25_W_SHORT, BM25_W_LONG = 1.0, 0.4    # A: weight at <=3 and >=8 content tokens, linear between
SHORT_TOKENS, LONG_TOKENS = 3, 8
AND_MIN_ROWS = 1                        # B: fall back to OR below this (plan value 5 was a no-op, see eval report)
DUP_RATIO = 0.9                         # C: difflib ratio for near-identical consecutive pages
RERANK_N = 10                           # E: fused candidates re-scored by the cross-encoder (N=10 keeps p50 < 3 s on CPU)
RERANK_CHARS = 1500                     # E: chunk text passed to the cross-encoder (after the title)
_state = threading.local()  # per-thread outcome of the latest search() call


def last_rerank_error() -> str | None:
    """Why the latest `search()` on this thread skipped reranking, or None if it did not."""
    return getattr(_state, "rerank_error", None)


def _short(e: BaseException) -> str:
    msg = " ".join(str(e).split())[:120]
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


# db path -> (meta generation, chunk ids, embedding matrix)
_matrix_cache: dict[str, tuple[str | None, np.ndarray, np.ndarray]] = {}


@dataclass(frozen=True)
class RankConfig:
    length_weighting: bool = False
    and_first: bool = False
    collapse: bool = False
    rerank: bool = False  # E: cross-encoder over the top RERANK_N fused candidates (needs a reranker)


DEFAULT_RANK = RankConfig(and_first=True, rerank=True)  # adopted from the golden + holdout eval, see docs/eval/2026-10-29-ranking.md


class IndexMismatchError(ValueError):
    def __init__(self) -> None:
        super().__init__("index was built with a different embedding model — run `unilib index --full`")


@dataclass(frozen=True)
class Hit:
    path: str
    module: str
    title: str
    page: int
    original_file: str | None
    original_page: int | None
    snippet: str
    score: float
    kind: str = "pdf"
    also_pages: tuple[int, ...] = ()  # near-identical adjacent pages collapsed into this hit
    citekey: str | None = None        # set for Zotero papers (module PAPERS)

    def cite(self) -> str:
        unit = "cell " if self.kind == "ipynb" else "p."
        s = f"@{self.citekey} {unit}{self.page}" if self.citekey else f"{self.path} {unit}{self.page}"
        if self.original_file:
            s += f" (orig {Path(self.original_file).name} p.{self.original_page})"
        if self.also_pages:
            s += f" (also {', '.join(unit + r for r in _runs(self.also_pages))})"
        return s


def _runs(pages: tuple[int, ...]) -> list[str]:
    """Sorted pages as contiguous runs: (7, 9, 10) -> ["7", "9–10"]."""
    out: list[list[int]] = []
    for p in sorted(set(pages)):
        if out and p == out[-1][1] + 1:
            out[-1][1] = p
        else:
            out.append([p, p])
    return [str(a) if a == b else f"{a}–{b}" for a, b in out]


def bm25_weight(n: int) -> float:
    """RRF weight of the BM25 ranking for a query with n content tokens (candidate A)."""
    if n <= SHORT_TOKENS:
        return BM25_W_SHORT
    if n >= LONG_TOKENS:
        return BM25_W_LONG
    t = (n - SHORT_TOKENS) / (LONG_TOKENS - SHORT_TOKENS)
    return BM25_W_SHORT + t * (BM25_W_LONG - BM25_W_SHORT)


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


_STOPWORDS = frozenset(
    "a an and are as at be by for from how in is it of on or that the this to was what when which who with".split())


def _content_tokens(query: str) -> list[str]:
    """Keyword tokens of the notation-folded query (the FTS index stores folded text)."""
    toks = _tokens(fold(query))
    return [t for t in toks if t not in _STOPWORDS] or toks  # stopwords add BM25 noise to paraphrase queries


def _fts_query(query: str, op: str = "OR") -> str:
    return f" {op} ".join(f'"{t}"' for t in _content_tokens(query))


def _adjacent(page: int, pages: tuple[int, ...]) -> bool:
    return min(pages) - 1 <= page <= max(pages) + 1


def _similar(a: str, b: str) -> bool:
    m = difflib.SequenceMatcher(None, a, b)
    return m.real_quick_ratio() >= DUP_RATIO and m.quick_ratio() >= DUP_RATIO and m.ratio() >= DUP_RATIO


def _snippet(text: str, query: str, width: int = 300) -> str:
    flat = " ".join(text.split())
    folded, offsets = fold_with_offsets(flat)  # match on folded text, cut the original
    pos = -1
    for tok in sorted(set(_content_tokens(query)), key=len, reverse=True):
        m = re.search(rf"\b{re.escape(tok)}\b", folded, re.IGNORECASE)
        if m:
            pos = offsets[m.start()]
            break
    start = max(0, pos - width // 3) if pos >= 0 else 0
    body = flat[start:start + width]
    return ("…" if start else "") + body + ("…" if start + width < len(flat) else "")


def _doc_filter(module: str | None, citekey: str | None, report: str | None,
                exclude_books: bool = False) -> tuple[str, list]:
    """SQL condition over docs alias `d` plus its parameters ('' when unfiltered)."""
    clauses, params = [], []
    if module:
        clauses.append("d.module = ?")
        params.append(module)
    if citekey:
        clauses.append("d.citekey = ?")
        params.append(citekey)
    if report:
        clauses.append("instr(',' || d.reports || ',', ?) > 0")
        params.append(f",{report},")
    if exclude_books:
        clauses.append("d.type != 'B'")
    return " AND ".join(clauses), params


def _fts_ranked(conn, query: str, where: str, params: list, pool: int, and_first: bool) -> list[int]:
    """Chunk ids by BM25. With and_first, all content tokens must match unless that
    gives fewer than AND_MIN_ROWS rows, in which case any token may match (candidate B)."""
    if not _content_tokens(query):
        return []
    sql = "SELECT chunks_fts.rowid FROM chunks_fts"
    if where:
        sql += " JOIN chunks c ON c.id = chunks_fts.rowid JOIN docs d ON d.id = c.doc_id"
    sql += " WHERE chunks_fts MATCH ?"
    if where:
        sql += f" AND {where}"
    sql += " ORDER BY bm25(chunks_fts) LIMIT ?"
    if and_first:
        ranked = [r[0] for r in conn.execute(sql, [_fts_query(query, "AND"), *params, pool])]
        if len(ranked) >= AND_MIN_ROWS:
            return ranked
    return [r[0] for r in conn.execute(sql, [_fts_query(query), *params, pool])]


def _embeddings(conn, db: Path, generation: str | None) -> tuple[np.ndarray, np.ndarray]:
    """Chunk-id vector and embedding matrix, cached per (db path, generation).

    Must be called inside the caller's read transaction so generation and rows agree.
    """
    key = str(db)
    cached = _matrix_cache.get(key)
    if cached and generation is not None and cached[0] == generation:
        return cached[1], cached[2]
    rows = conn.execute("SELECT id, embedding FROM chunks WHERE no_text = 0").fetchall()
    ids = np.array([r[0] for r in rows], dtype=np.int64)
    mat = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows]) if rows else np.zeros((0, 1), np.float32)
    _matrix_cache[key] = (generation, ids, mat)
    return ids, mat


def _rerank(conn, reranker: Reranker, query: str, ordered: list[int], n: int) -> list[int]:
    """Reorder the first n chunk ids by cross-encoder score; the rest keep fused order."""
    head = ordered[:n]
    if not head:
        return ordered
    marks = ",".join("?" * len(head))
    rows = {r[0]: f"{r[1]}\n{r[2][:RERANK_CHARS]}" for r in conn.execute(
        f"SELECT c.id, d.title, c.text FROM chunks c JOIN docs d ON d.id = c.doc_id WHERE c.id IN ({marks})", head)}
    head = [cid for cid in head if cid in rows]
    scores = reranker.score(query, [rows[cid] for cid in head])
    if len(scores) != len(head):
        raise ValueError(f"reranker returned {len(scores)} scores for {len(head)} candidates")
    by_score = dict(zip(head, scores))
    return sorted(head, key=by_score.__getitem__, reverse=True) + ordered[n:]


def search(root: Path, embedder: Embedder, query: str, module: str | None = None,
           k: int = 8, pool: int = 50, *, citekey: str | None = None, report: str | None = None,
           books: bool = False, config: RankConfig | None = None,
           reranker: Reranker | None = None, rerank_n: int | None = None) -> list[Hit]:
    """Hybrid BM25 + vector search fused by RRF. With `config.rerank` and a `reranker`,
    the top `rerank_n` (default RERANK_N) fused candidates are reordered by cross-encoder
    score before pages are deduplicated, collapsed and cut to k. Reading-list book parts
    (type B) are excluded unless `books`."""
    config = config or DEFAULT_RANK
    k = max(1, min(int(k), MAX_K))
    if not _tokens(fold(query)):  # a lone symbol like ∇ still folds to a keyword
        return []
    module = module.upper() if module else None
    _state.rerank_error = None
    if config.rerank and reranker is not None:
        try:
            reranker.load()  # before the read snapshot, so a slow load cannot pin the WAL
        except Exception as e:
            _state.rerank_error = _short(e)
            reranker = None
    qvec = np.asarray(embedder.embed_query(query), dtype=np.float32)
    conn = connect(root, readonly=True)
    try:
        conn.execute("BEGIN")  # one snapshot for every read below
        generation = get_meta(conn, "generation")
        stored_dim = get_meta(conn, "dim")
        stored_model = get_meta(conn, "model")
        if stored_dim is not None and int(stored_dim) != qvec.shape[-1]:
            raise IndexMismatchError()
        if stored_model is not None and stored_model != embedder.model:
            raise IndexMismatchError()
        exclude_books = not books and conn.execute(
            "SELECT 1 FROM docs WHERE type = 'B' LIMIT 1").fetchone() is not None
        where, params = _doc_filter(module, citekey, report, exclude_books)

        fts_ranked = _fts_ranked(conn, query, where, params, pool, config.and_first)

        vec_ranked: list[int] = []
        ids, mat = _embeddings(conn, db_path(root), generation)
        if len(ids):
            if mat.shape[1] != qvec.shape[-1]:
                raise IndexMismatchError()
            allowed = None
            if where:
                allowed = {r[0] for r in conn.execute(
                    f"SELECT c.id FROM chunks c JOIN docs d ON d.id = c.doc_id WHERE {where}", params)}
            for i in np.argsort(-(mat @ qvec)):
                cid = int(ids[i])
                if allowed is None or cid in allowed:
                    vec_ranked.append(cid)
                    if len(vec_ranked) == pool:
                        break

        fts_w = bm25_weight(len(_content_tokens(query))) if config.length_weighting else 1.0
        scores: dict[int, float] = defaultdict(float)
        for ranked, w in ((fts_ranked, fts_w), (vec_ranked, 1.0)):
            for rank, cid in enumerate(ranked):
                scores[cid] += w / (RRF_K + rank + 1)

        cols = {r[1] for r in conn.execute("PRAGMA table_info(docs)")}
        kind = "d.kind" if "kind" in cols else "'pdf' AS kind"  # pre-migration DBs lack the column
        ck = "d.citekey" if "citekey" in cols else "NULL AS citekey"
        hits: list[Hit] = []
        best_text: list[str] = []  # whitespace-collapsed text of each hit's best-scoring chunk
        seen: set[tuple[str, int]] = set()
        ordered = sorted(scores, key=scores.__getitem__, reverse=True)
        if config.rerank and reranker is not None:
            try:
                ordered = _rerank(conn, reranker, query, ordered, RERANK_N if rerank_n is None else rerank_n)
            except Exception as e:  # a broken reranker must never break search; keep the fused order
                _state.rerank_error = _short(e)
        for cid in ordered:
            r = conn.execute(
                "SELECT c.page, c.text, d.path, d.module, d.title, d.original_file, d.page_offset, " + kind + ", " + ck +
                " FROM chunks c JOIN docs d ON d.id = c.doc_id WHERE c.id = ?", (cid,)).fetchone()
            if r is None or (r["path"], r["page"]) in seen:
                continue
            seen.add((r["path"], r["page"]))
            if config.collapse:
                flat = " ".join(r["text"].split())
                j = next((j for j, h in enumerate(hits) if h.path == r["path"]
                          and _adjacent(r["page"], (h.page, *h.also_pages))
                          and _similar(flat, best_text[j])), None)
                if j is not None:
                    hits[j] = replace(hits[j], also_pages=tuple(sorted((*hits[j].also_pages, r["page"]))))
                    continue
                best_text.append(flat)
            hits.append(Hit(
                path=r["path"], module=r["module"], title=r["title"], page=r["page"],
                original_file=r["original_file"],
                original_page=r["page"] + r["page_offset"] if r["original_file"] else None,
                snippet=_snippet(r["text"], query), score=round(scores[cid], 4), kind=r["kind"],
                citekey=r["citekey"],
            ))
            if len(hits) == k:
                break
        return hits
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()

from __future__ import annotations

import math
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .embed import Embedder
from .models import make_reranker
from .rerank import Reranker
from .search import DEFAULT_RANK, RankConfig, search


@dataclass(frozen=True)
class GoldenQuery:
    q: str
    kind: str
    answers: frozenset[tuple[str, int]]
    module: str | None = None


@dataclass(frozen=True)
class EvalRow:
    query: GoldenQuery
    rank: int | None  # 1-based rank of first acceptable hit, None if not in top k
    seconds: float = 0.0  # wall clock of the search call


@dataclass
class EvalReport:
    rows: list[EvalRow] = field(default_factory=list)

    def hit_at(self, n: int) -> float:
        return sum(1 for r in self.rows if r.rank is not None and r.rank <= n) / len(self.rows) if self.rows else 0.0

    @property
    def mrr(self) -> float:
        return sum(1 / r.rank for r in self.rows if r.rank) / len(self.rows) if self.rows else 0.0

    def latency(self, q: float) -> float:
        """Per-query search latency at quantile q (0..1), nearest-rank."""
        xs = sorted(r.seconds for r in self.rows)
        return xs[min(len(xs), max(1, math.ceil(q * len(xs)))) - 1] if xs else 0.0

    def summary(self) -> str:
        return (f"hit@1 {self.hit_at(1):.2f}  hit@3 {self.hit_at(3):.2f}  MRR {self.mrr:.3f}  "
                f"p50 {self.latency(0.5):.2f}s  p95 {self.latency(0.95):.2f}s")

    def format(self) -> str:
        lines = [f"{'rank':>4}  {'kind':<10} query"]
        for r in self.rows:
            lines.append(f"{r.rank if r.rank else '-':>4}  {r.query.kind:<10} {r.query.q}")
        lines.append(f"{self.summary()}  (n={len(self.rows)})")
        return "\n".join(lines)


def load_golden(path: Path) -> list[GoldenQuery]:
    data = tomllib.loads(Path(path).read_text())
    out = []
    for q in data["query"]:
        answers = set()
        for a in q["answers"]:
            p, _, page = a.rpartition(":")
            answers.add((p, int(page)))
        out.append(GoldenQuery(q=q["q"], kind=q.get("kind", "concept"), answers=frozenset(answers), module=q.get("module")))
    return out


def evaluate(root: Path, embedder: Embedder, golden: list[GoldenQuery], k: int = 10,
             config: RankConfig | None = None, reranker: Reranker | None = None,
             rerank_n: int | None = None) -> EvalReport:
    """Rank of the first acceptable hit per query. One untimed warm-up search runs
    first so lazy model loads and the embedding cache are not counted as latency."""
    rep = EvalReport()
    if reranker is None and (config or DEFAULT_RANK).rerank:
        reranker = make_reranker()
    opts = dict(k=k, config=config, reranker=reranker, rerank_n=rerank_n)
    if golden:
        search(root, embedder, golden[0].q, module=golden[0].module, **opts)
    for gq in golden:
        t0 = time.perf_counter()
        hits = search(root, embedder, gq.q, module=gq.module, **opts)
        dt = time.perf_counter() - t0
        rank = None
        for i, h in enumerate(hits, 1):
            pages = {h.page, *getattr(h, "also_pages", ())}
            if any((h.path, p) in gq.answers for p in pages):
                rank = i
                break
        rep.rows.append(EvalRow(gq, rank, dt))
    return rep

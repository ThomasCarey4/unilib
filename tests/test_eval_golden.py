import pytest
from unilib.config import db_path, library_root
from unilib.evaluate import evaluate, load_golden

# The real library and its private query sets (<library>/eval/*.toml): UNILIB_ROOT or the default.
REAL = library_root()
EVAL = REAL / "eval"
# Adopted scores minus 0.05, rounded down to 2 dp (docs/eval/2026-10-29-ranking.md, addendum 3).
# (hit@1, hit@3, MRR) per set.
THRESHOLDS = {
    "golden.toml": (0.71, 0.79, 0.75),    # adopted 0.76 / 0.84 / 0.800
    "holdout.toml": (0.61, 0.68, 0.65),   # adopted 0.67 / 0.73 / 0.700
    "holdout2.toml": (0.85, 0.85, 0.85),  # adopted 0.90 / 0.90 / 0.900
}

pytestmark = pytest.mark.eval


@pytest.fixture(scope="module")
def models():
    """Embedder and reranker, loaded once for every eval set."""
    from unilib.models import make_embedder, make_reranker
    from unilib.search import DEFAULT_RANK
    return make_embedder(), make_reranker() if DEFAULT_RANK.rerank else None


@pytest.mark.skipif(not db_path(REAL).exists(), reason="real library not indexed")
@pytest.mark.parametrize("name", ["golden.toml", "holdout.toml", "holdout2.toml"])
def test_eval_set_meets_thresholds(name, models):
    if not (EVAL / name).is_file():
        pytest.skip(f"no {EVAL / name}")
    embedder, reranker = models
    rep = evaluate(REAL, embedder, load_golden(EVAL / name), k=10, reranker=reranker)
    print(rep.format())
    min_hit1, min_hit3, min_mrr = THRESHOLDS[name]
    assert rep.hit_at(1) >= min_hit1 and rep.hit_at(3) >= min_hit3 and rep.mrr >= min_mrr

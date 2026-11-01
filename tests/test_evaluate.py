from helpers import SCI, FakeEmbedder, make_pdf
from unilib.evaluate import EvalReport, EvalRow, GoldenQuery, evaluate, load_golden
from unilib import evaluate as evaluate_mod
from unilib.index import index_library
from unilib.search import RankConfig


def _golden(tmp_path, body):
    p = tmp_path / "golden.toml"
    p.write_text(body)
    return p


def test_load_golden(tmp_path):
    g = load_golden(_golden(tmp_path, f'''
[[query]]
q = "armijo"
kind = "exact"
answers = ["{SCI}/L09_A.pdf:2", "{SCI}/L09_A.pdf:3"]
'''))
    assert g[0].q == "armijo" and g[0].answers == {(f"{SCI}/L09_A.pdf", 2), (f"{SCI}/L09_A.pdf", 3)}
    assert g[0].module is None


def test_evaluate_ranks_and_aggregates(lib, tmp_path):
    make_pdf(lib / SCI / "L09_A.pdf", ["intro", "Armijo sufficient decrease"])
    make_pdf(lib / SCI / "L10_B.pdf", ["gradient descent steepest"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    g = load_golden(_golden(tmp_path, f'''
[[query]]
q = "armijo"
kind = "exact"
answers = ["{SCI}/L09_A.pdf:2"]

[[query]]
q = "nothing matches this zzz"
kind = "concept"
answers = ["{SCI}/L09_A.pdf:99"]
'''))
    rep = evaluate(lib, FakeEmbedder(), g, k=5, config=RankConfig(and_first=True))
    assert rep.rows[0].rank == 1
    assert rep.rows[1].rank is None
    assert rep.hit_at(1) == 0.5 and rep.hit_at(3) == 0.5
    assert rep.mrr == 0.5
    assert "hit@1" in rep.format()
    assert all(r.seconds > 0 for r in rep.rows)
    assert "p50 " in rep.format()


def test_latency_quantiles():
    gq = GoldenQuery("q", "exact", frozenset())
    rep = EvalReport([EvalRow(gq, None, s) for s in (0.4, 0.1, 0.3, 0.2)])
    assert rep.latency(0.5) == 0.2 and rep.latency(0.95) == 0.4 and rep.latency(0.0) == 0.1
    assert EvalReport().latency(0.5) == 0.0


class _Fake:
    model = "fake"
    built = 0

    def __init__(self):
        type(self).built += 1

    def load(self):
        pass

    def score(self, query, texts):
        return [0.0] * len(texts)


def test_evaluate_builds_reranker_when_config_reranks(lib, tmp_path, monkeypatch):
    make_pdf(lib / SCI / "L09_A.pdf", ["Armijo sufficient decrease"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    g = load_golden(_golden(tmp_path, f'[[query]]\nq = "armijo"\nanswers = ["{SCI}/L09_A.pdf:1"]\n'))
    monkeypatch.setattr(evaluate_mod, "make_reranker", _Fake)
    _Fake.built = 0
    evaluate(lib, FakeEmbedder(), g)  # default config reranks
    assert _Fake.built == 1
    evaluate(lib, FakeEmbedder(), g, config=RankConfig(and_first=True))
    assert _Fake.built == 1
    evaluate(lib, FakeEmbedder(), g, config=RankConfig(rerank=True), reranker=_Fake())
    assert _Fake.built == 2  # only the explicit one

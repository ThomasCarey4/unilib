"""One server, several libraries: /mcp serves the first, /<name>/mcp the others, each with its own instructions."""
import asyncio

import pytest
from starlette.testclient import TestClient

from helpers import SCI, FakeEmbedder, make_pdf
from unilib import serve, server
from unilib.config import Library
from unilib.index import index_library

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}}}
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json",
           "authorization": "Bearer s3cret", "mcp-protocol-version": "2025-06-18"}

ENG_TOML = """
[library]
title = "Engineering references"
books_default = true

[[module]]
code = "PG"
name = "PostgreSQL"
folder = "PG"
keywords = ["mvcc", "vacuum"]
"""


@pytest.fixture(autouse=True)
def fake_models(monkeypatch):
    monkeypatch.setattr(server, "_embedder", FakeEmbedder())
    monkeypatch.setattr(server, "_reranker", None)
    monkeypatch.setattr(server.search_mod, "DEFAULT_RANK", server.search_mod.RankConfig(and_first=True))


@pytest.fixture
def eng(tmp_path):
    root = tmp_path / "EngLibrary"
    (root / "PG").mkdir(parents=True)
    (root / "_ingest").mkdir()
    (root / "modules.toml").write_text(ENG_TOML)
    return root


@pytest.fixture
def two(lib, eng):
    make_pdf(lib / SCI / "L09_Line-Search.pdf", ["intro", "Armijo condition sufficient decrease"])
    make_pdf(eng / "PG" / "B01_Rogov-Ch5-Vacuum.pdf", ["intro", "vacuum removes dead tuples below the xmin horizon"])
    for root in (lib, eng):
        index_library(root, FakeEmbedder(), log=lambda s: None)
    return [Library("uni", lib), Library("eng", eng)]


def _text(r):
    assert r.status_code == 200, r.text
    return r.json()["result"]["content"][0]["text"]


def _search(c, path, query):
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": query}}}
    return _text(c.post(path, json=call, headers=HEADERS))


def test_endpoint_paths():
    assert serve.endpoint_path(0, "uni") == "/mcp"
    assert serve.endpoint_path(1, "eng") == "/eng/mcp"


def test_each_endpoint_searches_its_own_library(two, monkeypatch, tmp_path):
    monkeypatch.setenv("UNILIB_ROOT", str(tmp_path / "nowhere"))  # the endpoints must not fall back to the env
    app = serve.build_app(server.build_servers(two), host="0.0.0.0")
    with TestClient(serve.Gate(app, "s3cret")) as c:
        uni = _search(c, "/mcp", "Armijo vacuum")
        eng = _search(c, "/eng/mcp", "Armijo vacuum")
    assert "L09_Line-Search.pdf" in uni and "Rogov" not in uni
    assert "B01_Rogov-Ch5-Vacuum.pdf" in eng and "L09" not in eng


def test_server_info_and_instructions_per_endpoint(two):
    app = serve.build_app(server.build_servers(two), host="0.0.0.0")
    with TestClient(serve.Gate(app, "s3cret")) as c:
        uni = c.post("/mcp", json=INIT, headers=HEADERS).json()["result"]
        eng = c.post("/eng/mcp", json=INIT, headers=HEADERS).json()["result"]
    assert uni["serverInfo"]["name"] == "unilib"
    assert uni["instructions"].startswith(server.UNI_INSTRUCTIONS)  # a library without [library] keeps today's text
    assert "Other libraries on this server: eng (Engineering references)" in uni["instructions"]
    assert "Other libraries on this server: uni." in eng["instructions"]
    assert eng["serverInfo"]["name"] == "unilib-eng"
    assert "Engineering references" in eng["instructions"]
    assert "PG — PostgreSQL" in eng["instructions"]
    assert "university" not in eng["instructions"].lower()
    assert "prefer the one matching the version the project runs, then the newest" in eng["instructions"]


def test_unknown_path_is_404(two):
    app = serve.build_app(server.build_servers(two), host="0.0.0.0")
    with TestClient(serve.Gate(app, "s3cret")) as c:
        assert c.post("/other/mcp", json=INIT, headers=HEADERS).status_code == 404


def test_built_servers_expose_the_same_tools(two):
    (_, uni), (_, eng) = server.build_servers(two)
    names = lambda m: sorted(t.name for t in asyncio.run(m.list_tools()))
    assert names(uni) == names(eng) == names(server.mcp)


def test_bound_tool_ignores_env_root(two, monkeypatch, tmp_path):
    monkeypatch.setenv("UNILIB_ROOT", str(tmp_path / "nowhere"))
    (_, _), (_, eng) = server.build_servers(two)
    text = asyncio.run(eng.call_tool("list_library", {})).content[0].text
    assert "PG — PostgreSQL" in text and "B01_Rogov-Ch5-Vacuum.pdf" in text


def test_instructions_survive_a_bad_modules_toml(eng):
    (eng / "modules.toml").write_text("not toml [")
    text = server.library_instructions(Library("eng", eng))
    assert "list_library" in text  # generic text, no crash at startup


def test_search_books_default_follows_library(two):
    from unilib.search import search
    uni, eng = two
    q = "vacuum dead tuples xmin horizon"
    assert any(h.path.endswith("B01_Rogov-Ch5-Vacuum.pdf") for h in search(eng.root, FakeEmbedder(), q))
    assert not search(eng.root, FakeEmbedder(), q, books=False)


# --- search(library=...) across libraries -------------------------------------------------

class KeywordReranker:
    """Scores by keyword so cross-library order is predictable."""
    model = "fake-rerank"

    def load(self):
        pass

    def score(self, query, texts):
        return [float("xmin" in t) + float("Armijo" in t) * 0.5 for t in texts]


@pytest.fixture
def env_two(two, monkeypatch):
    uni, eng = two
    monkeypatch.setenv("UNILIB_LIBRARIES", f"uni={uni.root},eng={eng.root}")
    return two


def _call(m, name, args):
    return asyncio.run(m.call_tool(name, args)).content


def test_search_other_library_prefixes_citations(env_two):
    (_, uni), (_, eng) = server.build_servers(env_two)
    text = _call(uni, "search", {"query": "vacuum xmin", "library": "eng"})[0].text
    assert "[1] eng:PG/B01_Rogov-Ch5-Vacuum.pdf p.2" in text
    own = _call(eng, "search", {"query": "vacuum xmin", "library": "eng"})[0].text
    assert "[1] PG/B01_Rogov-Ch5-Vacuum.pdf p.2" in own  # its own library: no prefix


def test_search_all_merges_by_rerank_score(env_two, monkeypatch):
    monkeypatch.setattr(server, "_reranker", KeywordReranker())
    monkeypatch.setattr(server.search_mod, "DEFAULT_RANK", server.search_mod.RankConfig(and_first=True, rerank=True))
    (_, uni), _ = server.build_servers(env_two)
    text = _call(uni, "search", {"query": "Armijo vacuum xmin", "library": "all", "k": 4})[0].text
    first, second = text.split("\n\n")[:2]
    assert first.startswith("[1] eng:PG/B01_Rogov-Ch5-Vacuum.pdf p.2")   # score 1.0
    assert second.startswith(f"[2] {SCI}/L09_Line-Search.pdf p.2")       # score 0.5, own library unprefixed


def test_search_all_without_reranker_interleaves(env_two):
    (_, uni), _ = server.build_servers(env_two)
    text = _call(uni, "search", {"query": "Armijo vacuum xmin", "library": "all", "k": 2})[0].text
    libs = ["eng" if "eng:" in block.split("\n")[0] else "uni" for block in text.split("\n\n")[:2]]
    assert sorted(libs) == ["eng", "uni"]


def test_search_unknown_library(env_two):
    (_, uni), _ = server.build_servers(env_two)
    text = _call(uni, "search", {"query": "x", "library": "nope"})[0].text
    assert "Unknown library 'nope'" in text and "uni, eng" in text


def test_get_page_with_library_prefix(env_two):
    (_, uni), _ = server.build_servers(env_two)
    out = _call(uni, "get_page", {"path": "eng:PG/B01_Rogov-Ch5-Vacuum.pdf", "page": 2, "image": False})
    assert out[0].text.startswith("eng:PG/B01_Rogov-Ch5-Vacuum.pdf p.2") and "xmin horizon" in out[0].text


def test_list_library_other_library(env_two):
    (_, uni), _ = server.build_servers(env_two)
    text = _call(uni, "list_library", {"library": "eng"})[0].text
    assert "PG — PostgreSQL" in text and "MATH3001" not in text


def test_reserved_library_names(monkeypatch):
    from unilib.config import libraries
    for name in ("lesson", "zotero"):
        monkeypatch.setenv("UNILIB_LIBRARIES", f"{name}=/a")
        with pytest.raises(ValueError, match="reserved"):
            libraries()


def test_eval_across_libraries(env_two, tmp_path):
    from unilib.evaluate import evaluate, load_golden
    uni, eng = env_two
    g = tmp_path / "g.toml"
    g.write_text('[[query]]\nq = "xmin horizon vacuum"\nanswers = ["eng:PG/B01_Rogov-Ch5-Vacuum.pdf:2"]\n')
    cfg = server.search_mod.RankConfig(and_first=True)
    assert evaluate(uni.root, FakeEmbedder(), load_golden(g), config=cfg).hit_at(3) == 0.0
    rep = evaluate(uni.root, FakeEmbedder(), load_golden(g), config=cfg, others=(("eng", eng.root),))
    assert rep.hit_at(3) == 1.0

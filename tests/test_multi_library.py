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
    assert uni["instructions"] == server.UNI_INSTRUCTIONS  # a library without [library] keeps today's text
    assert eng["serverInfo"]["name"] == "unilib-eng"
    assert "Engineering references" in eng["instructions"]
    assert "PG — PostgreSQL" in eng["instructions"]
    assert "university" not in eng["instructions"].lower()


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

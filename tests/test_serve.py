import asyncio

import pytest
from starlette.testclient import TestClient

from helpers import SCI, FakeEmbedder, make_pdf
from unilib import server, serve
from unilib.index import index_library
from unilib.serve import Gate, exit_when_idle

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}}}
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    monkeypatch.setattr(server, "_embedder", FakeEmbedder())
    monkeypatch.setattr(server, "_reranker", None)
    monkeypatch.setattr(server.search_mod, "DEFAULT_RANK", server.search_mod.RankConfig(and_first=True))


def _client(token):
    app = server.mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")
    return Gate(app, token)


def test_gate_rejects_missing_or_wrong_token():
    with TestClient(_client("s3cret")) as c:
        assert c.post("/mcp", json=INIT, headers=HEADERS).status_code == 401
        r = c.post("/mcp", json=INIT, headers={**HEADERS, "authorization": "Bearer nope"})
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


def test_search_over_http_without_session(lib):
    make_pdf(lib / SCI / "L09_Line-Search.pdf", ["intro", "Armijo condition sufficient decrease"])
    index_library(lib, FakeEmbedder(), log=lambda s: None)
    auth = {**HEADERS, "authorization": "Bearer s3cret"}
    with TestClient(_client("s3cret")) as c:
        r = c.post("/mcp", json=INIT, headers=auth)
        assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "unilib"
        # stateless: a tool call needs no mcp-session-id from an earlier initialize
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "search", "arguments": {"query": "Armijo"}}}
        r = c.post("/mcp", json=call, headers={**auth, "mcp-protocol-version": "2025-06-18"})
        assert r.status_code == 200
        text = r.json()["result"]["content"][0]["text"]
        assert f"{SCI}/L09_Line-Search.pdf p.2" in text


def test_get_stream_is_declined_and_does_not_hold_the_server_up():
    gate = _client("s3cret")
    with TestClient(gate) as c:
        r = c.get("/mcp", headers={"accept": "text/event-stream", "authorization": "Bearer s3cret"})
        assert r.status_code == 405 and r.headers["allow"] == "POST"
        assert c.get("/mcp").status_code == 401  # auth still comes first
    assert gate._inflight == 0


class _Server:
    should_exit = False


def test_idle_exit_waits_for_inflight_and_idle_time():
    now = [0.0]
    gate = Gate(None, None, clock=lambda: now[0])
    srv = _Server()

    async def scenario():
        gate._inflight = 1  # a long request (e.g. index) keeps the server up
        now[0] = 10_000.0
        task = asyncio.create_task(exit_when_idle(gate, srv, idle_s=60, poll_s=0.001))
        await asyncio.sleep(0.01)
        assert not srv.should_exit
        gate._inflight, gate._last = 0, now[0]
        now[0] += 59
        await asyncio.sleep(0.01)
        assert not srv.should_exit
        now[0] += 2
        await asyncio.wait_for(task, 1)
        assert srv.should_exit

    asyncio.run(scenario())


def test_systemd_socket_requires_matching_pid(monkeypatch):
    monkeypatch.setenv("LISTEN_FDS", "1")
    monkeypatch.setenv("LISTEN_PID", "999999999")
    assert serve.systemd_socket() is None
    monkeypatch.delenv("LISTEN_PID")
    assert serve.systemd_socket() is None


def test_token_required_beyond_loopback(monkeypatch, tmp_path):
    monkeypatch.delenv("UNILIB_TOKEN_FILE", raising=False)
    monkeypatch.setattr(serve, "systemd_socket", lambda: None)
    with pytest.raises(SystemExit, match="UNILIB_TOKEN_FILE"):
        serve.serve_http(server.mcp, "0.0.0.0", 0, None)
    empty = tmp_path / "tok"
    empty.write_text("\n")
    monkeypatch.setenv("UNILIB_TOKEN_FILE", str(empty))
    with pytest.raises(SystemExit, match="empty"):
        serve.read_token()

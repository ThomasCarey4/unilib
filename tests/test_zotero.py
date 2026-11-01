import json
import os
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

import pytest

from helpers import add_papers, make_bib, make_pdf
from unilib.config import REPORT_MARKER, PapersConfig, load_papers
from unilib.zotero import ZoteroError, aux_keys, find, find_aux, rpc, setup, sync_report


class FakeRPC:
    def __init__(self, missing=()):
        self.calls, self.missing = [], set(missing)

    def __call__(self, method, *params):
        self.calls.append((method, *params))
        if method == "item.export":
            missing = [k for k in params[0] if k in self.missing]
            if missing:
                raise ZoteroError(f"item.export: not found: {', '.join(missing)}")
            return "".join(f"@article{{{k}, title={{T}}}}\n" for k in params[0])
        if method == "item.search":
            return [{"citekey": "he2016", "title": "Deep Residual Learning", "issued": {"date-parts": [[2016]]}},
                    {"citekey": "lecun1998", "title": "Gradient-based learning"}]
        return {"libraryID": 1, "key": "ABC"}


def _report(tmp_path, aux_text, name="dl-cw"):
    d = tmp_path / name
    d.mkdir()
    (d / "main.tex").write_text("\\documentclass{article}\n")
    (d / "notes.tex").write_text("no documentclass here")
    (d / "main.aux").write_text(aux_text)
    return d


def test_aux_keys_biblatex_bibtex_and_input(tmp_path):
    (tmp_path / "ch1.aux").write_text("\\citation{c}\n")
    aux = tmp_path / "main.aux"
    aux.write_text("\\abx@aux@cite{0}{a}\n\\abx@aux@cite{0}{a}\n\\citation{b,c}\n\\citation{*}\n\\@input{ch1.aux}\n")
    assert aux_keys(aux) == ["a", "b", "c"]


def test_find_aux_prefers_main_document(tmp_path):
    d = _report(tmp_path, "")
    (d / "build").mkdir()
    newer = d / "build" / "main.aux"
    newer.write_text("")
    (d / "notes.aux").write_text("")
    os.utime(d / "main.aux", (1_000, 1_000))  # explicit mtimes: same-tick writes tie on coarse filesystem clocks
    os.utime(newer, (2_000, 2_000))
    assert find_aux(d) == newer


def test_find_aux_missing(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    (d / "main.tex").write_text("\\documentclass{article}")
    with pytest.raises(ZoteroError, match="compile"):
        find_aux(d)


def test_sync_report(lib, tmp_path):
    add_papers(lib, tmp_path / "uni.bib", f"{tmp_path}/*/refs.bib")
    toml_before = (lib / "modules.toml").read_text()
    d = _report(tmp_path, "\\abx@aux@cite{0}{he2016}\n\\abx@aux@cite{0}{typo2020}\n")
    fake = FakeRPC(missing={"typo2020"})
    res = sync_report(load_papers(lib), d, rpc=fake)
    assert [c[0] for c in fake.calls] == ["collection.scanAUX", "autoexport.add", "item.export", "item.export"]
    assert fake.calls[0][1:] == ("//Uni/dl-cw", str(d / "main.aux"))
    assert fake.calls[1][1:] == ("//Uni/dl-cw", "Better BibLaTeX", str(d / "refs.bib"), {}, False)
    assert (res.name, res.cited, res.missing, res.tagged) == ("dl-cw", ["he2016", "typo2020"], ["typo2020"], True)
    assert "@article{he2016" in (d / "refs.bib").read_text()
    assert (d / REPORT_MARKER).is_file()  # sync-report opts the folder in as a report
    assert [r.name for r in load_papers(lib).reports()] == ["dl-cw"]
    assert (lib / "modules.toml").read_text() == toml_before  # nothing writes the tracked file at runtime


def test_sync_report_untagged_outside_glob_or_renamed(lib, tmp_path):
    add_papers(lib, tmp_path / "uni.bib", f"{tmp_path}/*/refs.bib")
    d = _report(tmp_path, "\\citation{he2016}\n")
    assert sync_report(load_papers(lib), d, name="custom", rpc=FakeRPC()).tagged is False  # index tags by folder name
    assert not (d / REPORT_MARKER).exists()  # the index would tag under the folder name, not "custom"
    (tmp_path / "deep").mkdir()
    outside = _report(tmp_path / "deep", "\\citation{he2016}\n")
    assert sync_report(load_papers(lib), outside, rpc=FakeRPC()).tagged is False


def test_sync_report_bad_name(lib, tmp_path):
    add_papers(lib, tmp_path / "uni.bib")
    with pytest.raises(ValueError, match="report name"):
        sync_report(load_papers(lib), _report(tmp_path, ""), name="has space", rpc=FakeRPC())


def test_setup_registers_the_configured_export(tmp_path):
    fake = FakeRPC()
    papers = PapersConfig(tmp_path / "exp" / "uni.bib")
    assert setup(papers, rpc=fake) == tmp_path / "exp" / "uni.bib"
    assert fake.calls == [("autoexport.add", "//Uni", "Better BibLaTeX", str(papers.bib), {}, False)]
    assert papers.bib.parent.is_dir()


def test_find_marks_uni_membership_and_pdf(lib, tmp_path):
    pdf = make_pdf(tmp_path / "z" / "he.pdf", ["x"])
    add_papers(lib, make_bib(tmp_path / "z" / "uni.bib", [{"key": "he2016", "title": "R", "file": str(pdf)}]))
    fake = FakeRPC()
    rows = find(load_papers(lib), "deep learning", rpc=fake)
    # a condition list, not a bare string: BBT 9 on Zotero 9 rejects string quick search ("Invalid condition 'groupStart'")
    assert fake.calls == [("item.search", [["quicksearch-titleCreatorYear", "contains", "deep learning"]])]
    assert [(r.citekey, r.year, r.in_uni, r.has_pdf) for r in rows] == [
        ("he2016", 2016, True, True), ("lecun1998", None, False, False)]


def test_rpc_transport_and_errors(monkeypatch):
    replies = iter([{"jsonrpc": "2.0", "result": {"zotero": "7"}, "id": 1},
                    {"jsonrpc": "2.0", "error": {"code": -32602, "message": "Unknown translator"}, "id": 1}])
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps(next(replies)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("UNILIB_ZOTERO_URL", f"http://127.0.0.1:{srv.server_port}/")
        assert rpc("api.ready") == {"zotero": "7"}
        assert seen[0]["method"] == "api.ready" and seen[0]["params"] == []
        with pytest.raises(ZoteroError, match="Unknown translator"):
            rpc("autoexport.add", "//Uni", "Nope", "/x.bib")
    finally:
        srv.shutdown()


def test_rpc_zotero_not_running(monkeypatch):
    monkeypatch.setenv("UNILIB_ZOTERO_URL", "http://127.0.0.1:9/")
    with pytest.raises(ZoteroError, match="not running"):
        rpc("api.ready")


@contextmanager
def _zotero_server(status, body: bytes, ctype="application/json", delay=0.0):
    """Local stand-in for BBT answering every POST with the given status/body after `delay` seconds."""
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the client gave up (timeout test)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)  # daemon_threads off: server_close() joins handler threads
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}/"
    finally:
        srv.shutdown()
        srv.server_close()
        t.join()


def test_rpc_http_error_html_body(monkeypatch):
    with _zotero_server(500, b"<html>oops</html>", ctype="text/html") as url:
        monkeypatch.setenv("UNILIB_ZOTERO_URL", url)
        with pytest.raises(ZoteroError, match="HTTP 500"):
            rpc("item.export", ["a"])


def test_rpc_http_error_json_without_error_key(monkeypatch):
    with _zotero_server(500, b'{"jsonrpc": "2.0", "id": 1}') as url:
        monkeypatch.setenv("UNILIB_ZOTERO_URL", url)
        with pytest.raises(ZoteroError, match="HTTP 500"):
            rpc("item.export", ["a"])


def test_rpc_200_non_json_body(monkeypatch):
    with _zotero_server(200, b"not json at all", ctype="text/plain") as url:
        monkeypatch.setenv("UNILIB_ZOTERO_URL", url)
        with pytest.raises(ZoteroError, match="unexpected reply"):
            rpc("item.export", ["a"])


def test_rpc_200_json_without_result(monkeypatch):
    with _zotero_server(200, b'{"jsonrpc": "2.0", "id": 1}') as url:
        monkeypatch.setenv("UNILIB_ZOTERO_URL", url)
        with pytest.raises(ZoteroError, match="unexpected reply"):
            rpc("item.export", ["a"])


def test_rpc_timeout_is_not_reported_as_not_running(monkeypatch):
    with _zotero_server(200, b'{"jsonrpc": "2.0", "result": 1, "id": 1}', delay=0.5) as url:
        monkeypatch.setenv("UNILIB_ZOTERO_URL", url)
        with pytest.raises(ZoteroError, match="did not answer"):
            rpc("api.ready", timeout=0.1)


def test_sync_report_non_str_export(lib, tmp_path):
    class ListRPC(FakeRPC):
        def __call__(self, method, *params):
            if method == "item.export":
                return ["not", "text"]
            return super().__call__(method, *params)

    d = _report(tmp_path, "\\abx@aux@cite{0}{he2016}\n")
    with pytest.raises(ZoteroError, match="unexpected reply type list"):
        sync_report(load_papers(lib), d, rpc=ListRPC())

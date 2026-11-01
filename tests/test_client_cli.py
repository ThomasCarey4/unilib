import pytest

from helpers import make_bib
from unilib import client_cli, zotero


def _config(tmp_path, webdav=True):
    pw = tmp_path / "pw"
    pw.write_text("hunter2\n")
    cfg = tmp_path / "client.toml"  # the path isolate_client points UNILIB_CLIENT_CONFIG at
    cfg.write_text(f'[papers]\nbib = "{tmp_path}/uni.bib"\nreports = "{tmp_path}/*/refs.bib"\n'
                   + (f'[webdav]\nurl = "http://dav:8091/"\nuser = "unilib"\npassword_file = "{pw}"\n' if webdav else ""))


def test_check_bib(tmp_path, capsys):
    bib = make_bib(tmp_path / "refs.bib", [{"key": "k", "type": "online", "title": "T"}])
    assert client_cli.main(["check-bib", str(bib)]) == 0
    assert "SKIPPED" in capsys.readouterr().out


def test_zotero_sync_report_uses_client_papers(tmp_path, capsys, monkeypatch):
    _config(tmp_path)
    calls = []
    monkeypatch.setattr(zotero, "sync_report", lambda papers, d, name=None, rpc=None: calls.append((papers.bib, d, name)) or
                        zotero.SyncResult("r", d / "main.aux", d / "refs.bib", ["a"], ["b"], False))
    assert client_cli.main(["zotero", "sync-report", str(tmp_path), "--name", "r"]) == 1  # missing keys → exit 1
    out = capsys.readouterr().out
    assert calls == [(tmp_path / "uni.bib", tmp_path, "r")] and "Not in Zotero: b" in out and "not tag" in out


def test_zotero_setup_registers_client_export(tmp_path, capsys, monkeypatch):
    _config(tmp_path)
    seen = []
    monkeypatch.setattr(zotero, "rpc", lambda *a, **k: seen.append(a))
    assert client_cli.main(["zotero", "setup"]) == 0
    assert seen == [("autoexport.add", "//Uni", "Better BibLaTeX", str(tmp_path / "uni.bib"), {}, False)]


def test_zotero_not_running(monkeypatch, capsys):
    def down(*a, **k):
        raise zotero.ZoteroError("Zotero is not running")
    monkeypatch.setattr(zotero, "rpc", down)
    assert client_cli.main(["zotero", "status"]) == 2
    assert "not running" in capsys.readouterr().err


def test_drop_puts_files_into_ingest(tmp_path, capsys, monkeypatch):
    _config(tmp_path)
    sent = []
    monkeypatch.setattr(client_cli, "put", lambda url, data, user, pw: sent.append((url, data, user, pw)) or 201)
    a = tmp_path / "slides (1).pdf"
    a.write_bytes(b"%PDF")
    b = tmp_path / "notes.txt"
    b.write_text("x")
    assert client_cli.main(["drop", str(a), str(b)]) == 1  # notes.txt is refused
    assert sent == [("http://dav:8091/_ingest/slides%20%281%29.pdf", b"%PDF", "unilib", "hunter2")]
    cap = capsys.readouterr()
    assert "dropped slides (1).pdf" in cap.out and "skipped" in cap.err


def test_drop_needs_webdav_config(tmp_path, capsys):
    _config(tmp_path, webdav=False)
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF")
    assert client_cli.main(["drop", str(f)]) == 2
    assert "[webdav] needs" in capsys.readouterr().err


def test_client_imports_nothing_heavy():
    import subprocess
    import sys
    code = ("import sys, unilib.client_cli, unilib.local_server; "
            "bad = [m for m in ('torch', 'numpy', 'pymupdf', 'transformers', 'unilib.search', 'unilib.index') "
            "if m in sys.modules]; assert not bad, bad")
    subprocess.run([sys.executable, "-c", code], check=True)

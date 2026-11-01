import asyncio
import subprocess
import sys

from helpers import make_bib
from unilib import local_server, zotero


def call(name, args):
    return asyncio.run(local_server.mcp.call_tool(name, args))


def test_check_bib_tool(lib, tmp_path):
    bib = make_bib(tmp_path / "refs.bib", [{"key": "k", "type": "online", "title": "T"}])
    text = call("check_bib", {"bib_path": str(bib)}).content[0].text
    assert "SKIPPED" in text and "@k" in text
    assert "No such .bib" in call("check_bib", {"bib_path": str(tmp_path / "x.bib")}).content[0].text


def test_zotero_tools_report_errors_as_text(lib, tmp_path, monkeypatch):
    def down(*a, **k):
        raise zotero.ZoteroError("Zotero is not running")
    monkeypatch.setattr(zotero, "rpc", down)
    assert "not running" in call("zotero_find", {"terms": "x"}).content[0].text
    d = tmp_path / "r"
    d.mkdir()
    assert "compile" in call("zotero_sync_report", {"report_dir": str(d)}).content[0].text


def test_local_server_loads_no_models():
    """unilib-local must start fast on the laptop: no torch / fastembed / index imports."""
    code = ("import sys, unilib.local_server; "
            "bad = [m for m in ('torch', 'fastembed', 'transformers', 'unilib.search', 'unilib.models') if m in sys.modules]; "
            "assert not bad, bad")
    subprocess.run([sys.executable, "-c", code], check=True)

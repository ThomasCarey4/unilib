"""unilib snapshot: fetch [[snapshot]] sources (pdf, git markdown, html pages) into _ingest/<id>/."""
import json
import subprocess

import pytest

from helpers import make_pdf
from unilib import cli
from unilib.htmlmd import html_to_markdown
from unilib.markdown import read, sections
from unilib.snapshot import Snapshot, load_snapshots, run_snapshot

MODULES = '[[module]]\ncode = "TSDB"\nname = "TimescaleDB"\nfolder = "TSDB"\n'


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@x", "PATH": "/usr/bin:/bin:" + __import__("os").environ["PATH"]})


@pytest.fixture
def docs_repo(tmp_path):
    repo = tmp_path / "docsrepo"
    (repo / "compression" / "settings").mkdir(parents=True)
    (repo / "hypertables").mkdir()
    (repo / "compression" / "about.md").write_text(
        "---\ntitle: About compression\n---\nimport X from 'y';\n\n# Compression\n\n<Highlight type=\"note\">\n"
        "Chunks are compressed per segment.\n</Highlight>\n\n## Order by\n\nDerived when unset.\n")
    (repo / "compression" / "settings" / "orderby.mdx").write_text("# compress_orderby\n\nSet it explicitly.\n")
    (repo / "hypertables" / "about.md").write_text("# Hypertables\n\nChunks by time.\n")
    (repo / "README.md").write_text("# readme\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "docs")
    _git(repo, "tag", "2.22.0")
    return repo


@pytest.fixture
def eng(tmp_path, docs_repo):
    root = tmp_path / "Eng"
    (root / "TSDB").mkdir(parents=True)
    (root / "_ingest").mkdir()
    (root / "modules.toml").write_text(MODULES)
    pdf = make_pdf(tmp_path / "web" / "rfc9266.pdf", ["RFC 9266 tls-exporter channel binding"])
    page = tmp_path / "web" / "chapter.html"
    page.write_text("<html><body><nav>Home | Books</nav><main><h1>Handling Overload</h1>"
                    "<p>Shed load <b>early</b>.</p><ul><li>one</li><li>two</li></ul>"
                    "<pre><code>retry_budget = 0.1</code></pre></main><footer>©</footer></body></html>")
    (root / "sources.toml").write_text(f'''
[[snapshot]]
id = "tsdb-docs"
mode = "git"
url = "{docs_repo}"
ref = "2.22.0"
paths = ["compression/**/*.md", "compression/**/*.mdx", "hypertables/*.md"]
group = 1
version = "TimescaleDB 2.22"
licence = "Apache-2.0 (docs licence unverified)"
source_url = "https://github.com/timescale/docs/blob/2.22.0"

[[snapshot]]
id = "rfc9266"
mode = "pdf"
url = "file://{pdf}"
version = "RFC 9266"
licence = "IETF Trust"

[[snapshot]]
id = "sre-overload"
mode = "html"
urls = ["file://{page}"]
version = "SRE book 1e"
licence = "CC BY-NC-ND 4.0"
''')
    return root


def test_load_snapshots(eng):
    snaps = {s.id: s for s in load_snapshots(eng)}
    assert snaps["tsdb-docs"].mode == "git" and snaps["tsdb-docs"].group == 1
    assert snaps["sre-overload"].urls == (f"file://{eng.parent / 'web' / 'chapter.html'}",)


@pytest.mark.parametrize("text, match", [
    ('[[snapshot]]\nid = "x"\nmode = "ftp"\nurl = "u"\n', "mode"),
    ('[[snapshot]]\nid = "Bad Id"\nmode = "pdf"\nurl = "u"\n', "id"),
    ('[[snapshot]]\nid = "x"\nmode = "git"\nurl = "u"\n', "ref"),
    ('[[snapshot]]\nid = "x"\nmode = "html"\n', "urls"),
])
def test_bad_snapshot_rejected(tmp_path, text, match):
    (tmp_path / "sources.toml").write_text(text)
    with pytest.raises(ValueError, match=match):
        load_snapshots(tmp_path)


def test_git_snapshot_groups_into_markdown(eng):
    out = run_snapshot(eng, "tsdb-docs", log=lambda s: None)
    names = sorted(p.name for p in out)
    assert names == ["Compression.md", "Hypertables.md"]
    comp = eng / "_ingest" / "tsdb-docs" / "Compression.md"
    meta, body = read(comp)
    assert meta["version"] == "TimescaleDB 2.22" and meta["snapshot"] == "tsdb-docs" and len(meta["commit"]) == 40
    assert "import X" not in body and "<Highlight" not in body and "Chunks are compressed per segment." in body
    assert "Source: https://github.com/timescale/docs/blob/2.22.0/compression/about.md" in body
    heads = [s.heading for s in sections(comp)]
    assert "About compression › Compression" in heads   # file title is h1, its own headings demoted
    assert any(h.endswith("compress_orderby") for h in heads)
    assert "readme" not in (eng / "_ingest" / "tsdb-docs" / "Hypertables.md").read_text()


def test_pdf_snapshot(eng):
    (out,) = run_snapshot(eng, "rfc9266", log=lambda s: None)
    assert out == eng / "_ingest" / "rfc9266" / "rfc9266.pdf" and out.read_bytes().startswith(b"%PDF")


def test_html_snapshot(eng):
    (out,) = run_snapshot(eng, "sre-overload", log=lambda s: None)
    meta, body = read(out)
    assert meta["source_url"].endswith("chapter.html") and meta["licence"] == "CC BY-NC-ND 4.0"
    assert body.lstrip().startswith("# Handling Overload")
    assert "Home | Books" not in body and "©" not in body
    assert "Shed load **early**." in body and "- one" in body and "retry_budget = 0.1" in body


def test_snapshot_refuses_overwrite_without_refresh(eng):
    run_snapshot(eng, "rfc9266", log=lambda s: None)
    with pytest.raises(FileExistsError, match="--refresh"):
        run_snapshot(eng, "rfc9266", log=lambda s: None)
    run_snapshot(eng, "rfc9266", refresh=True, log=lambda s: None)
    state = json.loads((eng / ".index" / "snapshots.json").read_text())
    assert state["rfc9266"]["files"]["rfc9266.pdf"]  # sha256 recorded


def test_unknown_snapshot(eng):
    with pytest.raises(KeyError, match="nope"):
        run_snapshot(eng, "nope", log=lambda s: None)


def test_cli_snapshot_lists_and_runs(eng, monkeypatch, capsys):
    monkeypatch.setenv("UNILIB_ROOT", str(eng))
    assert cli.main(["snapshot"]) == 0
    listed = capsys.readouterr().out
    assert "tsdb-docs · git" in listed and "not fetched" in listed
    assert cli.main(["snapshot", "rfc9266"]) == 0
    assert "rfc9266/rfc9266.pdf" in capsys.readouterr().out


def test_html_to_markdown_tables_and_links():
    md = html_to_markdown("<h2>T</h2><table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>"
                          "<p>see <a href='https://x.org'>docs</a> and <code>x &lt; y</code></p>"
                          "<script>alert(1)</script>")
    assert "## T" in md and "| a | b |" in md and "| 1 | 2 |" in md
    assert "see docs (https://x.org) and `x < y`" in md and "alert" not in md

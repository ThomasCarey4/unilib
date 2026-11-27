"""Project lessons: markdown notes in project repos, indexed as module LESSONS and cited `lesson:<id> §N`."""
import asyncio

import pytest

from helpers import FakeEmbedder
from unilib import server
from unilib.config import LESSONS, load_lessons
from unilib.index import index_library, lesson_status
from unilib.search import search

LESSON = """---
id: serenlink-2026-09-25-jsonb-compress-orderby
title: jsonb column in derived compress_orderby makes chunks uncompressable
project: serenlink
kind: incident
date: 2026-09-25
versions: {postgresql: "16.10", timescaledb: "2.22.0"}
tags: [timescaledb, compression, btree]
refs:
  - "TSDB/D04_Compression-Settings.md §2 [TimescaleDB 2.22]"
status: current
---
## Finding
A jsonb column in the derived compress_orderby made every chunk with a large value uncompressable.

## Rule
Never put a jsonb column in compress_orderby; set it explicitly.
"""


@pytest.fixture
def eng(tmp_path, monkeypatch):
    root = tmp_path / "Eng"
    (root / "PG").mkdir(parents=True)
    repo = tmp_path / "repos" / "SerenLink" / "docs" / "lessons"
    repo.mkdir(parents=True)
    (repo / "2026-09-25-jsonb-compress-orderby.md").write_text(LESSON)
    (root / "modules.toml").write_text(
        '[library]\ntitle = "Eng"\n\n[[module]]\ncode = "PG"\nname = "PostgreSQL"\nfolder = "PG"\n\n'
        f'[lessons]\nglobs = ["{tmp_path}/repos/*/docs/lessons/*.md"]\n')
    monkeypatch.setenv("UNILIB_ROOT", str(root))
    return root, repo


def _index(root):
    logs = []
    stats = index_library(root, FakeEmbedder(), log=logs.append)
    return stats, logs


def test_load_lessons(eng):
    root, _ = eng
    assert len(load_lessons(root)) == 1
    assert load_lessons(root.parent) == ()  # no modules.toml [lessons]: none (and no crash on missing file)


def test_lesson_indexed_and_cited(eng):
    root, _ = eng
    stats, _ = _index(root)
    assert stats.added == 1
    hits = search(root, FakeEmbedder(), "jsonb compress_orderby uncompressable")
    assert hits[0].module == LESSONS
    assert hits[0].cite() == ("lesson:serenlink-2026-09-25-jsonb-compress-orderby §1"
                              " [postgresql 16.10, timescaledb 2.22.0]")
    assert hits[0].title.startswith("jsonb column")


def test_project_filter(eng):
    root, _ = eng
    _index(root)
    q = "jsonb compress_orderby"
    assert search(root, FakeEmbedder(), q, project="serenlink")
    assert not search(root, FakeEmbedder(), q, project="serensite")


@pytest.mark.parametrize("bad, why", [
    (LESSON.replace("kind: incident", "kind: anecdote"), "kind"),
    (LESSON.replace("project: serenlink\n", ""), "project"),
    (LESSON.replace("id: serenlink-2026-09-25-jsonb-compress-orderby", "id: Bad Id!"), "id"),
    ("no frontmatter at all\n", "frontmatter"),
])
def test_invalid_lesson_is_skipped_and_logged(eng, bad, why):
    root, repo = eng
    (repo / "2026-09-25-jsonb-compress-orderby.md").write_text(bad)
    stats, logs = _index(root)
    assert stats.added == 0
    assert any("lesson" in m and why in m for m in logs), logs


def test_duplicate_lesson_id_skipped(eng):
    root, repo = eng
    (repo / "copy.md").write_text(LESSON)
    stats, logs = _index(root)
    assert stats.added == 1 and any("duplicate lesson id" in m for m in logs)


def test_superseded_lesson(eng):
    root, repo = eng
    (repo / "2026-09-25-jsonb-compress-orderby.md").write_text(
        LESSON.replace("status: current", "status: superseded-by:serenlink-2026-12-01-x"))
    _index(root)
    assert not search(root, FakeEmbedder(), "jsonb compress_orderby")
    assert search(root, FakeEmbedder(), "jsonb compress_orderby", superseded=True)


def test_get_page_and_list_library(eng, monkeypatch):
    root, _ = eng
    _index(root)
    monkeypatch.setattr(server, "_embedder", FakeEmbedder())
    out = asyncio.run(server.mcp.call_tool(
        "get_page", {"path": "lesson:serenlink-2026-09-25-jsonb-compress-orderby", "page": 2})).content[0].text
    assert out.startswith("lesson:serenlink-2026-09-25-jsonb-compress-orderby §2") and "Never put a jsonb" in out
    listing = asyncio.run(server.mcp.call_tool("list_library", {"module": "LESSONS"})).content[0].text
    assert "LESSONS — project lessons" in listing
    assert "lesson:serenlink-2026-09-25-jsonb-compress-orderby · serenlink · incident · 2026-09-25" in listing
    assert [s.id for s in lesson_status(root)] == ["serenlink-2026-09-25-jsonb-compress-orderby"]


def test_lesson_removed_when_file_goes(eng):
    root, repo = eng
    _index(root)
    (repo / "2026-09-25-jsonb-compress-orderby.md").unlink()
    stats, _ = _index(root)
    assert stats.removed == 1


def test_eval_answer_can_name_a_lesson(eng, tmp_path):
    from unilib.evaluate import evaluate, load_golden
    root, _ = eng
    _index(root)
    g = tmp_path / "g.toml"
    g.write_text('[[query]]\nq = "jsonb compress_orderby"\n'
                 'answers = ["lesson:serenlink-2026-09-25-jsonb-compress-orderby:1", "lesson:serenlink-2026-09-25-jsonb-compress-orderby:2"]\n')
    from unilib.search import RankConfig
    rep = evaluate(root, FakeEmbedder(), load_golden(g), config=RankConfig(and_first=True))
    assert rep.hit_at(1) == 1.0

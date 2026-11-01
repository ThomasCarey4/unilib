# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Machine-specific setup (hosts, paths, how this deployment is wired) is in `CLAUDE.local.md` when it exists. It's gitignored, so read it if it's present.

## What this repo is

`unilib` indexes and searches a library of lecture slides, notebooks, reading-list textbook chapters and Zotero papers. The library itself is a separate data directory (`UNILIB_ROOT`, default `~/Documents/UniLibrary`) holding `modules.toml`, one folder per module, and `.index/`. It is never part of this repo.

One distribution, two roles:

- **Server** (the `server` extra; the dev group includes it):
  - `unilib-mcp` serves MCP over stateless streamable HTTP with these tools: `search`, `get_page`, `list_library`, `ingest_list`, `ingest_peek`, `ingest_outline`, `ingest_convert`, `ingest_place`, `ingest_split`, `ingest_unsort` and `index`.
  - `unilib` is the server-side CLI.
  - The server is long-running (or socket-activated), so code changes take effect only after it restarts or idles out.
- **Client** (the base install: stdlib + `mcp`, no torch or pymupdf):
  - `unilib-local-mcp` (`check_bib`, `zotero_sync_report`, `zotero_find`) and `unilib-client` (`drop`, `check-bib`, `zotero …`) act on the current machine.
  - `test_client_cli.py::test_client_imports_nothing_heavy` enforces that the client imports nothing heavy.

`skills/ingest/SKILL.md` is the `/ingest` workflow, driven entirely by the server's ingest tools. Follow it exactly: nothing moves before the user approves the table.

## Commands

```bash
uv run pytest                                   # all but eval tests (addopts = -m 'not eval')
uv run pytest -m "not slow and not eval"        # also skip the slow tests, which download the real Qwen3 models (~2.4 GB); a bare -m "not slow" replaces addopts and would run the real-index eval
uv run pytest tests/test_search.py::test_dimension_mismatch_raises   # single test
uv run pytest -m eval                           # golden + holdout + holdout2 thresholds against the REAL index, sets from <library>/eval/; skipped if absent
uv run unilib index [--full]
uv run unilib search "query" [--module CODE] [-k 8] [--no-rerank] [--citekey KEY | --report NAME] [--books]
uv run unilib outline --book FILE [--author A] [--start N]
uv run unilib eval [--golden FILE] [--compare] [--no-rerank] [--rerank-n N]
uv run unilib-mcp [--stdio] [--host H --port P] [--idle-exit S]
uv run unilib-client drop FILE… | check-bib FILE | zotero setup|sync-report DIR [--name N]|find TERMS|status
```

`convert` needs `soffice` (LibreOffice) and strips `LD_LIBRARY_PATH` before running it.

Environment:
- **Server:**
  - `UNILIB_ROOT`: library root.
  - `UNILIB_TOKEN_FILE`: bearer token. Required unless the server is bound to loopback.
  - `UNILIB_IDLE_EXIT_S`: default 900; `0` means never exit.
  - `UNILIB_MODEL_CACHE`: default `~/.cache/unilib/models`.
- **Client:**
  - `UNILIB_CLIENT_CONFIG`: default `~/.config/unilib/client.toml`. `[papers]` sets the Zotero layout and falls back to defaults; `[webdav]` is needed for `drop`.
  - `UNILIB_CACHE`: default `~/.cache/unilib`.
  - `UNILIB_MAILTO`: Crossref polite pool.
  - `UNILIB_ZOTERO_URL`: Better BibTeX endpoint.

Tests:
- The `lib` fixture (`tests/conftest.py`) builds a temporary library with two modules and sets `UNILIB_ROOT`.
- An autouse fixture points the client config and cache at the test's temporary directory.
- `tests/helpers.py` provides `make_pdf`, `make_notebook`, and `FakeEmbedder`, a deterministic 64-dim hashing embedder. Use `FakeEmbedder` for anything not marked `slow`.

## Architecture

- **Naming and placement** (`organize.py`):
  - Files are named `<TYPE><NN>_<Kebab-Title>.<ext>` with TYPE ∈ `L T CW N E X B`. `E` is for `.ipynb` only; `B` is a reading-list textbook chapter (PDF only).
  - On a name collision the file gets a `-b`…`-z` suffix, which `parse_name` strips.
  - `place` and `split` drop exact duplicates, comparing sha256 against every library file, including `_originals/`.
  - A sibling PPTX goes to `<module>/_originals/`.
  - `split` cuts a deck into parts and archives the source PDF to `_originals/`. Each part records its original path and page offset in the PDF `keywords` metadata (`unilib-original=…;unilib-offset=N`), so citations show `(orig X p.N)`.
  - `split --skip` keeps pages in a part but marks them `unilib-skip=` (part-relative), so the index blanks them.
  - `outline --book` proposes B parts and skips from a book's outline.
- **Index** (`index.py`): one SQLite file, `.index/library.db` (WAL mode).
  - Tables: `meta`, `docs`, `chunks` (the original text plus a float32 embedding blob), and FTS5 `chunks_fts`, which stores `textnorm.fold(text)` with the porter tokenizer.
  - Only files directly inside module folders are indexed; `_originals/` is not.
  - Pages are split into chunks of up to 300 words, and a tail under 50 words merges into the previous chunk.
  - Notebooks are indexed per section (`notebook.py`), so `page` means the first cell id for `.ipynb`.
  - The embedded text is `title + "\n" + chunk`.
  - Change detection checks mtime/size first, then sha256. Each doc is written in its own savepoint.
  - A change of embedding model/dim or of `SCHEMA_VERSION` triggers a full rebuild automatically. A full rebuild runs in one transaction, so readers keep seeing the old index until it commits. Bump `SCHEMA_VERSION` whenever the stored form changes.
  - Every write bumps `meta.generation`. `search.py` uses it to invalidate its in-process embedding-matrix cache.
- **Text folding** (`textnorm.py`): maps Greek letters and maths symbols to ASCII words (`δ` → `delta`, `≤` → `<=`). It applies to both the indexed FTS text and queries. `fold_with_offsets` maps snippet positions back to the original text.
- **Search** (`search.py`): a hybrid of FTS5 BM25 and cosine similarity over embeddings.
  - Models (`models.py` → `qwen.py`): Qwen3-Embedding-0.6B (dim 1024, with its query instruction) and Qwen3-Reranker-0.6B in bf16 (P(yes) over yes/no, computed in float32).
  - Results are fused with RRF (`RRF_K=60`, pool 50). The top `RERANK_N=10` are then rescored by the cross-encoder. Hits are deduped by (path, page) and cut to k (max 50).
  - `RankConfig` switches: A length-weighted BM25, B AND-first FTS with OR fallback, C collapse of near-identical adjacent pages, E rerank. `DEFAULT_RANK` is B + E. Read the ranking eval notes (see `CLAUDE.local.md`) before changing any ranking constant.
  - `books=False` by default: `d.type != 'B'` is added only when a B doc exists, so without books the SQL is unchanged.
  - All reads use one snapshot. `reranker.load()` runs before the snapshot, so a slow load can't pin the WAL.
  - Reranker failures never break search: results fall back to fused order and the error is reported via the thread-local `last_rerank_error()`. A failed model load isn't retried for `RETRY_AFTER_S` (600 s).
  - A model/dim mismatch with the index raises `IndexMismatchError`.
- **Papers** (`config.py`, `index.py`, `bibcheck.py`, `zotero.py`, `local_server.py`): Zotero papers are indexed as module `PAPERS`.
  - The server reads `[papers]` from `modules.toml`: `bib`, `reports` (a glob; only folders holding the `.unilib-report` marker that `sync-report` writes count) and `zotero_dir`. `docs.path` is `zotero:<path under zotero_dir>` or absolute for linked files. `docs.citekey/year/authors/reports` hold the metadata. `strip_references` drops the reference list from the indexed text.
  - Clients read their own `[papers]` from the client config (`client_papers`), because Zotero exports are per machine. Nothing writes `modules.toml` at runtime.
  - `get_page(path="@citekey", page=N)` works as well as a hit's `path`/`page`. `search` takes `citekey=` and `report=`.
  - `bibcheck.py` checks a .bib against Crossref, then DataCite for DOIs Crossref lacks (arXiv), or OpenAlex by title. Its cache is `$UNILIB_CACHE/bibcheck.json`.
  - `zotero.py` talks to Better BibTeX JSON-RPC only; it never writes Zotero's sqlite.
- **MCP server** (`server.py`, `serve.py`, `ingest_tools.py`):
  - **Model loading:** the embedder and reranker load lazily behind locks, because tools run in worker threads.
  - **Database access:** reads are read-only. For a missing index, a busy DB or an unavailable model, tools return a message string instead of raising.
  - **`get_page`:** takes the hit's `path` and `page` (not the orig page) and returns the page text plus a PNG render for PDFs, or the section text for notebooks.
  - **`serve.py`:**
    - Wraps the stateless streamable-HTTP app in `Gate`, which checks the bearer token and tracks in-flight requests and last activity.
    - Takes the listening socket from systemd (`LISTEN_FDS`; `LISTEN_PID` may be the parent, because `uv run` starts Python as a child).
    - `exit_when_idle` stops uvicorn after the idle timeout.
  - **`ingest_tools.py`:** runs the CLI commands in-process on files under `<library>/_ingest/`, refusing paths outside it. Writes and the captured stdout are serialised under one lock.
- **Eval** (`evaluate.py`):
  - Query sets are private to each library (`<library>/eval/{golden,holdout,holdout2}.toml`). Answers are `path:page` entries.
  - Metrics: hit@1, hit@3, MRR and p50/p95 latency. `test_eval_golden.py` enforces thresholds (adopted scores minus 0.05).
  - A ranking change must hold up on golden and holdout, be confirmed on holdout2, and be written up.

## Repo conventions

- Never commit library content (slides, notebooks, books, `modules.toml`, the index, eval sets) or machine-specific details; those belong in the library directory or `CLAUDE.local.md`.
- Commits use Conventional Commits with the `unilib` scope, e.g. `feat(unilib): …`, `fix(unilib): …`, `test(unilib): …`, `docs: …`, `chore: …`.

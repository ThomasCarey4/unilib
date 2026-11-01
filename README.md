# unilib

Search over your lecture slides, notebooks, reading-list textbooks and Zotero papers, served to Claude Code (or any MCP client) from a single machine.

- **Library:** a plain directory with one folder per module, defined in `modules.toml`. Files are renamed to `<TYPE><NN>_<Title>.pdf|ipynb`, e.g. `L03_Newton-Methods.pdf`. Types: `L` lecture, `T` tutorial/lab, `CW` coursework, `N` notes, `E` example notebook, `X` other, `B` reading-list textbook chapter.
- **Server** (`unilib-mcp`): indexes the library and serves MCP over streamable HTTP. Its tools are `search`, `get_page`, `list_library`, the ingest tools and `index`.
- **Client** (`unilib-client`, `unilib-local-mcp`): runs on each machine you work on. It talks to Zotero and your LaTeX reports (`check_bib`, `zotero_sync_report`, `zotero_find`) and uploads new files to the server's drop zone.

Search is hybrid. Keyword results (SQLite FTS5) and semantic results (Qwen3-Embedding-0.6B) are fused, then the top hits are reranked by Qwen3-Reranker-0.6B. Maths notation is folded to words (`δ` → `delta`), so `J(x) delta = -F(x)` matches the slide text. Results cite `path p.N`, and for slides split out of a combined deck they also give the page in the original file.

## Architecture

```
MCP client ──HTTP /mcp (bearer token)──► unilib-mcp      (socket-activated; exits when idle)
           ──stdio──► unilib-local-mcp   (Zotero, report .bib files on this machine)
unilib-client drop ──WebDAV PUT──► <library>/_ingest/  ──► /ingest skill (server tools) ──► index
```

The server only uses memory while it's being used. Under systemd socket activation it starts on the first request, loads the models lazily, and exits after `UNILIB_IDLE_EXIT_S` seconds (default 900) without requests. The first search after an idle period takes about 7 s; later searches take about 2 s on a CPU. The transport is stateless, so an idle exit never strands a client session.

## Server setup

Requirements: [uv](https://docs.astral.sh/uv/), Python ≥ 3.12, and LibreOffice (`soffice`) if you want to convert PPTX files. The models download on first use to `~/.cache/unilib/models` (about 2.4 GB). torch runs on the CPU.

```bash
git clone <this repo> ~/src/unilib && cd ~/src/unilib
mkdir -p ~/Documents/UniLibrary/{_ingest,_unsorted}
cp modules.example.toml ~/Documents/UniLibrary/modules.toml    # edit, then create the module folders
uv run --extra server unilib index
```

To serve, create a token and use socket activation (systemd units shown; adapt the paths):

```ini
# unilib.socket
[Socket]
ListenStream=8095
[Install]
WantedBy=sockets.target

# unilib.service
[Service]
Environment=UNILIB_ROOT=%h/Documents/UniLibrary UNILIB_TOKEN_FILE=%h/.config/unilib/token
ExecStart=uv run --frozen --no-dev --extra server --project %h/src/unilib unilib-mcp
```

Without socket activation, `unilib-mcp --host 0.0.0.0 --port 8095` binds directly. Use `--idle-exit 0` to never exit, or `--stdio` for the stdio transport. A token is required unless the server is bound to loopback. Serve over a private network or behind TLS; the token is sent as plain HTTP otherwise.

Register the server with Claude Code:

```bash
claude mcp add --scope user --transport http unilib http://SERVER:8095/mcp \
  --header "Authorization: Bearer $(cat token)"
```

For `/ingest` from other machines, share the library root over WebDAV. The client needs write access to `_ingest/`; nothing else has to be exposed.

## Client setup

```bash
cp client.example.toml ~/.config/unilib/client.toml        # Zotero paths, WebDAV for `drop`
claude mcp add --scope user unilib-local -- uvx --from ~/src/unilib unilib-local-mcp
ln -s ~/src/unilib/skills/ingest ~/.claude/skills/ingest
```

The client installs without the `server` extra: it needs only the standard library and `mcp`.

## NixOS / Home Manager

The flake provides `packages.<system>.{unilib,unilib-client}`, a NixOS module for the server and a Home Manager module for clients. The server package uses PyTorch's official CPU wheel (MKL) on x86_64-linux with Python 3.14: nixpkgs' source-built torch (OpenBLAS/Eigen) made search about 4× slower.

```nix
# flake.nix inputs
unilib = { url = "github:ThomasCarey4/unilib"; inputs.nixpkgs.follows = "nixpkgs"; };

# server host (NixOS)
imports = [ inputs.unilib.nixosModules.default ];
services.unilib = {
  enable = true;
  libraryDir = "/srv/unilib";
  tokenFile = "/run/secrets/unilib-token";
  convert.package = pkgs.libreoffice;          # optional: PPTX conversion
  webdav = {
    enable = true;
    environmentFile = "/run/secrets/unilib-webdav.env";  # UNILIB_WEBDAV_PASSWORD=…
    readableRegex = "^/COMP[^/]*/";                      # optional: read-only module folders; default only _ingest/
  };
};
networking.firewall.allowedTCPPorts = [ 8095 8091 ];   # the module opens nothing itself

# client (Home Manager)
imports = [ inputs.unilib.homeManagerModules.default ];
programs.unilib-client = {
  enable = true;
  settings.webdav = { url = "http://server:8091"; user = "unilib"; password_file = "/run/secrets/unilib-webdav"; };
  ingestSkill.enable = true;                    # ~/.claude/skills/ingest
  claudeCode = {
    register = true;                            # merges unilib + unilib-local into ~/.claude.json
    serverUrl = "http://server:8095/mcp";
    tokenFile = "/run/secrets/unilib-token";    # read at connect time via headersHelper; never stored
  };
};
```

By default the server runs as a `unilib` system user with `ProtectHome=true`. To index papers from a user's Zotero, set `user` to that user and `protectHome = "read-only"`.

## Usage

### Adding slides

1. Upload the files: `uvx --from ~/src/unilib unilib-client drop slides.pdf deck.pptx` (or copy them into `_ingest/` over WebDAV).
2. Run `/ingest` in Claude Code. It classifies each file by module, proposes names and split points for combined decks, and waits for your approval. Then it places the files and indexes them, all through the server's tools.

On the server you can do the same by hand:

```bash
uv run unilib convert ~/Documents/UniLibrary/_ingest/deck.pptx
uv run unilib peek ~/Documents/UniLibrary/_ingest/slides.pdf
uv run unilib place ~/Documents/UniLibrary/_ingest/slides.pdf MATH3001 L03 "Newton methods"
uv run unilib split ~/Documents/UniLibrary/_ingest/All.pdf COMP3002 --part "1-34:L:01:Intro" --part "35-80:L:02:Logic"
uv run unilib index
uv run unilib search "Armijo condition" --module MATH3001
```

`place` and `split` drop exact duplicates of files already in the library. Split decks and the source PPTX files are kept in `<module>/_originals/`.

### Reading-list books

Drop the PDF and run `/ingest`. It's split per chapter (`B01_…`), and exercise pages are kept but not indexed. Books are left out of search unless you pass `books=True` (MCP) or `--books` (CLI).

### Papers and reports

Papers from a Zotero collection are indexed as module `PAPERS` (see `[papers]` in `modules.example.toml`), so `search` and `get_page` (`@citekey`) cover them alongside the slides. You need Zotero with Better BibTeX on each machine, and the collection named "Uni".

- **Per machine:** run `unilib-client zotero setup` to register the Better BibTeX auto-export at `[papers] bib`.
- **Writing a report:**
  1. Use a folder matching `[papers] reports` with `\addbibresource{refs.bib}`.
  2. Compile, then run `zotero_sync_report` (or `unilib-client zotero sync-report DIR`). It collects the cited keys into `Uni/<name>` and refreshes `refs.bib`.
  3. Once the report's repo is on the server, re-indexing tags its papers.
- **Checking a bibliography:** `check_bib` (or `unilib-client check-bib refs.bib`) checks entries against Crossref, DataCite and OpenAlex.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `UNILIB_ROOT` | `~/Documents/UniLibrary` | library root (server) |
| `UNILIB_TOKEN_FILE` | unset | bearer token for the HTTP server |
| `UNILIB_IDLE_EXIT_S` | `900` | idle seconds before the server exits; `0` never |
| `UNILIB_MODEL_CACHE` | `~/.cache/unilib/models` | Hugging Face model cache |
| `UNILIB_CLIENT_CONFIG` | `~/.config/unilib/client.toml` | client config |
| `UNILIB_CACHE` | `~/.cache/unilib` | client caches (bibcheck) |
| `UNILIB_MAILTO` | unset | contact address sent to Crossref by `check-bib` |
| `UNILIB_ZOTERO_URL` | `http://127.0.0.1:23119/better-bibtex/json-rpc` | Better BibTeX endpoint |

The index lives in `<library>/.index/library.db`. Rebuild it with `unilib index --full`.

## Development

```bash
uv run pytest                              # unit tests, eval excluded
uv run pytest -m "not slow and not eval"   # also skip tests that download the real models
uv run pytest -m eval                      # ranking thresholds on <library>/eval/*.toml against the real index
uv run unilib eval --compare               # score every ranking configuration
```

"""unilib-local: tools that act on this machine's files and Zotero, so they run where Claude runs —
unlike `unilib`, the search server. Paths come from the client config (config.client_papers)."""
from __future__ import annotations

import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import zotero
from .bibcheck import check_bib as run_check_bib, format_report
from .config import client_papers

mcp = MCPServer(
    "unilib-local",
    instructions=(
        "Bibliography and Zotero tools for the user's LaTeX reports, acting on this machine. "
        "`check_bib` checks a report's .bib against Crossref/DataCite/OpenAlex; `zotero_sync_report` files the "
        "report's cited papers into Zotero collection Uni/<report> and refreshes its refs.bib; "
        "`zotero_find` answers \"do I already have X?\". Paper search and page reading are in `unilib`."
    ),
)


def _papers():
    """This machine's Zotero layout, or None when the client config is unreadable (check_bib then skips PDFs)."""
    try:
        return client_papers()
    except (OSError, ValueError) as e:
        print(f"unilib-local: client config: {e}", file=sys.stderr)
        return None


@mcp.tool()
def check_bib(bib_path: str) -> str:
    """Check every entry of a .bib file against Crossref, then DataCite (by DOI), or OpenAlex (by title).

    Reports OK / MISMATCH (title, year or first author differ; bib value vs remote) /
    NOT FOUND / SKIPPED (@online etc. without DOI) / ERROR, a DOI suggestion for
    entries without one, and [no PDF] when Zotero holds no PDF for the key.
    Takes ~0.5 s per uncached entry.

    Args:
        bib_path: absolute path to the report's .bib (usually <report>/refs.bib)
    """
    bib = Path(bib_path).expanduser()
    if not bib.is_file():
        return f"No such .bib file: {bib_path}"
    return format_report(run_check_bib(_papers(), bib))


@mcp.tool()
def zotero_sync_report(report_dir: str, name: str | None = None) -> str:
    """File a LaTeX report's cited papers into Zotero and refresh its refs.bib.

    Reads the report's newest .aux (compile first), makes Zotero collection
    Uni/<name> hold exactly the \\cite'd and \\nocite'd keys, keeps
    <report_dir>/refs.bib auto-exported (Better BibLaTeX) from it, marks the folder
    as a unilib report (.unilib-report) so the index tags its papers, and lists
    cited keys that Zotero does not have. Needs Zotero running.

    Args:
        report_dir: absolute path of the report folder
        name: collection/report name (default: the folder name)
    """
    try:
        res = zotero.sync_report(_papers(), Path(report_dir), name, rpc=zotero.rpc)
    except (zotero.ZoteroError, ValueError, OSError) as e:
        return f"Sync failed: {e}"
    out = [f"Uni/{res.name}: {len(res.cited)} cited keys from {res.aux.name}; refs.bib → {res.bib}"]
    if res.missing:
        out.append("Not in Zotero (typo, or clip the paper into Uni): " + ", ".join(res.missing))
    if not res.tagged:
        out.append("Note: refs.bib is outside [papers] reports (or the name differs from the folder), "
                   "so the index will not tag papers with this report.")
    out.append("Commit and push refs.bib and .unilib-report; the server's index tags papers once that repo is "
               "pulled on the server and `index` runs.")
    return "\n".join(out)


@mcp.tool()
def zotero_find(terms: str) -> str:
    """Search the whole Zotero library (title, creator, year, citekey) — e.g. "do I already have X?".

    Each row: @citekey · year · title · in Uni / not in Uni · PDF / no PDF. Needs Zotero running.

    Args:
        terms: words from the title, an author surname, a year or a citekey
    """
    try:
        rows = zotero.find(_papers(), terms, rpc=zotero.rpc)
    except (zotero.ZoteroError, ValueError, OSError) as e:
        return f"Zotero search failed: {e}"
    if not rows:
        return f"Nothing in Zotero matches {terms!r}."
    return "\n".join(f"@{r.citekey} · {r.year or 'n.d.'} · {r.title} · "
                     f"{'in Uni' if r.in_uni else 'not in Uni'} · {'PDF' if r.has_pdf else 'no PDF'}" for r in rows)


def main() -> None:
    mcp.run()

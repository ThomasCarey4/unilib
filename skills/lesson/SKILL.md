---
name: lesson
description: Record a project lesson — a measured finding, an incident's root cause, or a non-obvious decision — as a markdown note in the project's docs/lessons/, backed by unilib-eng citations, so later agents find it through the engineering library. Use when the user says "/lesson", "write this up as a lesson", "record this finding", or after an incident, a measurement or a decision that took real work to establish.
---

# Write a project lesson

Lessons are short notes in the project's own repo, at `docs/lessons/YYYY-MM-DD-<slug>.md`. The engineering library (MCP server `unilib-eng`) indexes them as module `LESSONS`. They are cited as `lesson:<id> §N` and filtered with `search(project=…)`.

## When to write one

Write a lesson for any of these:
- **Incident:** something broke, and the cause is now known (with evidence).
- **Measurement:** a number found by measuring, such as an insert rate, a size on disk, a latency or a compression ratio, with how it was measured.
- **Decision:** a choice between real alternatives, and why. For example, a version pin or a schema setting.

Don't write one for things the code or a commit message already says, for guesses, or for anything not yet verified. Ask the user before writing one they did not ask for.

## Steps

1. **Check for an existing lesson.** Run `search` on `unilib-eng` with `project=<project>` and the topic. If a lesson already covers it, update that note, or write a new one and mark the old one `status: superseded-by:<new id>`. Don't write duplicates.
2. **Find the references.** Search `unilib-eng` for the mechanism behind the finding, for example `search("btree index row size limit", module="PG")`. Open the hits with `get_page` and read them. Every `refs` entry must be a citation copied verbatim from a hit you read (`path p.N [version]`, `path §N [version]`, `@citekey p.N`). If nothing in the library supports the mechanism, say so in the note and leave `refs` empty. Never invent a citation.
3. **Check the versions.** Get the versions of the components involved from the project itself, such as image tags, `Cargo.lock` or migrations. If a reference's `[version]` differs from what the project runs, say so in the Mechanism section.
4. **Write the note** at `docs/lessons/YYYY-MM-DD-<slug>.md`, using the date the finding was made:

   ```markdown
   ---
   id: <project>-<yyyy-mm-dd>-<slug>     # lower case, digits, '.', '_', '-'; unique
   title: <one line stating the finding>
   project: <project>                    # lower case, e.g. serenlink
   kind: incident                        # incident | measurement | decision
   date: YYYY-MM-DD
   versions: {postgresql: "16.10", timescaledb: "2.22.0"}
   tags: [timescaledb, compression]
   refs:
     - "TSDB/D04_Compression-Settings.md §2 [TimescaleDB 2.22]"
   status: current                       # or superseded-by:<id>
   ---
   ## Finding
   One paragraph: the claim.

   ## Evidence
   Commands run, numbers measured, error text verbatim.

   ## Mechanism
   Why, in your own words, citing refs. Quote at most a line or two of any source.

   ## Rule
   The do/don't an agent should follow.
   ```

5. **Copyright and privacy.**
   - Paraphrase the references; don't paste their text.
   - Never put a lesson under a public subtree or into a public repo. For SerenLink, `signalkCompressor/` is public, so put lessons at the repo root's `docs/lessons/`.
   - Keep secrets, tokens and customer data out.
6. **Commit the note** in the project repo, following that repo's commit rules.
7. **Index it.** The desktop picks the note up once the repo is pulled there and the library is indexed: a nightly timer runs, or the user can run `index` on `unilib-eng`. Tell the user the note is searchable after that, not before.

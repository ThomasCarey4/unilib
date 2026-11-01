---
name: ingest
description: Import lecture slides, notebooks and reading-list books dropped into the unilib server's _ingest/ folder — classify each file by module, rename it, split large combined decks, and index, all through the unilib MCP tools. Use when the user says "ingest", "/ingest", or asks to add slides to their uni library.
---

# Ingest slides and notebooks into the library

Everything runs on the unilib server through its MCP tools (`ingest_list`, `ingest_peek`, `ingest_outline`, `ingest_convert`, `ingest_place`, `ingest_split`, `ingest_unsort`, `index`, `list_library`). File names are relative to the server's `_ingest/` drop zone. Users put files there over WebDAV (a file manager, or `unilib-client drop FILE…`).

If the unilib tools are unavailable (the server is unreachable), stop and tell the user.

## Steps

1. **List the drop zone:** `ingest_list`. Files go in as PDF, PPTX or `.ipynb`. If it's empty, tell the user and stop.
2. **Convert PPTX:** run `ingest_convert` for each `*.pptx`. On failure the file moves to `_unsorted/`; report it. If the PDF already exists, the tool reports `already converted:` and does nothing.
3. **Classify each PDF or notebook:** run `ingest_peek`. It returns the first pages' text plus a page-1 image. Look at the image when the text is thin or garbled. For a `.ipynb` it returns the cell count and the first 3 sections, with no image. Decide:
   - **Module code:** from `list_library`, which lists the codes, names and classification keywords.
   - **Type:**
     - `L` lecture
     - `T` tutorial/lab
     - `CW` coursework/assessment
     - `N` notes
     - `E` example notebook (`.ipynb` only)
     - `X` other
     - `B` reading-list textbook chapter (PDF only)
   - **`B` signals:** an ISBN/copyright page, PDF author/title metadata, more than 200 pages with a chapter outline, or the user calling it a reading-list book. Books are background material and are searched only with `books=True`.
   - **Number and title:** take the number from "Lecture 3", "Week 3", the file name, or date order. Give a short title (2–5 words). For a notebook, use the related unit/lecture number when it's evident (from the title, a markdown heading or the file name); otherwise use the next free `E` number in that module (see `list_library`).
   - **Not confident:** *plan* the destination `_unsorted/` and show it in the table.
4. **Large decks** (over 60 pages, PDF only; notebooks can't be outlined or split):
   - Run `ingest_outline` and propose parts `START-END:TYPE:NN:Title` at lecture boundaries (bookmarks, `*TITLE*` pages, "Lecture N").
   - If a boundary is ambiguous, look only at the candidate boundary pages (`ingest_peek` with `pages=0, render=N`).
   - With 2+ large decks, dispatch one native Claude subagent per deck (not agy) to propose its parts, then review their proposals.
   - **Books (`B`):** run `ingest_outline` with `book=true`.
     - Pass `start=NN` if the module already has B parts, and `author` if the metadata surname is wrong.
     - The output is a split proposal: one `--part` per chapter, `--skip` ranges for exercise-set pages (kept in the PDF, not indexed), and `# not split` lines for front/back matter (kept only in `_originals/books/`).
     - Shorten long chapter titles to 2–5 words in the table.
5. **Confirm with the user.** Show one table:

   | Source | → Destination | Notes |
   |---|---|---|
   | slides(1).pdf | MATH3001/L03_Newton-Methods.pdf | |
   | KRR_All.pdf | COMP3002: L01 p1–34, L02 p35–80, … | split, original → _originals/ |
   | Burden.pdf | MATH3001: B01 p19–64 Ch1 …, B12 p749–798 Ch12 | book: 12 chapters, 67 exercise skip ranges (123 pages); front/back matter → _originals/books/ |
   | mystery.pdf | _unsorted/ | low confidence |

   Wait for approval or edits. Nothing moves before this confirmation.
6. **Apply** (only after approval):
   - `ingest_place(file, module, type_num, title)`, e.g. `("slides(1).pdf", "MATH3001", "L03", "Newton methods")`. Notebooks are the same, e.g. `("demo.ipynb", "MATH3001", "E09", "Line search demo")` creates `E09_Line-Search-Demo.ipynb`.
   - `ingest_split(file, module, parts, skip)`, e.g. `parts=["1-34:L:01:Intro", "35-80:L:02:Propositional Logic"]`.
   - **Books:** take the `--part` values and the `--skip` values from step 4's proposal and pass them as `parts` and `skip`.
   - **Low-confidence files:** `ingest_unsort(file)` moves it to `_unsorted/`.
   - Report any `duplicate:` lines; the exact copy was dropped.
7. **Index:** run `index` and report the counts.
8. **Summarise:** what went where, what was split, duplicates, and anything left in `_ingest/` or `_unsorted/`. There is no commit: the library lives on the server, outside git.

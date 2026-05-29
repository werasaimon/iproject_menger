# DIAGNOSIS — 2026-05-28T15:12:33+03:00

## Summary
- Pages OK: 12 / 12
- API OK: 13 / 14 (one returns HTTP 400 on missing query arg — by design, but UX-fragile)
- Internal links followed: 18+ unique patterns
- Issues found: 1 P2, 2 P3 (no P0/P1)

## Healthy
| Endpoint / Page | Status | Note |
|---|---|---|
| `/` | 200 (26364B) | data-src loads graph/brain/code_graph/judgments/projects |
| `/p?name=iproject_menger` | 200 (34005B) | |
| `/p?name=iproject_menger&file=bin/say#files` | 200 (34005B) | project.html JS L395-410 auto-opens "files" tab from `#files` hash and calls `openFile(file)` |
| `/graph.html` | 200 (30840B) | legacy LangGraph dispatch graph |
| `/graphs/judgments.html` | 200 (4323B) | reads `/api/graph`, filters event=="judgment" |
| `/graphs/brain.html` | 200 (17516B) | uses `/api/brain/list` + `/api/brain?p=…` |
| `/graphs/projects.html` | 200 (3409B) | uses `/api/meta` + `/api/tree` |
| `/graphs/project_files.html?p=iproject_menger` | 200 (12737B) | |
| `/graphs/code_graph.html?p=istereolab-sdk` | 200 (10651B) | |
| `/graphs/world.html` | 200 (9040B) | |
| `/graphs/reasoning.html` | 200 (8331B) | |
| `/graphs/thinking.html` | 200 (3390B) | renders `THINKING.md` via `/api/file?p=iproject_menger` |
| `/api/projects` | 200 — 5 entries | depz-toolkit, istereolab-sdk, depz-camera-sdk, ifirmware-stereocam, iproject_menger |
| `/api/graph` | 200 — counts: tool_use=47, node_enter=18, node_exit=11, say_assistant=9, judgment=9, run_end=7, edge_activate=6, node_pending=6, run_start=6, **file_change=1** |
| `/api/brain/list` | 200 — `["depz-toolkit","iproject_menger","istereolab-sdk"]` (matches fix expectation) |
| `/api/brain?p=iproject_menger` | 200 — **69 nodes**, 0 edges (≥60 target met; serves `main.jsonl` via newest-mtime selection) |
| `/api/brain?p=istereolab-sdk` | 200 — 16 nodes, 0 edges |
| `/api/brain?p=depz-toolkit` | 200 — 11 nodes, 10 edges |
| `/api/tree?p=iproject_menger` | 200 — 32 entries |
| `/api/tree?p=istereolab-sdk` | 200 — 491 entries |
| `/api/imports?p=istereolab-sdk` | 200 — n_files=491, n_edges=666 |
| `/api/imports?p=iproject_menger` | 200 — n_files=32, n_edges=0 |
| `/api/imports?p=depz-toolkit` | 200 — n_files=67, n_edges=67 |
| `/api/meta?p=iproject_menger` | 200 (620B) | |
| `/api/hypotheses?p=iproject_menger` | 200 (239B) | 1 open hypothesis |
| `/log` | 200 (51347B) | |
| `/status` | 200 (53B) | `{busy:false,...}` |
| `/api/commands` | 200 (1280B) | |
| `/api/file?p=iproject_menger&path=THINKING.md` | 200 (8712B) | |
| `/api/file?p=iproject_menger&path=bin/say` | 200 (5680B) | |
| `/api/files?p=iproject_menger` | 200 (1189B) | |
| `/activity` | 200 (7966B) | |
| `/choices` | 200 (2B) | empty `[]`, OK |
| `/critique` | 200 (1766B) | |
| `/feedback?ts=0` | 200 (12B) | |
| `/trace` | 200 (5843B) | |
| `/api/graph/clear?dry=1` | 200 (12B) | |

## Issues
| Severity | Where | Symptom | Recommended fix |
|---|---|---|---|
| P2 | `server.py:819` — `/api/brain` | When `g` (graph name) is omitted, picks newest-mtime `.jsonl` from `data/brain/<p>/`. For `iproject_menger` today `main.jsonl` (69 nodes, mtime newer) wins over `arch.jsonl` (51 nodes). But mtime ordering is fragile — touching `arch.jsonl` (e.g. legacy writer, rsync) would silently regress brain.html to 51-stub view. | Pick by explicit priority (`main.jsonl` first), or accept `?g=main` as default in brain.html so the choice is explicit, not mtime-derived. |
| P3 | `data/brain/iproject_menger.jsonl` and `data/brain/istereolab-sdk.jsonl` exist as top-level files alongside the new `data/brain/<project>/` dir layout. | Legacy stragglers; `/api/brain/list` ignores them (only checks `iterdir()` for dirs matching `PROJECTS`), so they're dead data. | Move them under their respective project dirs or delete if obsolete. |
| P3 | `/api/git?p=…` returns `HTTP 400 {"error":"bad what"}` when called without `what=…`. | Not a bug — `project.html` always passes `what=log/status/show`. But the bare URL surfaced during diagnostic crawl looks scary. | Add `what` default of `"log"` in `server.py`, or document. Cosmetic. |

## Detailed notes

### JS function definitions — all required handlers present
- `code_graph.html:227` — `function closePanel()` defined
- `world.html:196` — `function closePanel()` defined
- `project_files.html:306` — `function closePanel()` defined
- `project.html:142` — `function tab(m)` handles tab switching; `tab('files')` triggered correctly from `#files` hash (project.html:395-410)
- `brain.html` — `escHtml`, `render`, `load` all defined
- `judgments.html` — `escHtml`, `render`, `load` all defined
- No JS function called-but-not-defined anywhere across the 11 crawled HTML pages.

### Link-anchor (`#files`) audit
All `<a>` links pointing into `/p?name=…&file=…` correctly include `#files`:
- `code_graph.html:203` — file detail panel
- `project_files.html:288, 302` — directory + file links
- `brain.html:264, 287, 316` — judgment/tool refs
- `projects.html:68` — file-tree button
- `reasoning.html:98` — tool_use file refs
- `world.html:190` — `p-tree` button

Three `/p?name=…` links **without** `#files` were flagged but are intentional (they target the project landing page, not the files tab):
- `index.html:227` — card-link to project landing
- `brain.html:295` — bare project label in tool-use row
- `reasoning.html:94` — bare project label

### `file_change` event presence
`/api/graph` returns `file_change=1` (1 occurrence in the last window). `bin/write` is recording — confirmed. Low count just means few file edits since last graph clear; not a bug.

### `/api/brain/list` — fixed expected result
Returns exactly `["depz-toolkit", "iproject_menger", "istereolab-sdk"]` — matches what was promised by today's fix. Note `depz-camera-sdk` and `ifirmware-stereocam` are in `/api/projects` but absent from brain list because they have no `data/brain/<name>/` subdirectory (the listing predicate at `server.py:803` requires both `is_dir()` and `any(*.jsonl)`).

### Brain node count vs target
Target: ≥60 nodes from `main.jsonl`. Actual: 69 nodes (matches `wc -l data/brain/iproject_menger/main.jsonl`). The old `arch.jsonl` has 51 lines — not served, because `main.jsonl` is newer by mtime. See P2 above for fragility note.

### thinking.html
Hard-coded to `iproject_menger`'s `THINKING.md`. Works fine. Not parametrized by `?p=…` — if that's desired, file a separate ticket.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

`iproject_menger` — a **local LAN web app** (`http://192.168.1.103:8078`, never `localhost` in messages) that lets the user research a set of git repos and **pair-chat with Claude / Codex / Gemini** from a browser (desktop or phone). It is its **own** repo: the server, HTML and all manager code live only here. It reads the managed projects from outside, **read-only** — it never writes into them.

The *super-goal* (see `VISION.md`) is not a chat UI. It is to render code as math: states are points, commits are morphisms, bindings are functors, the per-project **brain** graph (`BRAIN.md`, `data/brain/<project>.jsonl`) is the accumulating diagram of our judgments.

Companion docs you should skim once: `VISION.md` (philosophy), `BRAIN.md` (judgment graph + JL), `CLAUDE_AGENT_GUIDE.md` (the protocol of how Claude talks to the site — read this if anything below is unclear).

## Quick start

```bash
bin/ctx                  # compact project state (~80 tokens). Always run first.
bin/serve start          # start server (systemd --user, or direct python3 server.py)
bash bin/watch &         # required daemon — without it site messages reach no AI
python3 server.py        # alternative direct start; listens on :8078
```

The site is gated by a token in `data/.token` (auto-generated). LAN IPs are auto-trusted.

## The pair-chat protocol — how Claude talks to the site

This is the single most important thing to understand. Full details in `CLAUDE_AGENT_GUIDE.md`.

```
browser /say → server.py → data/inbox_new → bin/watch → bin/langgraph_dispatch.py
                                                            │
                            ┌───────────────────────────────┼─────────────────────┐
                            ▼                               ▼                     ▼
                  data/claude_inbox.txt          codex subprocess         gemini subprocess
                  (UserPromptSubmit hook                   │                     │
                   bin/watch-claude reads it)              └──── reply ──────────┘
                            │                                        │
                            ▼                                        ▼
                   Claude replies via bin/say "text"        replies.jsonl (model-tagged)
```

- **Receiving:** site messages arrive via the `UserPromptSubmit` hook `bin/watch-claude` (preferred), prefixed `FROM_SITE:`. If no hook, fall back to the polling pattern in `CLAUDE_AGENT_GUIDE.md` — never write a custom watcher.
- **Replying:** **always via `bin/say "text"`**. Never append to `replies.jsonl` directly — `bin/say` also flips `data/status.json` to `busy:false`, which turns off the site's "🤔 thinking" indicator. Skipping it leaves the spinner stuck.
- **Live status strip:** `bin/claude_tracer.py` (`PostToolUse` hook) writes every tool call to `data/graph_events.jsonl`; the site reads it for the "what Claude is doing now" strip.
- **Long-running work:** call `bin/busy "правлю layout…"` to update the strip text without ending the turn. Use `bin/step KIND "what" "why"` to log a reasoning step (also visible on the site).
- **Choices instead of free-text:** `bin/ask "вопрос" "вар1" "вар2"` renders clickable buttons. **Never use `AskUserQuestion`** — the user is on phone/tablet, terminal dialogs don't reach them. Free-text from the user always overrides any pending choice.

## Architecture — the moving parts

| File | Role |
|---|---|
| `server.py` | Single-file `http.server` on `:8078`. Project registry, read-only git API (`/api/git` whitelist: `log`/`status`/`show`), file tree, `/say`/`/log` chat, `/api/graph` tool trace, `/api/brain*`, `/api/hypotheses`, `/api/depth` (proxies a depth monitor service on a sibling port), `/api/orchestration/*` (LangGraph). Hard `no-cache` headers on every response. |
| `bin/watch` | The persistent daemon. Infinite loop on `data/inbox_new` → forks `langgraph_dispatch.py` per message. **Must always be running**, otherwise site messages reach no AI. |
| `bin/langgraph_dispatch.py` | Real LangGraph `StateGraph` (router → dispatch → evaluate, with retry edges). Picks {claude, codex, gemini} by Shannon entropy + keyword scoring; `@claude` / `@codex` / `@gemini` prefix always wins. Each transition emits to `graph_events.jsonl`. **`subprocess.run` for codex/gemini MUST pass `stdin=subprocess.DEVNULL`** — without it the CLI hangs reading stdin (fixed 2026-05-28). |
| `bin/dispatch` | Older simpler dispatcher (`@codex`/`@gemini` only). Kept for compatibility but `langgraph_dispatch.py` is the real one. |
| `bin/ctx` | Compressed per-project state — branch, last commit, hypothesis counts, focus hint, last inbox lines. Run at session start. Switches active project via `bin/ctx <name>`. |
| `bin/claude_tracer.py` | `PostToolUse` hook — appends every tool call to `data/graph_events.jsonl`. Powers the live "#think" status strip and the LangGraph viewer. |
| `bin/transcript_tail.py` | Daemon that tails the current Claude session transcript (`~/.claude/projects/-home-wera-n-GIT-iproject-menger/*.jsonl`) and streams `thinking` / `text` / `tool_use` events into `graph_events.jsonl` with project/file/line attribution. Without it the UI sees only final replies, not the reasoning. Run with `--once` for one pass. |
| `bin/write` | Wrapper that writes a file from stdin AND emits a `file_change` event (with unified diff stored under `data/diffs/`) into `graph_events.jsonl`. Use instead of plain `>` redirects when an edit should appear on the site live. |
| `bin/critic` | Runs `codex exec -s read-only` with a brutal-but-objective code-critic prompt, writes verdict to `data/critique.md`. Output **not** echoed into Claude's context (visible only on the site). |
| `bin/hyp` | Append a hypothesis card to `data/hypotheses/<project>.jsonl`. Shown on the project page. |
| `bin/pr` | Build a clean PR branch `pr-dev_x-YYYYMMDD` from `dev_x` → `main`, stripping AI meta files (`CLAUDE.md`, `AGENTS.md`, `.claude/`, `.codex/`). Default dry-run; pass `--push` to actually push + `gh pr create`. |
| `bin/serve` | Start/stop/install the systemd `--user` unit `iproject-menger.service` (deploy/unit file). `bin/serve start` also opens Claude in this directory paired with the running server. |
| Static pages | `index.html` (cards + chat), `project.html` (one project's research view), `brain.html`, `graph.html`, `depth.html`, `meta.html`, `qr.html`. No build step; served as-is. `static/mermaid.min.js` is vendored. |
| `orchestrator.py` + `orchestration_api.py` | **Not present in the repo** as of 2026-05-28 — `server.py` imports them inside a `try/except ImportError` so the three `/api/orchestration/*` endpoints are simply disabled when missing. `ORCHESTRATOR_SUMMARY.md` describes the intended DAG (`plan → build_depz / build_sdk → test → validate`) but the files themselves haven't been committed. The real, working LangGraph lives in `bin/langgraph_dispatch.py`. |

## Data layout — append-only, JSONL everywhere

`data/` is **gitignored**. Treat its contents as sensitive (a PAT leaked once). Everything is append-only — **never rewrite a row, never delete one.** Erasure costs Landauer energy; the log is the ground truth.

| File | What it holds |
|---|---|
| `data/inbox.jsonl` | Every user message (browser → server). |
| `data/replies.jsonl` | Every AI reply, tagged `model: "claude" \| "codex" \| "gemini"`. |
| `data/inbox_new` | One-line latest unrouted user message. `bin/watch` consumes + clears it. |
| `data/claude_inbox.txt` | One-shot message routed to Claude. Hook reads + clears. |
| `data/status.json` | `{busy, text, at}` — drives the "thinking" indicator. `at` is epoch and is critical for the `/log` cross-midnight sort. |
| `data/graph_events.jsonl` | Every tool call from the tracer (live status strip + LangGraph viewer). |
| `data/brain/<project>.jsonl` | Per-project judgment graph (see `BRAIN.md`). Nodes + edges as separate lines. |
| `data/hypotheses/<project>.jsonl` | Open / proved / disproved hypothesis cards. |
| `data/active_project` | One-line project name; written by `bin/ctx <name>`. |
| `data/focus_<project>.txt` | Curated focus hint for `bin/critic`. |
| `data/.token` | Server access token (auto-generated; gates non-LAN requests). |

## Code rules — specific to this repo

- **Read-only over managed projects.** `server.py` whitelists git subcommands, hex-validates SHAs, confines paths to each project's root. Don't loosen this — the server is reachable over LAN/tunnel.
- **No build step.** HTML/JS are served directly. Force-refresh in the browser if a JS change isn't visible (server already sends `Cache-Control: no-store`, but a stale tab can still bite).
- **Single venv philosophy doesn't apply here.** This is plain Python 3 + stdlib + optional `langgraph`/`langchain`/`pydantic`/`aiohttp` (see `requirements_langgraph.txt`). Server gracefully degrades if optional deps are missing.
- **Append-only JSONL.** All persistent state. Never rewrite rows, never delete. A changed mind is a new node with a `contradicts` / `refines` edge — not a mutation.
- **Codex/Gemini subprocess calls need `stdin=subprocess.DEVNULL`.** They hang otherwise. Re-verify after any edit to `langgraph_dispatch.py:_run_codex` / `_run_gemini`.
- **Self-modifying hooks are blocked.** The user must install `.claude/settings.json` hooks themselves — Claude can write the command text but cannot run it from inside Claude Code (auto-mode classifier HARD BLOCK).
- **One `bin/watch` at a time.** Multiple racers will steal each other's messages.
- **No explanatory comments.** Only when the *why* is non-obvious (a hidden constraint, a workaround, a subtle invariant).
- **No localhost URLs in replies.** The user is on a tablet; use `192.168.1.103:8078` or relative paths.

## Vocabulary

Project aliases (used in UI and inbox messages):

| Short | Full |
|---|---|
| `[ip]` | `iproject_menger` |
| `[is]` | `istereolab-sdk` |
| `[dt]` | `depz-toolkit` |
| `[dc]` | `depz-camera-sdk` |
| `[fw]` | `ifirmware-stereocam` |

Concept tokens you'll see in code and prose:

| Short | Meaning |
|---|---|
| `iso` | isomorphism — invertible change, 0 Landauer cost (a pure rename) |
| `mono` | monomorphism / embedding — additive change, *superset never subset* |
| `H` | Shannon entropy `H = -Σ p·log₂p` |
| `S` | action `S = LOC × churn` — minimize |
| `K̂` | Kolmogorov complexity estimate ≈ `gzip / raw` |
| JL | the judgment language described in `BRAIN.md` (formal + informal atoms over code objects) |

## Response style

- **Russian for prose, English for identifiers and code.** Short answers, no trailing summaries (the user reads the diff).
- **Colleague, not order-executor.** Think aloud, push back when warranted, finish to a commit.
- **Visual first.** The user thinks in images — prefer a rendered page / graph / inline image over walls of terminal text.
- **Pre-announce confirmation-needing actions in the chat *before* the tool call.** Tool prompts are invisible to the user when driven from the site.
- **Free-text from the user always wins.** A typed message overrides any pending `bin/ask` choice UI.

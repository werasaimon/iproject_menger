# 🤖 Claude Agent Guide — iproject_menger

> How the local site (`:8078`) talks to Claude, what Claude must read/write, and how to keep the dialog alive even when the standard hook is not configured.

---

## What this site is

`iproject_menger` is a **local LAN web app** (`http://127.0.0.1:8078`, LAN: `http://192.168.1.103:8078`) that the user types into. Messages typed in the site's chat are routed through a daemon to one of three AI CLIs (Claude / Codex / Gemini), and the AI's reply shows up back in the chat feed.

```
┌─────────────┐ /say        ┌──────────────┐ inbox_new ┌─────────────────────────┐
│  Browser    │────────────►│  server.py   │──────────►│  bin/watch (daemon)     │
│  chat input │             │  :8078       │           │                         │
└─────────────┘             └──────┬───────┘           │  → bin/langgraph_       │
       ▲                           │                   │    dispatch.py (router) │
       │                           │ /log              │                         │
       │                           ▼                   │  Shannon entropy → one  │
       │                    /replies.jsonl             │  of {claude, codex,     │
       │                                               │  gemini} per message    │
       │                                               └──────┬──────────────────┘
       │                                                      │ if claude
       │                                                      ▼
       │                                               data/claude_inbox.txt
       │                                                      │
       │            ┌───────────────────────────────────┐    │
       └────────────│  bin/say "reply text"             │◄───┴── Claude reads
                    │  → appends to replies.jsonl       │       (via hook or
                    │  → flips status.json busy=false   │        background watcher)
                    └───────────────────────────────────┘
```

---

## Layout — files Claude must know

| Path | Role |
|---|---|
| `server.py` | HTTP server on `:8078`. Serves `index.html`, routes `/say` (incoming), `/log` (chat feed), `/control`, `/api/graph` (live tool trace), etc. Hard `no-cache` headers (`Cache-Control: no-store, no-cache, must-revalidate, max-age=0` + `Pragma: no-cache` + `Expires: 0`). |
| `bin/watch` | **Persistent daemon.** Infinite loop on `data/inbox_new` → forks `python3 bin/langgraph_dispatch.py "$MSG"`. **Must be running**, otherwise no site messages ever reach any AI. |
| `bin/langgraph_dispatch.py` | LangGraph router: classifies each message by Shannon entropy + keyword keywords (`@claude`/`@codex`/`@gemini` explicit prefix wins). For claude-target, writes the message to `data/claude_inbox.txt`. `subprocess.run` for codex/gemini uses **`stdin=subprocess.DEVNULL`** — without it codex hangs reading stdin. |
| `bin/watch-claude` | **`UserPromptSubmit` hook** for Claude Code. Non-blocking: reads `claude_inbox.txt`; if non-empty, prints `FROM_SITE:\n<msg>` and clears the file. Must be wired up in `.claude/settings.json`. |
| `bin/say "text"` | The **only** way Claude writes back to the chat feed. Appends `{ts, at, role:"claude", text}` to `data/replies.jsonl` and flips `data/status.json` to `{busy:false, text:"", at:NOW}` (stops the "thinking…" indicator). The `at` (epoch) field is critical — without it `/log` cannot sort across midnight. |
| `bin/claude_tracer.py` | `PostToolUse` hook. On every Read/Bash/Edit/etc., appends one event to `data/graph_events.jsonl` ({ts, tool, icon, what, bytes}). This is what powers the live "what am I doing now" status strip on the page. |
| `data/inbox.jsonl` | History of every user message (append-only). |
| `data/replies.jsonl` | History of every AI reply (append-only). Read by `/log`. |
| `data/inbox_new` | One-line file with the most recent unrouted user message. `bin/watch` consumes and clears it. |
| `data/claude_inbox.txt` | One-shot: latest message routed to claude. Hook (or fallback watcher) reads + clears. |
| `data/status.json` | `{busy, text, at}` — drives the "🤔 thinking" indicator + live status strip. |
| `data/graph_events.jsonl` | Live trace of Claude's tool calls (for the status strip + the inline LangGraph viewer). |
| `data/` | **gitignored** — chat history is local only (had a leaked PAT once; assume contents are sensitive). |
| `SOUL.md` | gitignored — user's private notes. |

---

## How Claude RECEIVES a site message (two paths)

### A. Normal — `UserPromptSubmit` hook (preferred)

`.claude/settings.json` in the project Claude is running from must contain:

```json
"hooks": {
  "UserPromptSubmit": [{
    "matcher": "",
    "hooks": [{
      "type": "command",
      "command": "/home/wera_n/GIT/iproject_menger/bin/watch-claude"
    }]
  }]
}
```

Then every time the user types a prompt in Claude Code, the hook runs `bin/watch-claude`. If `claude_inbox.txt` has content, it gets injected as `FROM_SITE:\n<msg>` context and the file is cleared. Claude sees the site message inline with the user's local prompt and replies normally.

> ⚠️ **Installing or modifying this hook from inside Claude is blocked** by the auto-mode classifier (self-modification HARD BLOCK). The user must run the install command themselves in their terminal — Claude can only provide the command text.

### B. Fallback — background watcher pattern (when no hook)

Without the hook, Claude must poll `claude_inbox.txt` itself. The reliable pattern: spawn a background bash that blocks until the file is non-empty, then exits — the harness re-invokes Claude when it finishes, surfacing the message.

```bash
F=/home/wera_n/GIT/iproject_menger/data/claude_inbox.txt
S=/home/wera_n/GIT/iproject_menger/data/status.json
for i in $(seq 1 150); do
  if [ -s "$F" ]; then
    MSG=$(cat "$F"); rm -f "$F"
    python3 -c "import json,time;open('$S','w').write(json.dumps({'busy':True,'text':'🤔 thinking','at':time.time()}))"
    echo "FROM_SITE: $MSG"
    exit 0
  fi
  sleep 2
done
echo "TIMEOUT_NO_MSG"
```

After receiving + replying, **restart the watcher** so the next site message wakes Claude too. Always run **exactly one** watcher at a time (multiple would race on the inbox file; whoever wins reads, others just time out).

---

## How Claude REPLIES

**Always via `bin/say`** — no other mechanism. The shell command:

```bash
cd /home/wera_n/GIT/iproject_menger && bin/say "your reply text"
```

This:
1. Appends the reply to `data/replies.jsonl` with `ts` (HH:MM:SS) + `at` (epoch) + `role:"claude"`.
2. Writes `data/status.json` → `{busy:false, ...}` so the "thinking" indicator turns off.
3. The browser's `/log` poll (every ~1.5 s) picks up the new line within seconds.

**Do not** write directly to `replies.jsonl` (you'd skip the status flip and lose the spinner reset). Use `bin/say`.

---

## Live status strip (`#think`)

The site has a status strip at the bottom of the chat that shows what Claude is doing right now. Powered by:

1. `bin/claude_tracer.py` (PostToolUse hook) writes `{ts, tool, icon, what}` to `data/graph_events.jsonl` after every tool call.
2. Frontend `pollStatus()` reads `/api/graph` + `/status`:
   - If `status.busy=true` AND there's a graph_events entry **after** `status.at` AND it's < 15 s old → show `<icon> <verb> · <what>` ("📖 reading · file.py" / "⚡ exec · cmd").
   - Else if `status.busy=true` → show `status.text` ("🤔 thinking").
   - Else → hide the strip.

So as long as the tracer is hooked and `bin/say` flips busy=false at the end, the strip behaves correctly.

---

## Lifecycle — when things break

### Server died (`curl http://127.0.0.1:8078/` returns nothing)
```bash
pkill -f "python3 server.py" 2>/dev/null; sleep 1
cd /home/wera_n/GIT/iproject_menger && python3 server.py > /tmp/menger_server.log 2>&1 &
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8078/
```

If the Claude Code sandbox kills it (exit 144), the user must start it themselves from their terminal — that survives sandbox shutdown:
```
! cd ~/GIT/iproject_menger && python3 server.py
```

### `bin/watch` daemon died (messages typed but never routed)
```bash
pgrep -af "bin/watch" | grep -v grep
# if missing:
cd /home/wera_n/GIT/iproject_menger && bash bin/watch &
```

### Codex/Gemini dispatch hangs at 120s timeout
Look in `bin/langgraph_dispatch.py:_run_codex` / `_run_gemini`. The `subprocess.run` call **must** include `stdin=subprocess.DEVNULL` — otherwise the CLI sits there reading stdin forever. (Fixed 2026-05-28, but worth re-checking after any edit.)

### URL in a reply isn't clickable
`index.html`'s `poll()` linkifies http(s) URLs **after** HTML-escape (XSS-safe). If a reply's URL still shows as plain text, the browser is on a stale JS bundle — force-refresh (`Ctrl+Shift+R`). Server already sends `Cache-Control: no-store`, so this should be a one-time hit per code change.

### Status strip stuck on "thinking" forever
Means `bin/say` was never called → `status.json busy` never flipped. Either the agent crashed mid-reply, or replied via a path other than `bin/say`. Fix: always end agent turns by calling `bin/say "…"` (even if the reply is a short ack).

### Status strip stuck on "⚡ exec · bin/say …" 10 s after reply
Old bug, fixed: `bin/say` itself is a tool call → `claude_tracer` logs it → the strip shows it for ~15 s. The current logic gates on `status.busy` — when busy=false the strip hides regardless of recent graph events.

---

## Mobile (portrait) — layout notes

Both `index.html` and `graph.html` have media queries for `max-width: 768px / 720px`:
- `graph.html` flips `#wrap` from horizontal flex (sidebar 260 px + main) to vertical (runs list on top max-height 38 vh, graph below min-height 60 vh).
- `index.html` inline-graph iframe gets `min-height: 420px` and the parent `div[style*="520px"]` override → `height: auto`.

If the site renders weirdly on phone, check these two `@media` blocks first.

---

## Multi-AI routing — short version

`bin/langgraph_dispatch.py` classifies each user message and picks one or more of {claude, codex, gemini} to handle it. Selection is by **attention weights** (Shannon entropy over keyword scores), with explicit `@codex` / `@gemini` / `@claude` prefixes overriding the heuristic.

- **claude** handles ambiguous/open-ended messages (high entropy or no keywords).
- **codex** handles build / fix / edit / test / compile keywords.
- **gemini** handles explain / search / docs keywords.

Each gets its own subprocess (with `stdin=DEVNULL`). Replies all land in `replies.jsonl` with `model: "claude" | "codex" | "gemini"` so the feed can colour-tag them.

---

## Memory snippets (machine-readable)

Related entries in Claude's per-project memory at
`~/.claude/projects/-home-wera-n-GIT-istereolab-sdk/memory/`:

- `project_iproject_menger.md` — what this project is (super-goal: code as math/topology).
- `reference_iproject_menger_run.md` — short ops cheat-sheet (start/stop, fallback watcher).
- `feedback_telegram_preannounce.md` — when driven from this site, pre-announce confirmation-needing actions in chat **before** the tool call (prompts are invisible to user via the site).
- `feedback_visual_first.md` — user thinks in images; prefer rendered HTML / browser / inline images over walls of terminal text.

---

<sub>Written 2026-05-28 after several sessions of operating the site live (live dialog, hook install hard-blocks, watcher cycles, chat URL linkify, mobile graph layout, status busy-gate, no-cache headers).</sub>

#!/usr/bin/env bash
# Start every background daemon needed for the pair-chat. Idempotent: skips
# anything already running. Foreground CLI agents (claude/codex/gemini) are
# user-driven — open them in VSCode terminals via the tasks below or by hand.
set -u
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

# Match a process by its full command line — not a substring — so we don't
# get false positives from sandbox helpers or from this script itself.
is_up() { ps -eo cmd | grep -Fxq "$1"; }
log()   { printf "  %s\n" "$*"; }

echo "→ booting iproject_menger daemons (cwd=$ROOT)"

# ── 1. server ────────────────────────────────────────────────────────────────
if is_up "python3 server.py" || is_up "python3 ${ROOT}/server.py"; then
  log "[1/4] server.py        already running"
elif systemctl --user is-active --quiet iproject-menger.service 2>/dev/null; then
  log "[1/4] server.py        managed by systemd, ok"
else
  log "[1/4] server.py        starting…"
  nohup python3 server.py > /tmp/menger_server.log 2>&1 &
  disown
fi

# ── 2. watch (inbox → langgraph_dispatch) ────────────────────────────────────
if is_up "bash bin/watch" || is_up "bash ${ROOT}/bin/watch"; then
  log "[2/4] bin/watch        already running"
else
  log "[2/4] bin/watch        starting…"
  nohup bash bin/watch > /tmp/menger_watch.log 2>&1 &
  disown
fi

# ── 3. transcript_tail (Claude reasoning → graph_events) ─────────────────────
if is_up "python3 bin/transcript_tail.py" || is_up "python3 ${ROOT}/bin/transcript_tail.py"; then
  log "[3/4] transcript_tail  already running"
else
  log "[3/4] transcript_tail  starting…"
  nohup python3 bin/transcript_tail.py > /tmp/menger_transcript.log 2>&1 &
  disown
fi

# ── 4. embed_serve (sentence-transformer daemon :8079) ──────────────────────
if is_up "python3 bin/embed_serve.py" || is_up "python3 ${ROOT}/bin/embed_serve.py"; then
  log "[4/4] embed_serve      already running"
else
  log "[4/4] embed_serve      starting… (≈7s first load)"
  nohup python3 bin/embed_serve.py > /tmp/menger_embed.log 2>&1 &
  disown
fi

sleep 1
echo
echo "→ status"
ss -tlpn 2>/dev/null | grep -E "8078|8079" | sed 's/^/  /' || true
echo
echo "→ logs"
echo "  /tmp/menger_server.log  /tmp/menger_watch.log"
echo "  /tmp/menger_transcript.log  /tmp/menger_embed.log"
echo
echo "→ open agent CLIs in VSCode:"
echo "  Ctrl+Shift+P → 'Tasks: Run Task' → '▶ agents (4)'"
echo "  …or just open 4 terminals and run: claude · codex · gemini · codex -c model=\"gpt-5\""
echo
echo "site: http://192.168.1.103:8078/"

#!/usr/bin/env python3
"""
PostToolUse hook — читает JSON из stdin, эмитирует одно событие в graph_events.jsonl.
Должен быть быстрым (синхронный хук блокирует Claude).
"""
import sys, json, time, pathlib, re

EVENTS = pathlib.Path("/home/wera_n/GIT/iproject_menger/data/graph_events.jsonl")

TOOL_ICON = {
    "Bash":      "⚡", "Read":   "📖", "Edit":    "✏️",
    "Write":     "📝", "Agent":  "🤖", "Grep":    "🔎",
    "Glob":      "🗂",  "WebFetch":"🌐", "WebSearch":"🔍",
    "Task":      "📋",
}

def short_input(tool, inp):
    if tool == "Bash":
        cmd = (inp.get("command") or inp.get("cmd") or "")
        return cmd[:80].replace("\n", " ")
    if tool == "Read":
        p = inp.get("file_path","")
        return pathlib.Path(p).name if p else ""
    if tool in ("Edit","Write"):
        p = inp.get("file_path","")
        return pathlib.Path(p).name if p else ""
    if tool == "Agent":
        return (inp.get("description") or inp.get("prompt",""))[:60]
    if tool in ("Grep","Glob"):
        return inp.get("pattern","")[:50]
    if tool in ("WebFetch","WebSearch"):
        return (inp.get("url") or inp.get("query",""))[:60]
    return str(inp)[:60]

try:
    raw = sys.stdin.read()
    if not raw.strip():
        sys.exit(0)
    d = json.loads(raw)
    tool = d.get("tool_name","?")
    inp  = d.get("tool_input") or {}
    resp = d.get("tool_response") or {}

    # output size hint
    out_str = str(resp)
    out_len = len(out_str)

    ev = {
        "ts":    time.time(),
        "t":     time.strftime("%H:%M:%S"),
        "event": "claude_tool",
        "tool":  tool,
        "icon":  TOOL_ICON.get(tool, "🔧"),
        "what":  short_input(tool, inp),
        "bytes": out_len,
    }
    EVENTS.open("a").write(json.dumps(ev, ensure_ascii=False) + "\n")
except Exception:
    pass  # never block Claude

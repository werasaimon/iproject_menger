#!/usr/bin/env python3
"""Добавляет PostToolUse tracer-хуки в .claude/settings.json"""
import json, pathlib, sys

SETTINGS = pathlib.Path("/home/wera_n/GIT/istereolab-sdk/.claude/settings.json")
TRACER   = "python3 /home/wera_n/GIT/iproject_menger/bin/claude_tracer.py"

s = json.loads(SETTINGS.read_text())
existing = s["hooks"]["PostToolUse"]

# не дублировать если уже добавлены
already = any(
    h.get("matcher") in ("Bash","Read","Edit","Write","Agent")
    and any(hk.get("command","").endswith("claude_tracer.py") for hk in h.get("hooks",[]))
    for h in existing
)
if already:
    print("Хуки уже добавлены — ничего не изменено.")
    sys.exit(0)

for tool in ("Bash", "Read", "Edit", "Write", "Agent"):
    existing.append({
        "matcher": tool,
        "hooks": [{"type": "command", "command": TRACER}]
    })

SETTINGS.write_text(json.dumps(s, indent=2, ensure_ascii=False))
print(f"OK — добавлено 5 хуков в {SETTINGS}")
print("Перезапусти Claude (/restart) чтобы хуки загрузились.")

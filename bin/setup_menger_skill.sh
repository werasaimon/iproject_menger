#!/usr/bin/env bash
# Создаёт skill /start-menger и UserPromptSubmit hook для автозапуска сервера
set -e

SKILLS="/home/wera_n/GIT/istereolab-sdk/.claude/skills"
SETTINGS="/home/wera_n/GIT/istereolab-sdk/.claude/settings.json"
ENSURE="/home/wera_n/GIT/iproject_menger/bin/ensure_server.sh"

# ── 1. ensure_server.sh ────────────────────────────────────────────────────────
cat > "$ENSURE" << 'SCRIPT'
#!/usr/bin/env bash
# Идемпотентный запуск iproject_menger — ничего не делает если уже запущен.
pgrep -f "python3 server.py" > /dev/null && exit 0
cd /home/wera_n/GIT/iproject_menger
nohup python3 server.py > /tmp/menger_server.log 2>&1 &
disown
echo "[menger] started (pid=$!)"
SCRIPT
chmod +x "$ENSURE"
echo "✓ ensure_server.sh"

# ── 2. skill SKILL.md ──────────────────────────────────────────────────────────
mkdir -p "$SKILLS/start-menger"
cat > "$SKILLS/start-menger/SKILL.md" << 'SKILL'
---
name: start-menger
description: Start (or restart) the iproject_menger research server on port 8078. Trigger on "запусти менгер", "запусти сервер менгер", "start menger", "перезапусти менгер", "menger server", "/start-menger".
allowed-tools: Bash
---

# Start iproject_menger Server

Запускает или перезапускает сервер исследовательского сайта на порту 8078.

## Step 1 — перезапуск

```bash
pkill -f "python3 server.py" 2>/dev/null || true
sleep 1
cd /home/wera_n/GIT/iproject_menger && nohup python3 server.py > /tmp/menger_server.log 2>&1 &
disown
sleep 2
```

Используй `run_in_background: true` и `dangerouslyDisableSandbox: true`.

## Step 2 — проверка

```bash
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:8078/
tail -4 /tmp/menger_server.log
```

Ожидаем `HTTP 200`.

## Если sandbox убивает процесс (exit 144)

Скажи пользователю запустить вручную:

```
! cd /home/wera_n/GIT/iproject_menger && python3 server.py
```

## После запуска

Сайт: **http://localhost:8078**
Граф агентов (slide-in панель): кнопка **🕸 граф агентов** в хедере.
SKILL
echo "✓ skill start-menger"

# ── 3. UserPromptSubmit hook → settings.json ───────────────────────────────────
python3 - <<'PY'
import json, pathlib

path = pathlib.Path("/home/wera_n/GIT/istereolab-sdk/.claude/settings.json")
d = json.loads(path.read_text())
hooks = d.setdefault("hooks", {})
uph = hooks.setdefault("UserPromptSubmit", [])

cmd = "/home/wera_n/GIT/iproject_menger/bin/ensure_server.sh"
already = any(h.get("command","") == cmd for h in uph)
if not already:
    uph.append({
        "type": "command",
        "command": cmd,
    })
    path.write_text(json.dumps(d, indent=2, ensure_ascii=False))
    print("✓ hook UserPromptSubmit добавлен")
else:
    print("· hook уже есть, пропускаю")
PY

echo ""
echo "Готово! Перезапусти Claude (/restart) чтобы hook подхватился."
echo "После этого сервер будет стартовать автоматически при каждой сессии."
echo ""
echo "Явный вызов: /start-menger"
echo "Сайт: http://localhost:8078"

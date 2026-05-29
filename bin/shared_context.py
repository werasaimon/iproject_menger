#!/usr/bin/env python3
"""Shared context — the *prologue* every agent sees before each reply.

Squeezed snapshot of:
  - what this project is (CLAUDE.md, VISION.md)
  - how the brain/memory works (BRAIN.md)
  - the current distillation of the active project
  - the 4 agents and their roles

Cached on disk so we don't re-read the files every call. Regenerate on demand
(or when the underlying md files change).

Usage:
  bin/shared_context.py                # print to stdout
  bin/shared_context.py --refresh      # force rebuild cache
  bin/shared_context.py --no-distill   # skip the distillation slot (faster)

The dispatcher (bin/langgraph_dispatch.py:_build_prompt) prepends this to every
agent's prompt — Codex, Gemini, OpenAI and Claude all start each turn with the
same shared map of the project.
"""
import sys, os, json, time, pathlib, subprocess

BASE = pathlib.Path("/home/wera_n/GIT/iproject_menger")
DATA = BASE / "data"
CACHE = DATA / "shared_context.md"   # langgraph_dispatch._shared_context reads this
CACHE_TTL = 600   # seconds — refresh every ~10 min

PROJECT_DOCS = [
    ("Project rules",  BASE / "CLAUDE.md",              1200),
    ("Vision",         BASE / "VISION.md",               800),
    ("Brain model",    BASE / "BRAIN.md",                900),
    ("Agent protocol", BASE / "CLAUDE_AGENT_GUIDE.md",   600),
]

def _read_head(path: pathlib.Path, n: int) -> str:
    if not path.exists(): return ""
    try: txt = path.read_text(errors="ignore")
    except Exception: return ""
    return txt[:n].rstrip() + ("…" if len(txt) > n else "")

def _active_project() -> str:
    ap = DATA / "active_project"
    return ap.read_text().strip() if ap.exists() else "iproject_menger"

def _distill_one(project: str, max_bytes: int = 2200) -> str:
    try:
        r = subprocess.run(
            ["python3", str(BASE / "bin" / "distill.py"), "--stdout", project],
            capture_output=True, text=True, timeout=15)
        md = (r.stdout or "").strip()
        return md[:max_bytes] + ("\n…" if len(md) > max_bytes else "")
    except Exception:
        return "(дистилляция недоступна)"

def build(with_distill: bool = True) -> str:
    project = _active_project()
    L = []
    L.append("# ОБЩИЙ КОНТЕКСТ ДЛЯ ВСЕХ АГЕНТОВ")
    L.append("")
    L.append("Ты — один из четырёх равноправных ИИ-агентов в общем чате:")
    L.append("  🤖 Claude · ⚡ Codex · ✦ Gemini · ◆ GPT-5/OpenAI")
    L.append("Префикс @claude/@codex/@gemini/@openai — точная адресация. "
             "Без префикса — диспетчер выбирает по Shannon-энтропии и keywords.")
    L.append("Все вы видите единую append-only память:")
    L.append("  • `data/replies.jsonl` — общая лента, ваши ответы с тегом model")
    L.append("  • `data/brain/<project>/main.jsonl` — суждения с привязкой к проекту")
    L.append("  • `data/relations.jsonl` — граф связей "
             "(refines/builds_on/switch_topic/new_thread/mentions/contradicts/thumbs_up/thumbs_down)")
    L.append("  • `data/embeddings/<project>.jsonl` — 384-d вектор каждого суждения")
    L.append("  • `data/meta.jsonl` — сжатые мета-факты (co_occurrence, frequency, кластеры)")
    L.append("")
    L.append(f"АКТИВНЫЙ ПРОЕКТ: **{project}**")
    L.append("")
    L.append("## Принципы проекта (выжимка)")
    L.append("")
    for title, path, n in PROJECT_DOCS:
        head = _read_head(path, n)
        if not head: continue
        L.append(f"### {title} — `{path.name}`")
        L.append("")
        L.append(head)
        L.append("")
    if with_distill:
        L.append("## Дистилляция активного мозга (top judgments + темы + файлы)")
        L.append("")
        L.append(_distill_one(project, max_bytes=2200))
        L.append("")
    L.append("---")
    L.append("Отвечай как равный коллега, кратко и по делу. "
             "Не повторяй то что уже есть в дистилляции — стройся НА ней.")
    return "\n".join(L)

def get(refresh: bool = False, with_distill: bool = True) -> str:
    if not refresh and CACHE.exists():
        age = time.time() - CACHE.stat().st_mtime
        if age < CACHE_TTL:
            return CACHE.read_text()
    md = build(with_distill=with_distill)
    try: CACHE.write_text(md)
    except Exception: pass
    return md

def main():
    refresh = "--refresh" in sys.argv
    no_dist = "--no-distill" in sys.argv
    sys.stdout.write(get(refresh=refresh, with_distill=not no_dist))

if __name__ == "__main__": main()

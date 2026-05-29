#!/usr/bin/env python3
"""Distill the brain of one (or all) projects into ONE readable markdown file.

The graph is the working memory. This markdown is the human-facing projection:
- top active topics (from meta.jsonl)
- key judgments (highest-degree nodes in relations.jsonl)
- file hubs (most-mentioned files)
- topic clusters (from meta.jsonl topic_cluster, plus on-the-fly grouping)
- open hypotheses

Usage:
  bin/distill.py                 # distill all projects
  bin/distill.py PROJECT         # one project
  bin/distill.py --stdout PROJ   # print to stdout instead of writing file
"""
import sys, json, time, pathlib, collections, re
from datetime import datetime

D       = pathlib.Path("/home/wera_n/GIT/iproject_menger/data")
BRAIN   = D / "brain"
META    = D / "meta.jsonl"
RELS    = D / "relations.jsonl"
HYP     = D / "hypotheses"
DISTILL = D / "distill"
DISTILL.mkdir(parents=True, exist_ok=True)

TOP_TOPICS    = 8
TOP_JUDGMENTS = 12
TOP_FILES     = 10
TOP_CLUSTERS  = 6

def _read_jsonl(p):
    if not p.exists(): return []
    out = []
    for ln in p.read_text(errors="ignore").splitlines():
        try: out.append(json.loads(ln))
        except Exception: continue
    return out

def list_projects():
    if not BRAIN.exists(): return []
    return [p.name for p in sorted(BRAIN.iterdir())
            if p.is_dir() and (p / "main.jsonl").exists()]

def distill(project: str) -> str:
    judgments = [j for j in _read_jsonl(BRAIN / project / "main.jsonl")
                 if j.get("kind") == "judgment"]
    by_id = {j["id"]: j for j in judgments if j.get("id")}

    all_rels = _read_jsonl(RELS)
    rels = [r for r in all_rels if r.get("project") == project]

    all_meta = _read_jsonl(META)
    meta_p = [m for m in all_meta if m.get("project") == project]

    hyps = _read_jsonl(HYP / f"{project}.jsonl")

    # degree centrality on judgments
    deg = collections.Counter()
    for r in rels:
        if r.get("from_kind") == "judgment": deg[r["from_id"]] += 1
        if r.get("to_kind")   == "judgment": deg[r["to_id"]]   += 1

    # file mentions
    file_mentions = collections.Counter(
        r["to_id"] for r in rels if r.get("rel") == "mentions" and r.get("to_kind") == "file")

    # latest meta facts of each kind (compactor appends → take last)
    cooc_latest = next((m for m in reversed(meta_p) if m.get("kind") == "co_occurrence"), None)
    freq_latest = next((m for m in reversed(meta_p) if m.get("kind") == "frequency"), None)
    clusters    = [m for m in meta_p if m.get("kind") == "topic_cluster"]
    clusters    = clusters[-TOP_CLUSTERS:]

    # model distribution
    by_model = collections.Counter(j.get("model", "claude") for j in judgments)

    L = []
    now = datetime.now()
    L.append(f"# 🧠 Brain dump · `{project}` · {now.strftime('%Y-%m-%d %H:%M')}")
    L.append("")
    L.append(f"> Проекция живого графа в читаемый артефакт. "
             f"Граф — рабочая память; этот markdown — то что осело. "
             f"Граф: `data/brain/{project}/main.jsonl` + `data/relations.jsonl`.")
    L.append("")

    # ── overview ─────────────────────────────────────────────────────────────
    L.append("## 📊 Обзор")
    L.append("")
    L.append(f"- Суждений: **{len(judgments)}**")
    L.append(f"- Связей в графе (для этого проекта): **{len(rels)}**")
    L.append(f"- Открытых гипотез: **{len(hyps)}**")
    L.append(f"- Авторы суждений: " + ", ".join(
        f"{m}=**{n}**" for m, n in by_model.most_common()))
    L.append("")

    # ── active topics ────────────────────────────────────────────────────────
    L.append("## ⭐ Активные темы (top-co_occurrence)")
    L.append("")
    if cooc_latest and cooc_latest.get("pairs"):
        for p in cooc_latest["pairs"][:TOP_TOPICS]:
            L.append(f"- **{p['a']}** ↔ **{p['b']}**  · ×{p['n']}")
    else:
        L.append("_(пока нет — запусти `bin/meta_compact.py`)_")
    L.append("")

    if freq_latest and freq_latest.get("top"):
        L.append("### 🏷 Частоты понятий")
        L.append("")
        chips = ", ".join(f"`{t['t']}`·{t['n']}" for t in freq_latest["top"][:24])
        L.append(chips)
        L.append("")

    # ── key judgments ────────────────────────────────────────────────────────
    L.append(f"## 📚 Ключевые суждения (top-{TOP_JUDGMENTS} по degree)")
    L.append("")
    top_j = [jid for jid, _ in deg.most_common(TOP_JUDGMENTS)]
    if not top_j:
        L.append("_(нет суждений с рёбрами — граф ещё пустой)_")
    for jid in top_j:
        j = by_id.get(jid)
        if not j: continue
        model = j.get("model", "claude")
        stmt = (j.get("statement") or "").replace("\n", " ").strip()
        if len(stmt) > 240: stmt = stmt[:240] + "…"
        L.append(f"- **[{model}]** `{jid}` (degree={deg[jid]}, {j.get('ts','')}): {stmt}")
    L.append("")

    # ── file hubs ────────────────────────────────────────────────────────────
    L.append(f"## 📂 Файлы-концентраторы (top-{TOP_FILES} по упоминаниям)")
    L.append("")
    if not file_mentions:
        L.append("_(никаких mentions ещё нет)_")
    for f, n in file_mentions.most_common(TOP_FILES):
        L.append(f"- `{f}` — упомянут **×{n}**")
    L.append("")

    # ── clusters ─────────────────────────────────────────────────────────────
    if clusters:
        L.append("## 🌐 Тематические кластеры (jaccard ≥ 0.45)")
        L.append("")
        for c in clusters:
            tks = ", ".join(f"`{t}`" for t in (c.get("tokens") or [])[:5])
            L.append(f"- **{c.get('summary','кластер')}** · теги: {tks} · "
                     f"size={c.get('size','?')}")
        L.append("")

    # ── relation type breakdown ─────────────────────────────────────────────
    rel_counts = collections.Counter(r.get("rel") for r in rels)
    if rel_counts:
        L.append("## 🔗 Типы связей в проекте")
        L.append("")
        for rel, n in rel_counts.most_common():
            L.append(f"- `{rel}` — **{n}**")
        L.append("")

    # ── open hypotheses ─────────────────────────────────────────────────────
    L.append("## ❓ Открытые гипотезы")
    L.append("")
    if not hyps:
        L.append("_(нет открытых гипотез)_")
    for h in hyps[-10:]:
        text = (h.get("text") or h.get("statement") or "").strip()
        if len(text) > 280: text = text[:280] + "…"
        status = h.get("status", "open")
        ts = h.get("ts", "")
        L.append(f"- [{status}] `{ts}` — {text}")
    L.append("")

    # ── footer ───────────────────────────────────────────────────────────────
    L.append("---")
    L.append(f"_Сгенерировано `bin/distill.py` в {now.strftime('%H:%M:%S')}. "
             f"Live-граф: `/graphs/relations.html`. "
             f"Append-only — этот файл переписывается полностью, исходники не трогаются._")
    return "\n".join(L)

def main():
    args = [a for a in sys.argv[1:] if a]
    to_stdout = False
    if args and args[0] == "--stdout":
        to_stdout = True; args = args[1:]
    projects = args or list_projects()
    for p in projects:
        md = distill(p)
        if to_stdout:
            print(md)
        else:
            out = DISTILL / f"BRAIN_DUMP_{p}.md"
            out.write_text(md)
            print(f"[{p}] → {out} ({len(md)} bytes)")

if __name__ == "__main__": main()

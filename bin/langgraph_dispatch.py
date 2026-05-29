#!/usr/bin/env python3
"""
Настоящий LangGraph агент — не просто if/else роутер.

StateGraph с циклами и conditional edges:

  router → dispatch ──→ evaluate ──→ END          (все агенты успешны)
               ↑____________|  (failed && iter<2 → retry same failed peers)

State накапливает результаты между итерациями.
Каждый переход эмитирует событие в graph_events.jsonl.
"""
import sys, os, json, time, math, re, threading, subprocess, pathlib
from typing import TypedDict

D        = pathlib.Path("/home/wera_n/GIT/iproject_menger/data")
REPLIES  = D / "replies.jsonl"
STATUS   = D / "status.json"
CLAUDE_INBOX = D / "claude_inbox.txt"
EVENTS   = D / "graph_events.jsonl"
LOG      = D / "dispatch.log"
WORK_DIR = "/home/wera_n/GIT/istereolab-sdk"

THRESHOLD  = 0.05
MAX_ITERS  = 2          # максимум retry-циклов
MAX_DELEGATIONS_PER_REPLY = 2

AGENTS = {
    "claude": {
        "label": "🤖 Claude",
        "prefix": "@claude",
        "engine": "claude",
        "persona": "Ты — Claude, один из равноправных ИИ-агентов в общем чате вместе с Codex, Gemini и GPT-5/OpenAI. Все вы видите общую историю разговора и общую базу суждений.",
    },
    "codex": {
        "label": "⚡ Codex",
        "prefix": "@codex",
        "engine": "codex",
        "persona": "Ты — Codex, один из равноправных ИИ-агентов в общем чате вместе с Claude, Gemini и GPT-5/OpenAI. Все вы видите общую историю разговора и общую базу суждений.",
    },
    "gemini": {
        "label": "✦ Gemini",
        "prefix": "@gemini",
        "engine": "gemini",
        "persona": "Ты — Gemini, один из равноправных ИИ-агентов в общем чате вместе с Claude, Codex и GPT-5/OpenAI. Все вы видите общую историю разговора и общую базу суждений.",
    },
    "openai": {
        "label": "◆ GPT-5",
        "prefix": "@openai",
        "engine": "codex",
        "persona": "Ты — GPT-5/OpenAI агент, один из равноправных участников общего чата рядом с Claude, Codex и Gemini. Отвечай как отдельный участник, кратко и по делу.",
    },
}
_only = [a.strip() for a in os.environ.get("DEPZ_AGENTS", "").split(",") if a.strip()]
if _only:
    AGENTS = {a: AGENTS[a] for a in _only if a in AGENTS}
AGENT_IDS = tuple(AGENTS)
AGENT_PREFIX_RE = re.compile(r"^@(" + "|".join(re.escape(a) for a in AGENT_IDS) + r")\b")
DELEGATE_RE = re.compile(
    r"^\s*(?:DELEGATE|CALL|ASK)\s+@(" + "|".join(re.escape(a) for a in AGENT_IDS) + r")\s*:\s*(.+)$",
    re.I | re.M,
)

# ── информационная теория ─────────────────────────────────────────────────────

def shannon_entropy(text: str) -> float:
    if not text: return 0.0
    freq: dict[str, int] = {}
    for c in text: freq[c] = freq.get(c, 0) + 1
    n = len(text)
    return -sum((k/n)*math.log2(k/n) for k in freq.values())

def attention_weights(message: str) -> dict:
    m = message.lower()
    H = shannon_entropy(message)
    base = {a: 0.0 for a in AGENT_IDS}

    m_prefix = AGENT_PREFIX_RE.match(m)
    if m_prefix:
        base[m_prefix.group(1)] = 1.0
        return {**base, "H": H}

    codex_kw  = ["compile","build","cmake","run","test","fix","edit",
                 "запусти","скомпилируй","собери","починь","исправь"]
    gemini_kw = ["research","explain","what is","why","history","compare",
                 "объясни","почему","что такое","сравни","расскажи"]

    cs = sum(1 for kw in codex_kw  if kw in m)
    gs = sum(1 for kw in gemini_kw if kw in m)

    if H > 4.5 and cs + gs >= 2:
        t = cs + gs + 1
        return {**base, "claude":round(1/t,3), "codex":round(cs/t,3),
                "gemini":round(gs/t,3), "H":H}
    if cs > gs: return {**base, "claude":0.1, "codex":0.9, "H":H}
    if gs > cs: return {**base, "claude":0.1, "gemini":0.9, "H":H}
    return {**base, "claude":1.0, "H":H}

# ── события ───────────────────────────────────────────────────────────────────

def emit(event_type: str, **kw):
    entry = {"ts":time.time(), "t":time.strftime("%H:%M:%S"), "event":event_type, **kw}
    try: EVENTS.open("a").write(json.dumps(entry, ensure_ascii=False)+"\n")
    except Exception: pass

def log(msg: str):
    try: LOG.open("a").write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception: pass

def write_reply(text: str, model: str):
    e = {"ts": time.strftime("%H:%M:%S"),
         "at": time.time(),
         "role": model,
         "text": text,
         "model": model}
    try: REPLIES.open("a").write(json.dumps(e, ensure_ascii=False)+"\n")
    except Exception: pass

def set_status(busy: bool, text: str = ""):
    try: STATUS.write_text(json.dumps({"busy":busy,"text":text,"at":time.time()}))
    except Exception: pass

# ── LangGraph state ───────────────────────────────────────────────────────────

class DispatchState(TypedDict):
    message:      str
    attention:    dict   # {agent: weight, H: float}
    active:       list   # агенты этой итерации
    results:      dict   # {agent: result_str} — накапливается
    failed:       list   # провалившиеся на этой итерации
    iterations:   int
    done:         bool

# ── nodes ─────────────────────────────────────────────────────────────────────

def node_router(state: DispatchState) -> DispatchState:
    msg  = state["message"]
    attn = attention_weights(msg)
    H    = attn["H"]
    active = [a for a in AGENT_IDS if attn.get(a,0) >= THRESHOLD]

    emit("node_enter", node="router", H=round(H,3),
         attention={k:v for k,v in attn.items() if k!="H"}, message=msg[:80])
    for a in active:
        emit("edge_activate", src="router", dst=a, weight=round(attn[a],3), H=round(H,3))

    log(f"H={H:.2f} active={active}")
    return {**state, "attention":attn, "active":active,
            "results":{}, "failed":[], "iterations":0, "done":False}

INBOX    = D / "inbox.jsonl"
ACTIVE_P = D / "active_project"
BRAIN_D  = D / "brain"
SHARED_CONTEXT = D / "shared_context.md"

def _tail_jsonl(path: pathlib.Path, n: int) -> list[dict]:
    if not path.exists(): return []
    try:
        lines = path.read_text(errors="ignore").splitlines()[-n:]
    except Exception:
        return []
    out = []
    for ln in lines:
        try: out.append(json.loads(ln))
        except Exception: continue
    return out

def _active_project() -> str:
    try: return ACTIVE_P.read_text().strip() or "iproject_menger"
    except Exception: return "iproject_menger"

def _recent_dialog(n: int = 25) -> str:
    msgs = []
    for u in _tail_jsonl(INBOX, n):
        msgs.append({"at": u.get("at",0), "kind":"user", "text": u.get("text","")})
    for r in _tail_jsonl(REPLIES, n):
        model = r.get("model") or "claude"
        msgs.append({"at": r.get("at",0), "kind": model, "text": r.get("text","")})
    msgs.sort(key=lambda x: x["at"])
    msgs = msgs[-n:]
    icon = {"user":"👤 ты", **{a: spec["label"] for a, spec in AGENTS.items()}}
    lines = []
    for m in msgs:
        label = icon.get(m["kind"], m["kind"])
        txt = (m["text"] or "").strip().replace("\n", " ")
        if len(txt) > 400: txt = txt[:400] + "…"
        lines.append(f"{label}: {txt}")
    return "\n".join(lines)

def _brain_summary(project: str, n: int = 5) -> str:
    p = BRAIN_D / project / "main.jsonl"
    if not p.exists():
        p = BRAIN_D / f"{project}.jsonl"
    judgments = _tail_jsonl(p, n)
    if not judgments: return "(пока пусто)"
    lines = []
    for j in judgments:
        stmt = (j.get("statement") or "").strip().replace("\n"," ")
        if len(stmt) > 220: stmt = stmt[:220] + "…"
        model = j.get("model") or "claude"
        lines.append(f"• [{model}] {stmt}")
    return "\n".join(lines)

def _shared_context(project: str, task: str) -> str:
    """One compact context block injected into every agent prompt.

    Reads `data/shared_context.md` (produced by `bin/shared_context.py` from
    CLAUDE.md / VISION.md / BRAIN.md + a fresh distillation). Take the HEAD —
    that's where the principles and project rules live; the tail is filler."""
    manual = ""
    if SHARED_CONTEXT.exists():
        try:
            manual = SHARED_CONTEXT.read_text(errors="ignore").strip()
            if len(manual) > 7000:
                manual = manual[:7000] + "\n…"
        except Exception:
            manual = ""

    recent = _recent_dialog(10)
    brain = _brain_summary(project, 6)
    relevant = _attention_retrieve(project, task, k=8)

    lines = [
        "=== ОБЩИЙ КОНТЕКСТ ДЛЯ ВСЕХ АГЕНТОВ ===",
        f"active_project: {project}",
        "agents: " + ", ".join(f"{aid}={spec['engine']}" for aid, spec in AGENTS.items()),
        "",
    ]
    if manual:
        lines += ["--- shared_context.md ---", manual, ""]
    lines += ["--- recent_dialog ---", recent or "(пусто)", ""]
    if relevant:
        lines.append("--- relevant_judgments ---")
        for r in relevant:
            stmt = r["stmt"].replace("\n", " ")
            if len(stmt) > 320:
                stmt = stmt[:320] + "..."
            tag = "🌐global" if r.get("scope") == GLOBAL_SCOPE else r["model"]
            lines.append(f"* [{tag}] sim={r['sim']}: {stmt}")
        lines.append("")
    lines += ["--- latest_brain ---", brain, ""]
    return "\n".join(lines)

GLOBAL_SCOPE = "_global"

# Project-agnostic vocabulary: judgments about agents / the user / cross-project
# patterns belong in the global brain. Conservative — a judgment is promoted only
# when it carries this vocab AND mentions no project-local file (see _scope_for).
_GLOBAL_VOCAB_RE = re.compile(
    r"\b(claude|codex|gemini|openai|агент\w*|agent\w*|"
    r"юзер\w*|пользовател\w*|user|"
    r"все\w*\s+проект\w*|меж\w* проект\w*|межпроект\w*|кросс-проект\w*|across projects|cross-project|"
    r"любой проект|all projects)\b", re.IGNORECASE)

def _scope_for(text: str, project: str) -> str:
    """Route a fresh judgment to a brain scope. Default = active project (safe).
    Promote to _global ONLY on a strong signal: global vocab present AND no
    project-local file mention — so we never pollute the global brain by accident."""
    if not text: return project
    if _MENTION_RE.search(text): return project          # talks about a concrete file → project
    if _GLOBAL_VOCAB_RE.search(text): return GLOBAL_SCOPE
    return project

def _attention_retrieve(project: str, query: str, k: int = 8) -> list[dict]:
    """Top-K judgments by cosine similarity to the query (uses embed daemon).

    Searches the active project's brain AND the project-agnostic global brain
    (`_global`) in one ranked pool, so cross-project judgments surface by the
    same cosine+thumbs metric. Falls back silently to [] if the daemon is down."""
    import urllib.request, base64
    scopes = [project, GLOBAL_SCOPE] if project != GLOBAL_SCOPE else [GLOBAL_SCOPE]
    emb_files = [(s, D / "embeddings" / f"{s}.jsonl") for s in scopes]
    if not any(f.exists() for _, f in emb_files): return []
    try:
        import numpy as np
        # encode query
        req = urllib.request.Request("http://127.0.0.1:8079/embed",
            data=query.encode("utf-8"), method="POST",
            headers={"Content-Type": "text/plain; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=3) as r:
            qv = np.array(json.loads(r.read())["vector"], dtype=np.float32)
        ids, vecs, id_scope = [], [], {}
        for scope, emb_file in emb_files:
            if not emb_file.exists(): continue
            for ln in emb_file.read_text(errors="ignore").splitlines():
                try:
                    o = json.loads(ln)
                    ids.append(o["id"]); id_scope[o["id"]] = scope
                    vecs.append(np.frombuffer(base64.b64decode(o["vec_b64"]), dtype=np.float32))
                except Exception: continue
        if not vecs: return []
        M = np.stack(vecs)
        sims = (M @ qv).tolist()
        # add reinforcement bonus from user thumbs (relations.jsonl thumbs_up / thumbs_down)
        rel_p = D / "relations.jsonl"
        bonus = {jid: 0.0 for jid in ids}
        if rel_p.exists():
            for ln in rel_p.read_text(errors="ignore").splitlines():
                try: r = json.loads(ln)
                except Exception: continue
                if r.get("to_kind") != "judgment": continue
                rel = r.get("rel")
                if rel == "thumbs_up":   bonus[r.get("to_id")] = bonus.get(r.get("to_id"),0) + 0.10
                elif rel == "thumbs_down": bonus[r.get("to_id")] = bonus.get(r.get("to_id"),0) - 0.15
        sims_adj = [sims[i] + bonus.get(ids[i], 0.0) for i in range(len(ids))]
        top = sorted(zip(ids, sims_adj), key=lambda x: -x[1])[:k]
        # resolve to judgment text from each scope's brain
        by_id = {}
        for scope in scopes:
            brain_p = BRAIN_D / scope / "main.jsonl"
            if not brain_p.exists(): continue
            for ln in brain_p.read_text(errors="ignore").splitlines():
                try:
                    j = json.loads(ln)
                    if j.get("id"): by_id[j["id"]] = j
                except Exception: continue
        out = []
        for jid, sim in top:
            j = by_id.get(jid)
            if not j: continue
            out.append({"id": jid, "sim": round(sim, 3),
                        "scope": id_scope.get(jid, project),
                        "model": j.get("model","claude"),
                        "stmt": (j.get("statement") or "").strip()})
        return out
    except Exception as exc:
        log(f"attention retrieve failed: {exc}")
        return []

def _build_prompt(agent: str, task: str) -> str:
    persona = AGENTS.get(agent, {}).get("persona", "")
    project = _active_project()

    sections = [
        f"{persona}\n",
        _shared_context(project, task),
        "=== РАВНОПРАВНАЯ ДЕЛЕГАЦИЯ ===",
        "Ты можешь запускать любого другого агента так же, как они могут запускать тебя.",
        "Для делегации выведи отдельную строку строго в формате:",
        "DELEGATE @agent: короткая задача",
        "Доступные agent: " + ", ".join(AGENT_IDS) + ". Не делегируй без необходимости.",
        "",
    ]
    sections.append("=== ЗАПРОС ПОЛЬЗОВАТЕЛЯ (отвечай ТОЛЬКО на это, с учётом контекста выше) ===")
    sections.append(task)
    return "\n".join(sections)

_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яёЁ_]{2,}")

def _toks(s: str) -> set:
    return set(t.lower() for t in _TOKEN_RE.findall(s or ""))

def _edge_to_prev(project: str, text: str, now: float) -> dict | None:
    """Same logic as bin/say: jaccard → shannon_bits → rel."""
    p = BRAIN_D / project / "main.jsonl"
    if not p.exists(): return None
    try:
        prev = None
        for line in p.read_text().splitlines()[::-1]:
            if not line.strip(): continue
            try:
                j = json.loads(line)
                if j.get("kind") == "judgment":
                    prev = j; break
            except Exception: continue
        if not prev: return None
    except Exception: return None
    a = _toks(prev.get("statement") or "")
    b = _toks(text)
    union = a | b
    jaccard = (len(a & b) / len(union)) if union else 0.0
    bits = -math.log2(jaccard + 0.001)
    if   jaccard > 0.6:  rel = "refines"
    elif jaccard > 0.2:  rel = "builds_on"
    elif jaccard > 0.05: rel = "switch_topic"
    else:                rel = "new_thread"
    return {
        "to_prev": prev.get("id"),
        "jaccard": round(jaccard, 3),
        "shannon_bits": round(bits, 2),
        "rel": rel,
        "dt_s": round(now - float(prev.get("at") or now), 1),
    }

def _append_brain(project: str, agent: str, text: str, run_id: str | None = None):
    if not text: return
    import secrets as _secrets
    now = time.time()
    scope = _scope_for(text, project)        # _global for cross-project/agent/user facts, else project
    edge = _edge_to_prev(scope, text, now)
    rec = {
        "id": _secrets.token_hex(6),
        "kind": "judgment",
        "about": project,
        "scope": scope,
        "model": agent,
        "statement": text[:600],
        "value": None,
        "confidence": 0.7,
        "evidence": [],
        "ts": time.strftime("%H:%M:%S"),
        "at": now,
    }
    if run_id: rec["run_id"] = run_id
    if edge:   rec["edge_to_prev"] = edge
    try:
        out_dir = BRAIN_D / scope
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "main.jsonl").open("a").write(json.dumps(rec, ensure_ascii=False)+"\n")
        ev = {"event_type":"judgment", "id":rec["id"], "project":project, "scope":scope,
              "model":agent, "text":text[:200]}
        if edge: ev["edge_to_prev"] = edge
        emit(**ev)
        # also append to unified relations.jsonl so the relation graph is one place
        if edge and edge.get("to_prev"):
            _append_relation(
                from_kind="judgment", from_id=rec["id"],
                to_kind="judgment",   to_id=edge["to_prev"],
                rel=edge["rel"], weight=edge["shannon_bits"],
                source=agent, project=project)
        _detect_mentions(rec["id"], text, project, agent)
        _embed_judgment(rec["id"], text, scope, now)
    except Exception as exc:
        log(f"brain append failed: {exc}")

RELATIONS = D / "relations.jsonl"
_MENTION_RE = re.compile(r"(?:bin|data|graphs|src|include|examples|python|resources)/[\w./_-]+|"
                         r"\b[\w_-]+\.(?:py|html|cpp|h|hpp|md|jsonl|json|sh|yml|toml)\b")

def _append_relation(from_kind: str, from_id: str, to_kind: str, to_id: str,
                     rel: str, weight: float, source: str = "auto",
                     project: str | None = None, extra: dict | None = None):
    rec = {
        "ts": time.strftime("%H:%M:%S"),
        "at": time.time(),
        "from_kind": from_kind, "from_id": from_id,
        "to_kind":   to_kind,   "to_id":   to_id,
        "rel": rel, "weight": round(float(weight), 3),
        "source": source,
    }
    if project: rec["project"] = project
    if extra:   rec.update(extra)
    try: RELATIONS.open("a").write(json.dumps(rec, ensure_ascii=False)+"\n")
    except Exception as exc: log(f"relation append failed: {exc}")

def _embed_judgment(jid: str, text: str, project: str, at: float):
    """Fire-and-forget: POST text to embed daemon, append vec to embeddings/<project>.jsonl."""
    if not (text or "").strip(): return
    import urllib.request, base64
    try:
        req = urllib.request.Request("http://127.0.0.1:8079/embed",
            data=text.encode("utf-8"), method="POST",
            headers={"Content-Type": "text/plain; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=3) as r:
            vec = json.loads(r.read())["vector"]
        import struct
        raw = b"".join(struct.pack("<f", v) for v in vec)
        b64 = base64.b64encode(raw).decode("ascii")
        out_dir = D / "embeddings"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{project}.jsonl").open("a").write(json.dumps({
            "id": jid, "at": at, "vec_b64": b64
        }, ensure_ascii=False) + "\n")
    except Exception as exc:
        log(f"embed_judgment failed: {exc}")

def _detect_mentions(judgment_id: str, text: str, project: str, source: str):
    seen = set()
    for m in _MENTION_RE.findall(text or ""):
        m = m.strip().rstrip(".,:;)")
        if not m or m in seen: continue
        seen.add(m)
        _append_relation(
            from_kind="judgment", from_id=judgment_id,
            to_kind="file",       to_id=m,
            rel="mentions", weight=1.0,
            source=source, project=project)

_PROJECT_ROOTS = {
    "depz-toolkit":        "/home/wera_n/GIT/depz-toolkit",
    "istereolab-sdk":      "/home/wera_n/GIT/istereolab-sdk",
    "ifirmware-stereocam": "/home/wera_n/GIT/ifirmware-stereocam",
    "iproject_menger":     "/home/wera_n/GIT/iproject_menger",
}

SITE_DIR = _PROJECT_ROOTS["iproject_menger"]

def _agent_cwd() -> str:
    """All agents work together from the site folder (iproject_menger).
    Per-project work is reached via absolute paths; access to every project
    is granted by _writable_roots(), not by switching cwd."""
    return SITE_DIR

def _writable_roots() -> list[str]:
    """Every project folder on the site — so any agent can edit any project,
    not just the active one. Derived from _PROJECT_ROOTS (single source)."""
    return list(dict.fromkeys(_PROJECT_ROOTS.values()))

def _run_cli_agent(agent: str, task: str) -> str:
    spec = AGENTS[agent]
    prompt = _build_prompt(agent, task)
    roots = _writable_roots()
    if spec["engine"] == "claude":
        # claude's --add-dir is variadic; keep it last so it can't swallow the prompt
        cmd = ["claude", "-p", "--dangerously-skip-permissions", prompt, "--add-dir", *roots]
        timeout = 180
    elif spec["engine"] == "codex":
        # codex has no --add-dir; cwd is writable under workspace-write, the
        # other project roots are granted via the sandbox config override.
        toml_roots = "[" + ",".join(f'"{r}"' for r in roots) + "]"
        cmd = ["codex", "exec", "-s", "workspace-write",
               "-c", f"sandbox_workspace_write.writable_roots={toml_roots}", prompt]
        timeout = 180
    elif spec["engine"] == "gemini":
        # gemini uses --include-directories (comma-separated) to widen the workspace
        cmd = ["gemini", "--yolo",
               "--include-directories", ",".join(roots), "-p", prompt]
        timeout = 180
    else:
        raise RuntimeError(f"{agent} is not a CLI-backed agent")
    r = subprocess.run(cmd, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=timeout, cwd=_agent_cwd())
    out = (r.stdout or r.stderr or "").strip() or "(нет вывода)"
    if r.returncode != 0:
        raise RuntimeError(f"exit {r.returncode}: {out[:300]}")
    return out

def _delegations_from(text: str, source_agent: str) -> list[tuple[str, str]]:
    out = []
    for m in DELEGATE_RE.finditer(text or ""):
        target, task = m.group(1).lower(), m.group(2).strip()
        if target not in AGENTS or target == source_agent or not task:
            continue
        out.append((target, task[:1200]))
        if len(out) >= MAX_DELEGATIONS_PER_REPLY:
            break
    return out

def _run_delegation(source_agent: str, target_agent: str, task: str, iteration: int) -> str:
    emit("delegation", src=source_agent, dst=target_agent,
         task=task[:120], iteration=iteration)
    emit("node_enter", node=target_agent, task=task[:80],
         delegated_by=source_agent, iteration=iteration)
    set_status(True, f"{source_agent} → {target_agent}: {task[:45]}…")
    out = _run_cli_agent(target_agent, task)
    write_reply(out[:3000], target_agent)
    _append_brain(_active_project(), target_agent, out[:3000])
    emit("node_exit", node=target_agent, ok=True, delegated_by=source_agent,
         chars=len(out), iteration=iteration)
    return out

def node_dispatch(state: DispatchState) -> DispatchState:
    attn    = state["attention"]
    active  = state["active"]
    msg     = state["message"]
    task    = re.sub(r"^@\w+\s*", "", msg).strip() or msg
    itr     = state["iterations"]

    results: dict = dict(state.get("results") or {})
    failed:  list = []
    lock = threading.Lock()

    def run(agent: str):
        w = attn.get(agent, 0)
        emit("node_enter", node=agent, task=task[:80], weight=w, iteration=itr)
        set_status(True, f"{agent}[{itr}]: {task[:50]}…")
        try:
            out = _run_cli_agent(agent, task)

            with lock:
                results[agent] = out
                write_reply(out[:3000], agent)
            _append_brain(_active_project(), agent, out[:3000])
            emit("node_exit", node=agent, ok=True, chars=len(out), iteration=itr)
            for target, delegated_task in _delegations_from(out, agent):
                try:
                    delegated_out = _run_delegation(agent, target, delegated_task, itr)
                    with lock:
                        results[f"{agent}->{target}"] = delegated_out
                except subprocess.TimeoutExpired:
                    with lock: failed.append(target)
                    write_reply("(delegation timeout)", target)
                    emit("node_exit", node=target, ok=False, delegated_by=agent,
                         reason="timeout", iteration=itr)
                except Exception as exc:
                    with lock: failed.append(target)
                    write_reply(f"(delegation error: {str(exc)[:180]})", target)
                    emit("node_exit", node=target, ok=False, delegated_by=agent,
                         reason=str(exc)[:120], iteration=itr)

        except subprocess.TimeoutExpired:
            with lock: failed.append(agent)
            emit("node_exit", node=agent, ok=False, reason="timeout", iteration=itr)
            write_reply(f"(timeout)", agent)
        except Exception as exc:
            with lock: failed.append(agent)
            emit("node_exit", node=agent, ok=False,
                 reason=str(exc)[:120], iteration=itr)

    threads = [threading.Thread(target=run, args=(a,), daemon=True) for a in active]
    for t in threads: t.start()
    for t in threads: t.join(timeout=200)
    set_status(False)

    return {**state, "results":results, "failed":failed,
            "iterations": itr+1}

def node_evaluate(state: DispatchState) -> DispatchState:
    failed = state.get("failed") or []
    itr    = state["iterations"]

    emit("node_enter", node="aggregate",
         success=list((state.get("results") or {}).keys()),
         failed=failed, iterations=itr)

    # Есть провалы И ещё есть попытки → retry тех же равноправных агентов.
    if failed and itr < MAX_ITERS:
        reason = f"Агенты {failed} не справились — повторяю их же без передачи начальнику"
        log(reason)
        new_attn = {a: (1.0 if a in failed else 0.0) for a in AGENT_IDS}
        new_attn["H"] = state["attention"].get("H", 0.0)
        emit("node_exit", node="aggregate", action="retry", reason=reason)
        emit("edge_activate", src="aggregate", dst="dispatch",
             weight=1.0, reason="retry_loop")
        return {**state, "attention":new_attn, "active":failed,
                "failed":[], "done":False}

    emit("node_exit", node="aggregate", action="done", iterations=itr)
    return {**state, "done":True}

# ── conditional routing ───────────────────────────────────────────────────────

def route_evaluate(state: DispatchState) -> str:
    return "end" if state.get("done") else "retry"

# ── graph ─────────────────────────────────────────────────────────────────────

from langgraph.graph import StateGraph, END

def build_graph():
    g = StateGraph(DispatchState)
    g.add_node("router",   node_router)
    g.add_node("dispatch", node_dispatch)
    g.add_node("evaluate", node_evaluate)
    g.set_entry_point("router")
    g.add_edge("router", "dispatch")
    g.add_edge("dispatch", "evaluate")
    # Вот настоящий conditional edge с циклом:
    g.add_conditional_edges("evaluate", route_evaluate, {
        "retry": "dispatch",   # ← цикл назад
        "end":   END,
    })
    return g.compile()

GRAPH = build_graph()

# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    text = " ".join(sys.argv[1:]).strip() if len(sys.argv)>1 else sys.stdin.read().strip()
    if not text: sys.exit(0)

    emit("run_start", message=text[:80])
    result = GRAPH.invoke({
        "message":text, "attention":{}, "active":[],
        "results":{}, "failed":[], "iterations":0, "done":False
    })
    emit("run_end", active=list((result.get("results") or {}).keys()),
         iterations=result.get("iterations",0))

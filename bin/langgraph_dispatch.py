#!/usr/bin/env python3
"""
Настоящий LangGraph агент — не просто if/else роутер.

StateGraph с циклами и conditional edges:

  router → dispatch ──→ evaluate ──→ END          (все агенты успешны)
               ↑____________|  (failed && iter<2 → retry, эскалация к Claude)

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

    if re.match(r"^@codex\b",  m): return {"claude":0.0,"codex":1.0,"gemini":0.0,"H":H}
    if re.match(r"^@gemini\b", m): return {"claude":0.0,"codex":0.0,"gemini":1.0,"H":H}
    if re.match(r"^@claude\b", m): return {"claude":1.0,"codex":0.0,"gemini":0.0,"H":H}

    codex_kw  = ["compile","build","cmake","run","test","fix","edit",
                 "запусти","скомпилируй","собери","починь","исправь"]
    gemini_kw = ["research","explain","what is","why","history","compare",
                 "объясни","почему","что такое","сравни","расскажи"]

    cs = sum(1 for kw in codex_kw  if kw in m)
    gs = sum(1 for kw in gemini_kw if kw in m)

    if H > 4.5 and cs + gs >= 2:
        t = cs + gs + 1
        return {"claude":round(1/t,3),"codex":round(cs/t,3),"gemini":round(gs/t,3),"H":H}
    if cs > gs: return {"claude":0.1,"codex":0.9,"gemini":0.0,"H":H}
    if gs > cs: return {"claude":0.1,"codex":0.0,"gemini":0.9,"H":H}
    return {"claude":1.0,"codex":0.0,"gemini":0.0,"H":H}

# ── события ───────────────────────────────────────────────────────────────────

def emit(event_type: str, **kw):
    entry = {"ts":time.time(), "t":time.strftime("%H:%M:%S"), "event":event_type, **kw}
    try: EVENTS.open("a").write(json.dumps(entry, ensure_ascii=False)+"\n")
    except Exception: pass

def log(msg: str):
    try: LOG.open("a").write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception: pass

def write_reply(text: str, model: str):
    e = {"ts":time.strftime("%H:%M:%S"),"role":"claude","text":text,"model":model}
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
    active = [a for a in ("claude","codex","gemini") if attn.get(a,0) >= THRESHOLD]

    emit("node_enter", node="router", H=round(H,3),
         attention={k:v for k,v in attn.items() if k!="H"}, message=msg[:80])
    for a in active:
        emit("edge_activate", src="router", dst=a, weight=round(attn[a],3), H=round(H,3))

    log(f"H={H:.2f} active={active}")
    return {**state, "attention":attn, "active":active,
            "results":{}, "failed":[], "iterations":0, "done":False}

def _run_codex(task: str) -> str:
    r = subprocess.run(["codex","exec",task], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=180, cwd=WORK_DIR)
    out = (r.stdout or r.stderr or "").strip() or "(нет вывода)"
    if r.returncode != 0:
        raise RuntimeError(f"exit {r.returncode}: {out[:300]}")
    return out

def _run_gemini(task: str) -> str:
    r = subprocess.run(["gemini","-p",task], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=90, cwd=WORK_DIR)
    return (r.stdout or r.stderr or "").strip() or "(нет вывода)"

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
            if agent == "codex":
                out = _run_codex(task)
            elif agent == "gemini":
                out = _run_gemini(task)
            else:
                out = "(→ Claude inbox)"
                try: CLAUDE_INBOX.write_text(msg)
                except Exception: pass
                print("FROM_SITE:"); print(msg)

            with lock:
                results[agent] = out
                write_reply(out[:3000], agent)
            emit("node_exit", node=agent, ok=True, chars=len(out), iteration=itr)

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

    # Есть провалы И ещё есть попытки → retry с эскалацией к Claude
    if failed and itr < MAX_ITERS:
        reason = f"Агенты {failed} не справились — эскалирую к Claude"
        log(reason)
        # Направляем только к Claude, обновляем attention
        new_attn = {**state["attention"], "claude":1.0,
                    **{a:0.0 for a in failed}}
        emit("node_exit", node="aggregate", action="retry", reason=reason)
        emit("edge_activate", src="aggregate", dst="dispatch",
             weight=1.0, reason="retry_loop")
        return {**state, "attention":new_attn, "active":["claude"],
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

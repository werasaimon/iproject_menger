#!/usr/bin/env python3
"""Generate compressed meta-facts from recent brain judgments + relations.

Writes append-only to data/meta.jsonl. Each meta fact is one of:
  - co_occurrence: ["token_a","token_b", ...] appear together N times
  - topic_cluster: K judgments with jaccard ≥ 0.45 collapsed into one summary
  - frequency: token T mentioned N times in last window (rate / surprise)

This is the predictive / probabilistic layer: it loses determinism on purpose,
trading exactness for compact retrieval keys the dispatcher can feed to LLMs
instead of the full chat.

Usage:
  bin/meta_compact.py                 # compact all projects
  bin/meta_compact.py PROJECT         # compact one project
"""
import sys, json, time, math, re, pathlib, collections, hashlib

D = pathlib.Path("/home/wera_n/GIT/iproject_menger/data")
BRAIN = D / "brain"
META  = D / "meta.jsonl"
WINDOW = 200           # how many recent judgments to consider per project
JACCARD_CLUSTER = 0.45
TOP_COOC = 25
TOP_FREQ = 30
STOP = set("это что или для если как там при тут тебе теперь сейчас будет ещё уже было можно нужно надо очень тоже только просто такой такая такие about kind from with this that have been then else what where when which would could should there here just also only those these".split())

TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яёЁ_]{3,}")

def toks(s):
    return [t.lower() for t in TOKEN_RE.findall(s or "") if t.lower() not in STOP]

def jaccard(a: set, b: set) -> float:
    u = a | b
    return (len(a & b) / len(u)) if u else 0.0

def list_projects():
    if not BRAIN.exists(): return []
    return [p.name for p in BRAIN.iterdir() if p.is_dir() and (p / "main.jsonl").exists()]

def load_judgments(project: str, n: int = WINDOW):
    p = BRAIN / project / "main.jsonl"
    if not p.exists(): return []
    lines = p.read_text(errors="ignore").splitlines()[-n:]
    out = []
    for ln in lines:
        try:
            j = json.loads(ln)
            if j.get("kind") == "judgment": out.append(j)
        except Exception: continue
    return out

def emit_meta(rec: dict):
    META.open("a").write(json.dumps(rec, ensure_ascii=False) + "\n")

def compact_project(project: str):
    js = load_judgments(project)
    if not js:
        print(f"[{project}] no judgments — skip"); return
    now = time.time()

    # 1) frequency: per-token mentions
    freq = collections.Counter()
    by_token = collections.defaultdict(list)   # token -> [judgment_id, ...]
    for j in js:
        seen = set()
        for t in toks(j.get("statement","")):
            if t in seen: continue
            seen.add(t); freq[t] += 1
            by_token[t].append(j.get("id"))
    top_freq = freq.most_common(TOP_FREQ)
    fid = hashlib.sha1(f"freq:{project}:{now}".encode()).hexdigest()[:10]
    emit_meta({
        "id": fid, "ts": time.strftime("%H:%M:%S"), "at": now,
        "kind": "frequency", "project": project,
        "window": len(js),
        "top": [{"t": t, "n": n, "examples": by_token[t][-3:]} for t, n in top_freq],
    })

    # 2) co_occurrence: top token pairs appearing in the same judgment
    cooc = collections.Counter()
    for j in js:
        ts = set(toks(j.get("statement",""))) & set(t for t,_ in top_freq)
        for a in ts:
            for b in ts:
                if a < b: cooc[(a,b)] += 1
    top_cooc = [(a,b,c) for (a,b),c in cooc.most_common(TOP_COOC) if c >= 2]
    if top_cooc:
        cid = hashlib.sha1(f"cooc:{project}:{now}".encode()).hexdigest()[:10]
        emit_meta({
            "id": cid, "ts": time.strftime("%H:%M:%S"), "at": now,
            "kind": "co_occurrence", "project": project,
            "window": len(js),
            "pairs": [{"a": a, "b": b, "n": n} for a, b, n in top_cooc],
        })

    # 3) topic_cluster: greedy single-link clusters by jaccard ≥ threshold
    tok_sets = [set(toks(j.get("statement",""))) for j in js]
    parent = list(range(len(js)))
    def find(x):
        while parent[x] != x: parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb: parent[ra] = rb
    for i in range(len(js)):
        for k in range(i+1, len(js)):
            if jaccard(tok_sets[i], tok_sets[k]) >= JACCARD_CLUSTER:
                union(i, k)
    clusters = collections.defaultdict(list)
    for i, _ in enumerate(js): clusters[find(i)].append(i)
    saved = 0
    for root, members in clusters.items():
        if len(members) < 3: continue
        sample_ids = [js[i].get("id") for i in members[-5:]]
        # aggregate tokens for cluster signature
        sig = collections.Counter()
        for i in members: sig.update(tok_sets[i])
        top_tokens = [t for t,_ in sig.most_common(6)]
        ccid = hashlib.sha1((f"cluster:{project}:" + ",".join(sample_ids)).encode()).hexdigest()[:10]
        emit_meta({
            "id": ccid, "ts": time.strftime("%H:%M:%S"), "at": now,
            "kind": "topic_cluster", "project": project,
            "size": len(members),
            "tokens": top_tokens,
            "members": sample_ids,
            "summary": f"кластер из {len(members)} суждений вокруг {', '.join(top_tokens[:4])}",
        })
        saved += 1
    print(f"[{project}] window={len(js)} freq={len(top_freq)} cooc={len(top_cooc)} clusters_saved={saved}")

def main():
    projects = sys.argv[1:] or list_projects()
    for p in projects: compact_project(p)

if __name__ == "__main__": main()

#!/usr/bin/env python3
"""Embedding index over judgments — backfill, append, search.

Layout (append-only):
  data/embeddings/<project>.jsonl  with one row per judgment:
    {"id": "...", "at": epoch, "vec_b64": base64(float32 * 384)}

CLI:
  bin/embed_index.py backfill                  # encode every judgment in every brain
  bin/embed_index.py backfill PROJECT          # one project
  bin/embed_index.py add ID PROJECT TEXT       # encode + append one
  bin/embed_index.py topk PROJECT K "query"    # cosine top-K (uses daemon)
"""
import sys, json, time, base64, pathlib, urllib.request

import numpy as np

D = pathlib.Path("/home/wera_n/GIT/iproject_menger/data")
BRAIN = D / "brain"
EMB   = D / "embeddings"
EMB.mkdir(parents=True, exist_ok=True)
DAEMON = "http://127.0.0.1:8079"

def _post(path: str, body: bytes, ctype="text/plain; charset=utf-8") -> dict:
    req = urllib.request.Request(DAEMON + path, data=body, method="POST",
                                 headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def embed_one(text: str) -> np.ndarray:
    r = _post("/embed", text.encode("utf-8"))
    return np.array(r["vector"], dtype=np.float32)

def embed_batch(texts: list[str]) -> np.ndarray:
    body = json.dumps({"texts": texts}).encode("utf-8")
    r = _post("/batch", body, "application/json")
    return np.array(r["vectors"], dtype=np.float32)

def vec_to_b64(v: np.ndarray) -> str:
    return base64.b64encode(v.astype(np.float32).tobytes()).decode("ascii")

def b64_to_vec(s: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(s), dtype=np.float32)

def load_index(project: str) -> tuple[list[str], np.ndarray]:
    p = EMB / f"{project}.jsonl"
    ids: list[str] = []; vecs: list[np.ndarray] = []
    if not p.exists(): return ids, np.zeros((0, 384), dtype=np.float32)
    for ln in p.read_text(errors="ignore").splitlines():
        try:
            o = json.loads(ln)
            ids.append(o["id"]); vecs.append(b64_to_vec(o["vec_b64"]))
        except Exception: continue
    M = np.stack(vecs) if vecs else np.zeros((0, 384), dtype=np.float32)
    return ids, M

def append(project: str, jid: str, vec: np.ndarray, ts: float | None = None):
    p = EMB / f"{project}.jsonl"
    rec = {"id": jid, "at": ts or time.time(), "vec_b64": vec_to_b64(vec)}
    p.open("a").write(json.dumps(rec, ensure_ascii=False) + "\n")

def known_ids(project: str) -> set:
    p = EMB / f"{project}.jsonl"
    if not p.exists(): return set()
    out = set()
    for ln in p.read_text(errors="ignore").splitlines():
        try: out.add(json.loads(ln).get("id"))
        except Exception: continue
    return out - {None}

def backfill(project: str):
    src = BRAIN / project / "main.jsonl"
    if not src.exists(): print(f"[{project}] no brain"); return
    have = known_ids(project)
    todo: list[tuple[str, str, float]] = []
    for ln in src.read_text(errors="ignore").splitlines():
        try: j = json.loads(ln)
        except Exception: continue
        if j.get("kind") != "judgment": continue
        jid = j.get("id")
        if not jid or jid in have: continue
        stmt = (j.get("statement") or "").strip()
        if not stmt: continue
        todo.append((jid, stmt, float(j.get("at") or 0)))
    if not todo:
        print(f"[{project}] up to date ({len(have)} known)"); return
    print(f"[{project}] embedding {len(todo)} new judgments…")
    # batch encode for speed (chunks of 64)
    BS = 64
    for i in range(0, len(todo), BS):
        chunk = todo[i:i+BS]
        vecs = embed_batch([t for _, t, _ in chunk])
        for (jid, _, ts), vec in zip(chunk, vecs):
            append(project, jid, vec, ts)
    print(f"[{project}] done, total now {len(have)+len(todo)}")

def topk(project: str, k: int, query: str):
    ids, M = load_index(project)
    if M.shape[0] == 0:
        print(f"[{project}] index empty"); return
    qv = embed_one(query)
    # vectors are already L2-normalized by daemon (normalize_embeddings=True)
    sims = M @ qv
    order = np.argsort(-sims)[:k]
    for idx in order:
        print(f"{sims[idx]:.3f}  {ids[idx]}")

def list_projects():
    if not BRAIN.exists(): return []
    return [p.name for p in sorted(BRAIN.iterdir())
            if p.is_dir() and (p / "main.jsonl").exists()]

def main():
    if len(sys.argv) < 2: print(__doc__); sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "backfill":
        projs = sys.argv[2:] or list_projects()
        for p in projs: backfill(p)
    elif cmd == "add":
        _, _, jid, proj, *rest = sys.argv
        text = " ".join(rest)
        append(proj, jid, embed_one(text))
        print(f"ok {proj}:{jid}")
    elif cmd == "topk":
        _, _, proj, k, *rest = sys.argv
        topk(proj, int(k), " ".join(rest))
    else:
        print(__doc__); sys.exit(2)

if __name__ == "__main__": main()

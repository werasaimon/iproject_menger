#!/usr/bin/env python3
"""iProject Manager — a local project-research interface.

A standalone tool (its own project) that registers local git repos as PROJECTS
and exposes a read-only research API (git log as a state/delta timeline, status,
diffstat, file tree) plus a pair chat between the user (browser, incl. phone over
LAN) and the Claude Code agent watching `data/inbox_new`.

  GET /                     -> index.html  (project cards + chat)
  GET /p?name=NAME          -> project.html (one project's research view)
  GET /api/projects         -> [{name, path, branch, head, date, subject}]
  GET /api/git?p=NAME&what=log|status|show[&sha=SHA]
  GET /api/files?p=NAME&sub=RELPATH
  GET /say?text=.. , /log   -> pair chat (data/inbox.jsonl + data/replies.jsonl)
  GET /<page>.html          -> static page from the project dir or graphs/

Read-only git: whitelisted subcommands only, project must be registered, sha is
hex-validated, file paths confined to the project root. LAN only.
"""
import http.server, socketserver, urllib.parse, pathlib, json, time, subprocess, re, os, datetime, gzip, math

BASE   = pathlib.Path(__file__).resolve().parent
DATA   = BASE / "data"
GRAPHS = BASE / "graphs"
DATA.mkdir(exist_ok=True)
INBOX  = DATA / "inbox.jsonl"
NEW    = DATA / "inbox_new"
REPLY  = DATA / "replies.jsonl"
STATUS = DATA / "status.json"
SESSIONS = pathlib.Path.home() / ".claude" / "projects" / "-home-wera-n-GIT-iproject-menger"
PORT   = 8078

def set_status(busy, text=""):
    """Live 'Claude is thinking' signal the browser polls. at=epoch for elapsed timer."""
    try:
        STATUS.write_text(json.dumps({"busy": bool(busy), "text": text, "at": time.time()}))
    except Exception:
        pass

PROJECTS = {
    "depz-toolkit":          pathlib.Path("/home/wera_n/GIT/depz-toolkit"),
    "istereolab-sdk":        pathlib.Path("/home/wera_n/GIT/istereolab-sdk"),
    "depz-camera-sdk":       pathlib.Path("/home/wera_n/GIT/depz-camera-sdk"),
    "ifirmware-stereocam":   pathlib.Path("/home/wera_n/GIT/ifirmware-stereocam"),
    "iproject_menger":       BASE,
}
SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")

def git(path, *args, timeout=8):
    try:
        return subprocess.run(["git", "-C", str(path), *args],
                              capture_output=True, text=True, timeout=timeout).stdout
    except Exception as e:
        return f"(git error: {e})"

def activity_event(d):
    """One site-friendly 'what Claude is doing' event from a Claude Code transcript line.

    The real multiplexer: Claude Code appends every tool_use / tool_result / text /
    thinking to ~/.claude/projects/<slug>/<session>.jsonl live — we just read its tail.
    """
    t = d.get("type"); m = d.get("message")
    ts = d.get("timestamp") or ""
    if ts:
        try: ts = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
        except Exception: ts = ts[11:19]
    out = []
    if t == "assistant" and isinstance(m, dict):
        for c in (m.get("content") or []):
            if not isinstance(c, dict): continue
            k = c.get("type")
            if k == "tool_use":
                inp = c.get("input", {}) or {}
                key = next((x for x in ("file_path", "command", "path", "pattern",
                                        "description", "url", "prompt", "query") if x in inp), None)
                out.append({"ts": ts, "kind": "tool", "tool": c.get("name", ""),
                            "text": str(inp.get(key, ""))[:160]})
            elif k == "text" and c.get("text", "").strip():
                out.append({"ts": ts, "kind": "text", "tool": "", "text": c["text"].strip()[:200]})
            elif k == "thinking" and c.get("thinking", "").strip():
                out.append({"ts": ts, "kind": "thinking", "tool": "", "text": c["thinking"].strip()[:200]})
    elif t == "user" and isinstance(m, dict):
        c = m.get("content")
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    r = part.get("content", "")
                    if isinstance(r, list):
                        r = " ".join(x.get("text", "") for x in r if isinstance(x, dict))
                    out.append({"ts": ts, "kind": "result", "tool": "",
                                "text": str(r).replace("\n", " ").strip()[:140]})
    return out

class H(http.server.BaseHTTPRequestHandler):
    def _s(self, code, body, ctype="application/json; charset=utf-8"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def _page(self, name):
        for base in (BASE, GRAPHS):
            f = base / name
            if f.is_file():
                self._s(200, f.read_bytes(), "text/html; charset=utf-8"); return True
        return False

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        p = u.path

        if p in ("/", "/index.html"):
            if not self._page("index.html"): self._s(404, "no index"); return
            return
        if p == "/p":
            if not self._page("project.html"): self._s(404, "no project page"); return
            return

        if p == "/api/projects":
            out = []
            for name, path in PROJECTS.items():
                branch = git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
                head = git(path, "log", "-1", "--pretty=format:%h\x1f%ad\x1f%s", "--date=short").strip()
                sha, date, subj = (head.split("\x1f") + ["", "", ""])[:3]
                out.append({"name": name, "path": str(path), "branch": branch or "(no git)",
                            "head": sha, "date": date, "subject": subj})
            self._s(200, json.dumps(out, ensure_ascii=False)); return

        if p == "/api/git":
            name = q.get("p", ""); what = q.get("what", "")
            if name not in PROJECTS: self._s(400, json.dumps({"error": "unknown project"})); return
            path = PROJECTS[name]
            if what == "log":
                # \x1e starts each commit record; numstat lines (ins\tdel\tpath)
                # follow until the next record — so each commit carries its Δ.
                raw = git(path, "log", "--no-color", "--numstat",
                          "--pretty=tformat:\x1e%h\x1f%ad\x1f%s", "--date=short", "-50")
                items = []
                for chunk in raw.split("\x1e"):
                    chunk = chunk.strip("\n")
                    if not chunk: continue
                    lines = chunk.split("\n")
                    head = lines[0].split("\x1f")
                    if len(head) != 3: continue
                    ins = dele = files = 0
                    for ln in lines[1:]:
                        parts = ln.split("\t")
                        if len(parts) >= 3 and parts[2].strip():
                            files += 1
                            if parts[0].isdigit(): ins += int(parts[0])
                            if parts[1].isdigit(): dele += int(parts[1])
                    items.append({"sha": head[0], "date": head[1], "subj": head[2],
                                  "ins": ins, "del": dele, "files": files})
                self._s(200, json.dumps(items, ensure_ascii=False)); return
            if what == "status":
                self._s(200, json.dumps({"text": git(path, "status", "-s", "-b")})); return
            if what == "show":
                sha = q.get("sha", "")
                if not SHA_RE.match(sha): self._s(400, json.dumps({"error": "bad sha"})); return
                self._s(200, json.dumps({"text": git(path, "show", "--stat",
                    "--pretty=format:%h  %an  %ad%n%s%n", "--date=short", sha)})); return
            self._s(400, json.dumps({"error": "bad what"})); return

        if p == "/api/files":
            name = q.get("p", ""); sub = q.get("sub", "").lstrip("/")
            if name not in PROJECTS: self._s(400, json.dumps({"error": "unknown project"})); return
            base = PROJECTS[name].resolve()
            target = (base / sub).resolve()
            if not (target == base or str(target).startswith(str(base) + os.sep)) or not target.is_dir():
                self._s(400, json.dumps({"error": "bad path"})); return
            out = []
            for e in sorted(os.scandir(target), key=lambda x: (not x.is_dir(), x.name.lower())):
                if e.name.startswith(".git"): continue
                out.append({"name": e.name, "dir": e.is_dir(),
                            "size": (e.stat().st_size if e.is_file() else 0)})
            self._s(200, json.dumps({"sub": sub, "entries": out[:400]}, ensure_ascii=False)); return

        if p == "/api/file":
            name = q.get("p", ""); rel = q.get("path", "").lstrip("/")
            if name not in PROJECTS: self._s(400, json.dumps({"error": "unknown project"})); return
            base = PROJECTS[name].resolve()
            target = (base / rel).resolve()
            if not (target == base or str(target).startswith(str(base) + os.sep)) or not target.is_file():
                self._s(400, json.dumps({"error": "bad path"})); return
            sz = target.stat().st_size
            if sz > 400_000:
                self._s(200, json.dumps({"path": rel, "size": sz,
                    "text": f"(file too large to preview: {sz} bytes)"})); return
            try:
                txt = target.read_bytes().decode("utf-8")
            except Exception:
                self._s(200, json.dumps({"path": rel, "size": sz, "text": "(binary file)"})); return
            self._s(200, json.dumps({"path": rel, "size": sz, "text": txt}, ensure_ascii=False)); return

        if p == "/api/meta":
            name = q.get("p", "")
            if name not in PROJECTS:
                self._s(400, json.dumps({"error": "unknown"})); return
            proj = PROJECTS[name]
            SRC_EXTS = {".py", ".cpp", ".h", ".hpp", ".c"}
            SKIP_DIRS = {".git", "build", "build_sdk", ".venv", "__pycache__",
                         "dist", "deploy", "staging", "node_modules", ".eggs", ".cache"}

            # --- collect files per top-level module dir ---
            modules = {}
            for f in proj.rglob("*"):
                if f.is_dir(): continue
                if f.suffix not in SRC_EXTS: continue
                parts = f.relative_to(proj).parts
                if any(p in SKIP_DIRS for p in parts): continue
                mod = parts[0] if len(parts) > 1 else "(root)"
                modules.setdefault(mod, {"files": [], "loc": 0, "raw_bytes": 0, "gz_bytes": 0, "symbols": 0})
                try:
                    txt = f.read_bytes()
                    lines = txt.count(b"\n")
                    gz = len(gzip.compress(txt, compresslevel=9))
                    sym = len(re.findall(rb'(?:def |void |bool |int |auto |float )\s+\w+\s*\(', txt))
                    modules[mod]["files"].append(str(f.relative_to(proj)))
                    modules[mod]["loc"] += lines
                    modules[mod]["raw_bytes"] += len(txt)
                    modules[mod]["gz_bytes"] += gz
                    modules[mod]["symbols"] += sym
                except Exception: pass

            # --- git churn per module dir ---
            try:
                raw = git(proj, "log", "--no-color", "--numstat",
                          "--pretty=tformat:%H", "-80")
                cur_files = {}
                for ln in raw.splitlines():
                    ln = ln.strip()
                    if re.match(r'^[0-9a-f]{40}$', ln):
                        cur_files = {}; continue
                    parts = ln.split("\t")
                    if len(parts) == 3 and parts[0].isdigit():
                        top = parts[2].split("/")[0]
                        cur_files[top] = True
                # count commits touching each top dir
                churn = {}
                raw2 = git(proj, "log", "--no-color", "--name-only",
                           "--pretty=tformat:%H", "-80")
                commit_mods = set()
                for ln in raw2.splitlines():
                    ln = ln.strip()
                    if not ln: continue
                    if re.match(r'^[0-9a-f]{40}$', ln):
                        for m in commit_mods: churn[m] = churn.get(m, 0) + 1
                        commit_mods = set(); continue
                    top = ln.split("/")[0]
                    commit_mods.add(top)
                for m in commit_mods: churn[m] = churn.get(m, 0) + 1
            except Exception:
                churn = {}

            # --- shannon entropy of LOC distribution ---
            total_loc = sum(v["loc"] for v in modules.values()) or 1
            h_loc = 0.0
            for v in modules.values():
                p_i = v["loc"] / total_loc
                if p_i > 0: h_loc -= p_i * math.log2(p_i)

            # --- build result modules list ---
            mod_list = []
            for mod, v in sorted(modules.items(), key=lambda x: -x[1]["loc"]):
                if v["loc"] == 0: continue
                c = churn.get(mod, 0)
                k_ratio = round(v["gz_bytes"] / v["raw_bytes"], 3) if v["raw_bytes"] else 1.0
                action_s = v["loc"] * max(c, 1)
                mod_list.append({
                    "name": mod, "loc": v["loc"], "churn": c,
                    "action_s": action_s,
                    "symbols": v["symbols"],
                    "sym_density": round(v["symbols"] / (v["loc"] / 100), 2) if v["loc"] else 0,
                    "kolmogorov": k_ratio,   # gz/raw: lower = more compressible = repetitive
                    "files": len(v["files"])
                })
            mod_list.sort(key=lambda x: -x["action_s"])

            # --- reversibility · branch density · Fisher information ---
            # void_ratio   = void_funcs / total_funcs  → 1 = all sinks (pay Landauer)
            # reverse_score = 1 - void_ratio
            # branch_density = branches per 100 LOC    → proxy for channel capacity C
            # fisher_density = distinct param types / total param tokens → info diversity
            for entry in mod_list:
                mod = entry["name"]
                void_f = non_void_f = branches = params_total = 0
                params_distinct: set = set()
                for rel_f in modules[mod]["files"]:
                    fp = proj / rel_f
                    if not fp.exists(): continue
                    try:
                        txt = fp.read_text(errors="replace")
                        for ln in txt.splitlines():
                            s = ln.strip()
                            if s.startswith("//") or s.startswith("#"): continue
                            if re.search(r'\bvoid\s+\w+\s*\(', ln):   void_f += 1
                            elif re.search(r'\b(?:bool|int|float|double|auto|std::\w+|string)\s+\w+\s*\(', ln):
                                non_void_f += 1
                            if re.search(r'\bif\s*[({\s]|\bswitch\s*\(|\belif\b', ln): branches += 1
                            m2 = re.search(r'\(([^)]{0,200})\)', ln)
                            if m2:
                                for tok in re.findall(r'\b[A-Za-z_]\w*\b', m2.group(1)):
                                    if tok not in {"const","unsigned","signed","long","short",
                                                   "struct","class","self","cls","int","bool",
                                                   "void","float","double","auto"}:
                                        params_total += 1; params_distinct.add(tok)
                    except Exception: pass
                total_f = void_f + non_void_f
                entry["void_ratio"]     = round(void_f / total_f, 3) if total_f else 0.0
                entry["reverse_score"]  = round(1 - entry["void_ratio"], 3)
                entry["branch_density"] = round(branches / (entry["loc"] / 100), 2) if entry["loc"] else 0.0
                entry["fisher_density"] = round(len(params_distinct) / params_total, 3) if params_total else 0.0

            # --- brain graph morphism summary ---
            morphisms = {"iso": 0, "mono": 0, "epi": 0, "forbidden": 0, "total_edges": 0}
            landauer_ops = []
            bd = DATA / "brain" / name
            if bd.is_dir():
                for jf in bd.glob("*.jsonl"):
                    for ln in jf.read_text().splitlines():
                        try:
                            o = json.loads(ln)
                            if o.get("t") == "edge":
                                morphisms["total_edges"] += 1
                                rel = (o.get("rel") or "").lower()
                                if "изо" in rel or "iso" in rel: morphisms["iso"] += 1
                                elif "моно" in rel or "mono" in rel: morphisms["mono"] += 1
                                elif "эпи" in rel or "epi" in rel: morphisms["epi"] += 1
                                if o.get("ok") is False: morphisms["forbidden"] += 1
                                if "эпи" in rel or "epi" in rel:
                                    landauer_ops.append({"op": o.get("from","")+"→"+o.get("to",""),
                                                         "why": o.get("why","")})
                        except Exception: pass

            # --- collision detection: symbols appearing in >1 file ---
            collisions = []
            try:
                r2 = subprocess.run(
                    ["grep", "-rn", "--color=never",
                     "--include=*.py", "--include=*.cpp", "--include=*.h",
                     "--exclude-dir=.venv","--exclude-dir=build","--exclude-dir=.git",
                     r"def \|void \|bool \|int \|auto ", str(proj)],
                    capture_output=True, text=True, timeout=5)
                sym_files: dict = {}
                for ln in r2.stdout.splitlines():
                    m2 = re.search(r'(?:def |void |bool |int |auto |float )\s+(\w+)\s*\(', ln)
                    if not m2: continue
                    sym = m2.group(1)
                    if sym in ("main","self","cls","init","int","bool","float","void"): continue
                    fp = ln.split(":")[0]
                    rel = str(pathlib.Path(fp).relative_to(proj)) if fp.startswith(str(proj)) else fp
                    sym_files.setdefault(sym, set()).add(rel.split("/")[0])
                for sym, mods in sym_files.items():
                    if len(mods) > 1:
                        collisions.append({"symbol": sym, "modules": sorted(mods), "count": len(mods)})
                collisions.sort(key=lambda x: -x["count"])
            except Exception: pass

            # --- delegation inversion detector ---
            # Finds methods where a "canonical" name delegates to a "legacy" name
            # (e.g. control_set calls self.knob_set → wrong direction).
            # Correct direction: legacy = canonical (alias assignment, not a def that calls back).
            inversions = []
            ALIAS_PAIRS = [
                ("control_get",      "knob_get"),
                ("control_set",      "knob_set"),
                ("controls_to_dict", "knobs_to_dict"),
                ("list_filter_controls", "list_filter_knobs"),
                ("camera_control_names", "camera_knob_names"),
                ("is_camera_control",    "is_camera_knob"),
            ]
            try:
                py_files = [f for f in proj.rglob("*.py")
                            if not any(p in SKIP_DIRS for p in f.relative_to(proj).parts)]
                for pf in py_files:
                    txt = pf.read_text(errors="replace")
                    lines = txt.splitlines()
                    rel = str(pf.relative_to(proj))
                    in_def = None; def_start = 0
                    for i, ln in enumerate(lines):
                        m = re.match(r'\s+def (\w+)\s*\(', ln)
                        if m:
                            in_def = m.group(1); def_start = i + 1; continue
                        if in_def and re.match(r'\s+def |\S', ln) and not ln.strip().startswith(("#","\"","'")):
                            in_def = None
                        if in_def:
                            for canonical, legacy in ALIAS_PAIRS:
                                if in_def == canonical and re.search(r'\bself\.' + legacy + r'\b', ln):
                                    inversions.append({
                                        "file": rel, "line": i + 1,
                                        "canonical": canonical, "legacy": legacy,
                                        "snippet": ln.strip()[:100]
                                    })
            except Exception: pass

            self._s(200, json.dumps({
                "modules": mod_list[:20],
                "h_loc": round(h_loc, 3),
                "morphisms": morphisms,
                "landauer_ops": landauer_ops[:10],
                "collisions": collisions[:15],
                "total_loc": total_loc,
                "inversions": inversions[:20],
            }, ensure_ascii=False)); return

        if p == "/api/find":
            name = q.get("p", ""); query = q.get("q", "").strip()
            if name not in PROJECTS or not query or len(query) > 120:
                self._s(200, json.dumps([])); return
            proj = PROJECTS[name]
            exts = ["*.py","*.cpp","*.h","*.hpp","*.c","*.ts","*.js","*.java","*.rs","*.go","*.yaml","*.json","*.md"]
            includes = []
            for e in exts: includes += ["--include", e]
            try:
                skip = ["--exclude-dir=.venv","--exclude-dir=build","--exclude-dir=build_sdk",
                        "--exclude-dir=__pycache__","--exclude-dir=.git","--exclude-dir=dist",
                        "--exclude-dir=node_modules","--exclude-dir=.eggs","--exclude-dir=deploy",
                        "--exclude-dir=staging","--exclude-dir=.cache"]
                r = subprocess.run(
                    ["grep", "-rn", "--color=never"] + skip + includes + [query, str(proj)],
                    capture_output=True, text=True, timeout=5)
                hits, seen = [], set()
                for ln in r.stdout.splitlines()[:60]:
                    parts = ln.split(":", 2)
                    if len(parts) < 3: continue
                    fpath, lineno, snippet = parts[0], parts[1], parts[2].strip()
                    rel = str(pathlib.Path(fpath).relative_to(proj))
                    if rel in seen: continue
                    seen.add(rel)
                    exact = bool(re.search(r'\b' + re.escape(query) + r'\b', snippet))
                    hits.append({"file": rel, "line": int(lineno), "snippet": snippet[:120], "exact": exact})
                def _rank(h):
                    ext = pathlib.Path(h["file"]).suffix.lower()
                    src = 0 if ext in (".cpp",".h",".hpp",".c",".py",".ts",".js",".java",".rs",".go") else 1
                    return (0 if h["exact"] else 1, src, len(h["file"]))
                hits.sort(key=_rank)
                self._s(200, json.dumps(hits[:5], ensure_ascii=False))
            except Exception as e:
                self._s(200, json.dumps([]))
            return

        if p.startswith("/up/"):
            f = (DATA / "uploads" / p[4:]).resolve()
            if str(f).startswith(str((DATA / "uploads").resolve()) + os.sep) and f.is_file():
                self._s(200, f.read_bytes(), "image/" + (f.suffix[1:].lower() or "png")); return
            self._s(404, "no"); return

        # ── pair chat ──
        if p == "/say":
            t = q.get("text", "").strip()
            img_raw = q.get("img", "").strip()
            imgs = [x.strip() for x in img_raw.split(",") if x.strip()] if img_raw else []
            if t or imgs:
                entry = {"ts": time.strftime("%H:%M:%S"),
                         "text": t or ("📎 " + ", ".join(x.split("/")[-1] for x in imgs))}
                if imgs:
                    entry["imgs"] = imgs
                    if len(imgs) == 1:
                        entry["img"] = imgs[0]   # backward compat for single-img log renderer
                with INBOX.open("a") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                files_str = " ".join("[file: " + str(BASE / x) + "]" for x in imgs)
                inbox_text = (t + " " + files_str).strip() if t else files_str
                if not inbox_text: inbox_text = t
                NEW.write_text(inbox_text)
                if t:
                    m = re.match(r"^\[([^\]]+)\]", t)   # [project] prefix → active project
                    if m: (DATA / "active_project").write_text(m.group(1).strip())
                set_status(True, "получил сообщение, думаю…")
                try: (DATA / "choices.json").unlink()   # a message resolves any pending choice
                except FileNotFoundError: pass
            self._s(200, json.dumps({"ok": bool(t or imgs)})); return
        if p == "/choices":
            cf = DATA / "choices.json"
            self._s(200, cf.read_text() if cf.is_file() else "{}"); return
        if p == "/status":
            try:
                d = json.loads(STATUS.read_text()) if STATUS.exists() else {}
            except Exception:
                d = {}
            self._s(200, json.dumps({"busy": d.get("busy", False),
                                     "text": d.get("text", ""), "at": d.get("at", 0)})); return
        if p == "/feedback":
            ts = q.get("ts", ""); v = q.get("v", "")
            if ts and v in ("up", "down"):
                (DATA / "feedback.jsonl").open("a").write(
                    json.dumps({"ts": ts, "v": v, "at": time.strftime("%H:%M:%S")}) + "\n")
            self._s(200, json.dumps({"ok": True})); return
        if p == "/ctx":
            ap = DATA / "active_project"
            name = ap.read_text().strip() if ap.exists() else ""
            cf = DATA / "ctx" / (name + ".md")
            self._s(200, json.dumps({"project": name,
                "text": cf.read_text() if cf.exists() else ""}, ensure_ascii=False)); return
        if p == "/log":
            items = []
            for path, role in ((INBOX, "user"), (REPLY, "claude")):
                if path.exists():
                    for ln in path.read_text().splitlines():
                        try:
                            o = json.loads(ln); o.setdefault("role", role); items.append(o)
                        except Exception: pass
            items.sort(key=lambda x: x.get("ts", ""))
            self._s(200, json.dumps(items[-40:], ensure_ascii=False)); return
        if p == "/trace":
            items = []
            tr = DATA / "trace.jsonl"
            if tr.exists():
                for ln in tr.read_text().splitlines():
                    try: items.append(json.loads(ln))
                    except Exception: pass
            self._s(200, json.dumps(items[-60:], ensure_ascii=False)); return
        if p == "/activity":
            items = []
            try:
                files = sorted(SESSIONS.glob("*.jsonl"), key=lambda x: x.stat().st_mtime)
                f = files[-1] if files else None
            except Exception:
                f = None
            if f and f.is_file():
                try:
                    sz = f.stat().st_size
                    with f.open("rb") as fh:
                        if sz > 220_000: fh.seek(sz - 220_000)
                        chunk = fh.read().decode("utf-8", "replace")
                    for ln in chunk.splitlines()[-160:]:
                        try: d = json.loads(ln)
                        except Exception: continue
                        items.extend(activity_event(d))
                except Exception: pass
            self._s(200, json.dumps(items[-50:], ensure_ascii=False)); return
        if p == "/api/depth":
            import urllib.request as _ur
            DEPTH_PORT = int(q.get("port", "8080"))
            try:
                def _fetch(cmd):
                    url = f"http://localhost:{DEPTH_PORT}/control?cmd={cmd}"
                    return json.loads(_ur.urlopen(url, timeout=1).read())
                met  = _fetch("metrics")
                nmap = _fetch("noise_map")
                cells_flat = [v for row in nmap.get("cells", []) for v in row]
                total = sum(cells_flat)
                H = 0.0
                if total > 0:
                    H = -sum(v/total * math.log2(v/total) for v in cells_flat if v > 0)
                row = {"ts": round(time.time(), 2),
                       "std":      met.get("disparity_std_px"),
                       "points":   met.get("points"),
                       "fill":     met.get("fill_rate"),
                       "z_mean":   met.get("z_mean_m"),
                       "worst":    nmap.get("worst", {}).get("std"),
                       "entropy":  round(H, 4)}
                (DATA / "depth_metrics.jsonl").open("a").write(
                    json.dumps(row, ensure_ascii=False) + "\n")
                nmap["metrics"] = met
                nmap["entropy"] = row["entropy"]
                self._s(200, json.dumps(nmap, ensure_ascii=False))
            except Exception as e:
                self._s(503, json.dumps({"error": str(e)}))
            return

        if p == "/api/depth/history":
            f = DATA / "depth_metrics.jsonl"
            rows = []
            if f.exists():
                for ln in f.read_text().splitlines()[-200:]:
                    try: rows.append(json.loads(ln))
                    except Exception: pass
            self._s(200, json.dumps(rows, ensure_ascii=False)); return

        if p == "/critique":
            cr = DATA / "critique.md"
            self._s(200, json.dumps({"text": cr.read_text() if cr.exists() else ""},
                                    ensure_ascii=False)); return
        if p == "/api/commands":
            cf = DATA / "commands.json"
            self._s(200, cf.read_text() if cf.exists() else "[]"); return
        if p == "/api/brain/list":
            name = q.get("p", "")
            graphs = []
            bd = DATA / "brain" / name
            if name in PROJECTS and bd.is_dir():
                graphs = sorted(f.stem for f in bd.glob("*.jsonl"))
            self._s(200, json.dumps(graphs, ensure_ascii=False)); return
        if p == "/api/brain":
            name = q.get("p", ""); gname = q.get("g", "")
            nodes = []; edges = []
            # new layout: data/brain/<project>/<graph>.jsonl
            bd = DATA / "brain" / name
            if name in PROJECTS and bd.is_dir():
                if not gname:
                    files = sorted(bd.glob("*.jsonl"))
                    bf = files[0] if files else None
                else:
                    bf = bd / (gname + ".jsonl")
                    bf = bf if bf.is_file() else None
                if bf and bf.is_file():
                    for ln in bf.read_text().splitlines():
                        try:
                            o = json.loads(ln)
                            (edges if o.get("t") == "edge" else nodes).append(o)
                        except Exception: pass
            self._s(200, json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False)); return
        if p == "/api/hypotheses":
            name = q.get("p", ""); items = []
            hf = DATA / "hypotheses" / (name + ".jsonl")
            if name in PROJECTS and hf.is_file():
                for ln in hf.read_text().splitlines():
                    try: items.append(json.loads(ln))
                    except Exception: pass
            self._s(200, json.dumps(items, ensure_ascii=False)); return
        if p == "/hyp/add":
            name = q.get("p", ""); text = q.get("text", "").strip()
            if name in PROJECTS and text:
                (DATA / "hypotheses").mkdir(exist_ok=True)
                (DATA / "hypotheses" / (name + ".jsonl")).open("a").write(json.dumps(
                    {"id": str(int(time.time() * 1000)), "text": text, "status": "open",
                     "ts": time.strftime("%Y-%m-%d %H:%M")}, ensure_ascii=False) + "\n")
            self._s(200, json.dumps({"ok": True})); return
        if p == "/hyp/status":
            name = q.get("p", ""); hid = q.get("id", ""); st = q.get("status", "")
            hf = DATA / "hypotheses" / (name + ".jsonl")
            if name in PROJECTS and hf.is_file() and st in ("open", "confirmed", "refuted"):
                out = []
                for ln in hf.read_text().splitlines():
                    try:
                        o = json.loads(ln)
                        if o.get("id") == hid: o["status"] = st
                        out.append(o)
                    except Exception: pass
                hf.write_text("\n".join(json.dumps(o, ensure_ascii=False) for o in out) + "\n")
            self._s(200, json.dumps({"ok": True})); return
        if p == "/hyp/delete":
            name = q.get("p", ""); hid = q.get("id", "")
            hf = DATA / "hypotheses" / (name + ".jsonl")
            if name in PROJECTS and hf.is_file() and hid:
                out = []
                for ln in hf.read_text().splitlines():
                    try:
                        o = json.loads(ln)
                        if o.get("id") != hid: out.append(o)
                    except Exception: pass
                hf.write_text("\n".join(json.dumps(o, ensure_ascii=False) for o in out) + ("\n" if out else ""))
            self._s(200, json.dumps({"ok": True})); return
        if p == "/judgement":
            name = q.get("p", "")
            if name in PROJECTS:
                jf = DATA / "judgements" / (name + ".html")
                if jf.is_file():
                    self._s(200, jf.read_bytes(), "text/html; charset=utf-8"); return
            who = name if name in PROJECTS else "&lt;проект&gt;"
            self._s(200, "<!doctype html><body style='background:#0d1117;color:#8b949e;"
                "font:14px system-ui,sans-serif;padding:26px'>Пока нет суждений по этому проекту.<br><br>"
                "Напиши в чат: <b style='color:#58a6ff'>суждения " + who + "</b> — я соберу глубокий разбор "
                "(граф + карточки + таблицы) и он появится здесь.</body>", "text/html; charset=utf-8"); return

        name = p.lstrip("/")
        if name.endswith(".html") and "/" not in name:
            if self._page(name): return
        # serve graphs/ and static/ subdirectories (path-traversal safe)
        STATIC_DIRS = {"graphs": (GRAPHS, "text/html; charset=utf-8"),
                       "static": (BASE / "static", None)}
        MIME = {".js": "application/javascript", ".css": "text/css",
                ".html": "text/html; charset=utf-8", ".json": "application/json"}
        parts = name.split("/", 1)
        if len(parts) == 2 and parts[0] in STATIC_DIRS:
            base_dir, forced_ctype = STATIC_DIRS[parts[0]]
            target = (base_dir / parts[1]).resolve()
            if str(target).startswith(str(base_dir.resolve())) and target.is_file():
                ext = target.suffix.lower()
                ct = forced_ctype or MIME.get(ext, "application/octet-stream")
                self._s(200, target.read_bytes(), ct); return
        self._s(404, "no")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path == "/upload":
            try:
                n = int(self.headers.get("Content-Length", "0") or "0")
                data = self.rfile.read(n) if n > 0 else b""
                if not data or n > 12_000_000:
                    self._s(400, json.dumps({"error": "empty or too large"})); return
                (DATA / "uploads").mkdir(exist_ok=True)
                fname = q.get("name", "file")
                ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else "bin"
                if len(ext) > 5 or not ext.isalnum(): ext = "bin"
                fn = time.strftime("%H%M%S") + "_" + str(int(time.time() * 1000))[-4:] + "." + ext
                (DATA / "uploads" / fn).write_bytes(data)
                rel = "data/uploads/" + fn
                is_img = ext in ("jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "heif")
                # don't write to inbox here — caller sends text+file together via /say
                self._s(200, json.dumps({"ok": True, "path": rel, "is_img": is_img}))
            except Exception as e:
                self._s(400, json.dumps({"error": str(e)}))
            return
        self._s(404, "no")

    def log_message(self, *_): pass

class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

if __name__ == "__main__":
    print(f"iProject Manager on http://0.0.0.0:{PORT}/  (LAN: http://192.168.1.103:{PORT}/)", flush=True)
    S(("0.0.0.0", PORT), H).serve_forever()

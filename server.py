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
import http.server, socketserver, urllib.parse, pathlib, json, time, subprocess, re, os, datetime, gzip, math, secrets

# ── LangGraph orchestrator ──
try:
    from orchestrator import orchestrator, llm_tracker
    from orchestration_api import (
        handle_orchestration_start,
        handle_orchestration_graph,
        handle_orchestration_llm,
        ORCHESTRATION_HTML
    )
    HAS_ORCHESTRATION = True
except ImportError:
    HAS_ORCHESTRATION = False

BASE   = pathlib.Path(__file__).resolve().parent
DATA   = BASE / "data"
GRAPHS = BASE / "graphs"
DATA.mkdir(exist_ok=True)
INBOX  = DATA / "inbox.jsonl"
NEW    = DATA / "inbox_new"
REPLY  = DATA / "replies.jsonl"
STATUS = DATA / "status.json"
SHARED_CONTEXT = DATA / "shared_context.md"
DIRECT_DISPATCH = DATA / "server_dispatch_enabled"
DISPATCH_MODE = DATA / "dispatch_mode"
AGENT_QUEUES = DATA / "agent_queues"
SESSIONS = pathlib.Path.home() / ".claude" / "projects" / "-home-wera-n-GIT-iproject-menger"
PORT   = 8078
try:
    DIRECT_DISPATCH.write_text("1")
except Exception:
    pass

def set_status(busy, text=""):
    """Live 'Claude is thinking' signal the browser polls. at=epoch for elapsed timer."""
    try:
        STATUS.write_text(json.dumps({"busy": bool(busy), "text": text, "at": time.time()}))
    except Exception:
        pass

def receive_site_message(text="", imgs=None, source="site"):
    """Append a browser/site message to the pair-chat inbox and wake the watcher."""
    text = (text or "").strip()
    imgs = [str(x).strip() for x in (imgs or []) if str(x).strip()]
    if not text and not imgs:
        return False

    entry = {"ts": time.strftime("%H:%M:%S"), "at": time.time(),
             "text": text or ("📎 " + ", ".join(x.split("/")[-1] for x in imgs))}
    if source:
        entry["source"] = source
    if imgs:
        entry["imgs"] = imgs
        if len(imgs) == 1:
            entry["img"] = imgs[0]   # backward compat for single-img log renderer
    with INBOX.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    files_str = " ".join("[file: " + str(BASE / x) + "]" for x in imgs)
    inbox_text = (text + " " + files_str).strip() if text else files_str
    NEW.write_text(inbox_text or text)

    if text:
        m = re.match(r"^\[([^\]]+)\]", text)   # [project] prefix → active project
        if m:
            (DATA / "active_project").write_text(m.group(1).strip())
    set_status(True, "получил сообщение, думаю…")
    try:
        (DATA / "choices.json").unlink()   # a message resolves any pending choice
    except FileNotFoundError:
        pass
    dispatch_message(inbox_text or text)
    return True

def dispatch_message(message):
    """Server-owned dispatch path: no external Codex/Claude session required."""
    message = (message or "").strip()
    if not message:
        return
    mode = DISPATCH_MODE.read_text().strip() if DISPATCH_MODE.exists() else "background"
    if mode == "terminal":
        enqueue_terminal_jobs(message)
        return
    try:
        subprocess.Popen(
            ["python3", str(BASE / "bin" / "langgraph_dispatch.py"), message],
            cwd=str(BASE),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        try:
            (DATA / "dispatch.log").open("a").write(
                f"{time.strftime('%H:%M:%S')} server dispatch error: {exc}\n")
        except Exception:
            pass

def enqueue_terminal_jobs(message):
    """In visible-terminal mode, route message into per-agent queues."""
    AGENT_QUEUES.mkdir(exist_ok=True)
    target = None
    task = message
    m = re.match(r"^@([A-Za-z0-9_-]+)\s+(.*)", message, re.S)
    if m and m.group(1).lower() in AGENTS:
        target = m.group(1).lower()
        task = m.group(2).strip()
    if not target:
        # Match the background default: unaddressed messages go to Claude unless
        # the user explicitly asks a specialized agent.
        low = message.lower()
        if any(k in low for k in ("build", "test", "fix", "edit", "собери", "починь", "исправь")):
            target = "codex"
        elif any(k in low for k in ("explain", "compare", "research", "объясни", "сравни", "почему")):
            target = "gemini"
        else:
            target = "claude"
    rec = {"ts": time.time(), "source": "server", "task": task}
    with (AGENT_QUEUES / f"{target}.jsonl").open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try:
        (DATA / "dispatch.log").open("a").write(
            f"{time.strftime('%H:%M:%S')} queued terminal {target}: {task[:80]}\n")
    except Exception:
        pass

# ── access token ──
# A shared secret gating every request, so the server can be exposed over a public
# tunnel without handing the agent's inbox / git repos to anyone who finds the URL.
# Persisted (gitignored data/) so the URL survives restarts. Empty file → gate off.
TOKEN_FILE = DATA / ".token"
def _load_token():
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text().strip()
        if t:
            return t
    t = secrets.token_urlsafe(18)
    TOKEN_FILE.write_text(t)
    return t
TOKEN = _load_token()

PROJECTS = {
    "depz-toolkit":          pathlib.Path("/home/wera_n/GIT/depz-toolkit"),
    "istereolab-sdk":        pathlib.Path("/home/wera_n/GIT/istereolab-sdk"),
    "ifirmware-stereocam":   pathlib.Path("/home/wera_n/GIT/ifirmware-stereocam"),
    "iproject_menger":       BASE,
}
AGENTS = {
    "claude": {"label": "🤖 Claude", "prefix": "@claude", "engine": "claude", "can_delegate": True},
    "codex":  {"label": "⚡ Codex", "prefix": "@codex", "engine": "codex", "can_delegate": True},
    "gemini": {"label": "✦ Gemini", "prefix": "@gemini", "engine": "gemini", "can_delegate": True},
    "openai": {"label": "◆ GPT-5", "prefix": "@openai", "engine": "codex", "can_delegate": True},
}
_only = [a.strip() for a in os.environ.get("DEPZ_AGENTS", "").split(",") if a.strip()]
if _only:
    AGENTS = {a: AGENTS[a] for a in _only if a in AGENTS}

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
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Requested-With")
        if getattr(self, "_cookie_token", None):
            self.send_header("Set-Cookie",
                f"k={self._cookie_token}; Path=/; Max-Age=31536000; SameSite=Lax")
        self.end_headers()
        self.wfile.write(b)

    def _guard(self):
        """Gate every request behind the shared token (query ?k= once, then cookie).

        LAN addresses (127.x, 192.168.x, 10.x, 172.16-31.x) are trusted and bypass
        the token check — the key is only needed over the public tunnel.
        Returns True if allowed; otherwise emits 403 and returns False.
        """
        if not TOKEN:
            return True
        host = self.client_address[0]
        if (host.startswith("127.") or host.startswith("192.168.") or
                host.startswith("10.") or host == "::1" or
                any(host.startswith(f"172.{i}.") for i in range(16, 32))):
            return True
        u = urllib.parse.urlparse(self.path)
        qk = urllib.parse.parse_qs(u.query).get("k", [""])[0]
        ck = ""
        for part in self.headers.get("Cookie", "").split(";"):
            part = part.strip()
            if part.startswith("k="):
                ck = part[2:]
        if qk == TOKEN:
            if ck != TOKEN:
                self._cookie_token = TOKEN
            return True
        if ck == TOKEN:
            return True
        self._s(403, "forbidden — need access key", "text/plain; charset=utf-8")
        return False

    def _page(self, name):
        for base in (BASE, GRAPHS):
            f = base / name
            if f.is_file():
                self._s(200, f.read_bytes(), "text/html; charset=utf-8"); return True
        return False

    def do_GET(self):
        if not self._guard():
            return
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

        if p == "/api/agents":
            out = [{"id": aid, **spec} for aid, spec in AGENTS.items()]
            self._s(200, json.dumps(out, ensure_ascii=False)); return
        if p == "/api/dispatch_mode":
            mode = DISPATCH_MODE.read_text().strip() if DISPATCH_MODE.exists() else "background"
            self._s(200, json.dumps({"mode": mode}, ensure_ascii=False)); return

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

        if p == "/api/imports":
            # File-level dependency graph: parse #include / import directives.
            # Returns {nodes:[{path,ext,size}], edges:[{source,target,kind}]}.
            name = q.get("p", "")
            if name not in PROJECTS: self._s(400, json.dumps({"error":"unknown project"})); return
            base = PROJECTS[name].resolve()
            files = []
            try:
                gout = subprocess.run(["git","-C",str(base),"ls-files"],
                                      capture_output=True, text=True, timeout=5)
                if gout.returncode == 0 and gout.stdout.strip():
                    files = [l for l in gout.stdout.splitlines() if l]
            except Exception: pass
            if not files:
                for root, dirs, fs in os.walk(base):
                    dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules","__pycache__","build","dist")]
                    for f in fs:
                        rel = os.path.relpath(os.path.join(root, f), base)
                        files.append(rel)
                        if len(files) >= 1500: break
                    if len(files) >= 1500: break
            files = files[:1500]

            include_re = re.compile(rb'^\s*#\s*include\s+[<"]([^>"]+)[>"]', re.M)
            py_import_re = re.compile(rb'^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))', re.M)
            js_import_re = re.compile(rb'(?:^|\n)\s*(?:import|require)\s*\(?["\']([^"\']+)["\']', re.M)

            # build basename index for resolving headers without full path
            by_basename = {}
            for f in files:
                base_name = f.rsplit("/", 1)[-1]
                by_basename.setdefault(base_name, []).append(f)

            nodes = []
            edges = []
            for rel in files:
                ext = rel.rsplit(".", 1)[-1].lower() if "." in rel.rsplit("/",1)[-1] else ""
                try: sz = (base / rel).stat().st_size
                except: sz = 0
                nodes.append({"path": rel, "ext": ext, "size": sz})

                if ext not in ("h","hpp","cpp","c","cc","cxx","py","ts","tsx","js","jsx","mjs"):
                    continue
                try:
                    data = (base / rel).read_bytes()[:200_000]
                except Exception:
                    continue

                deps = set()
                if ext in ("h","hpp","cpp","c","cc","cxx"):
                    for m in include_re.finditer(data):
                        inc = m.group(1).decode("utf-8","ignore")
                        # try exact basename match within project
                        base_inc = inc.rsplit("/",1)[-1]
                        if base_inc in by_basename:
                            for cand in by_basename[base_inc]:
                                if cand != rel:
                                    deps.add(cand)
                elif ext == "py":
                    for m in py_import_re.finditer(data):
                        mod = (m.group(1) or m.group(2) or b"").decode("utf-8","ignore")
                        first = mod.split(".")[-1]
                        for cand in by_basename.get(first + ".py", []):
                            if cand != rel: deps.add(cand)
                else:  # js / ts
                    for m in js_import_re.finditer(data):
                        target = m.group(1).decode("utf-8","ignore")
                        target_base = target.rsplit("/",1)[-1]
                        for ext_try in (".js",".ts",".jsx",".tsx",""):
                            cands = by_basename.get(target_base + ext_try, [])
                            for cand in cands:
                                if cand != rel: deps.add(cand); break

                for d in deps:
                    edges.append({"source": rel, "target": d, "kind": "include" if ext in ("h","hpp","cpp","c","cc","cxx") else "import"})

            self._s(200, json.dumps({"project": name, "n_files": len(nodes),
                                     "n_edges": len(edges),
                                     "nodes": nodes, "edges": edges}, ensure_ascii=False)); return

        if p == "/api/tree":
            # Recursive flat file list for graph view. Uses git ls-files if repo,
            # else walks the dir. Caps at 1500 files to keep the graph render-able.
            name = q.get("p", "")
            if name not in PROJECTS: self._s(400, json.dumps({"error":"unknown project"})); return
            base = PROJECTS[name].resolve()
            files = []
            try:
                git_out = subprocess.run(["git","-C",str(base),"ls-files"],
                                         capture_output=True, text=True, timeout=5)
                if git_out.returncode == 0 and git_out.stdout.strip():
                    files = [l for l in git_out.stdout.splitlines() if l]
            except Exception: pass
            if not files:
                for root, dirs, fs in os.walk(base):
                    dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules","__pycache__","build","dist")]
                    for f in fs:
                        rel = os.path.relpath(os.path.join(root, f), base)
                        files.append(rel)
                        if len(files) >= 1500: break
                    if len(files) >= 1500: break
            files = files[:1500]
            entries = []
            # K̂ (Kolmogorov estimate) = gzip(file) / raw(file) — low ratio = high redundancy/compressibility
            for rel in files:
                fp = base / rel
                try:
                    sz = fp.stat().st_size
                except Exception: sz = 0
                k = None
                # Only compute K̂ for small-ish text files (< 64 KB) to keep response snappy
                if 0 < sz < 64_000:
                    try:
                        data = fp.read_bytes()
                        compressed = gzip.compress(data, compresslevel=6)
                        k = round(len(compressed) / max(1, sz), 3)
                    except Exception: pass
                entries.append({"path": rel, "size": sz, "k": k})
            self._s(200, json.dumps({"project": name, "count": len(entries),
                                     "entries": entries}, ensure_ascii=False)); return

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
            ok = receive_site_message(t, imgs, "say")
            self._s(200, json.dumps({"ok": ok})); return
        if p == "/hook":
            items = []
            if INBOX.exists():
                for ln in INBOX.read_text().splitlines()[-20:]:
                    try: items.append(json.loads(ln))
                    except Exception: pass
            self._s(200, json.dumps({"ok": True, "messages": items}, ensure_ascii=False)); return
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
        if p == "/claims":
            # Live file-lock map folded from append-only claims.jsonl
            # (written by bin/claim). collisions = files held by >1 agent.
            state = {}
            cf = DATA / "claims.jsonl"
            if cf.exists():
                for ln in cf.read_text().splitlines():
                    try: e = json.loads(ln)
                    except Exception: continue
                    state[(e["agent"], e["file"])] = e["op"]
            holders = {}
            for (agent, f), op in state.items():
                if op == "take":
                    holders.setdefault(f, []).append(agent)
            collisions = {f: a for f, a in holders.items() if len(a) > 1}
            self._s(200, json.dumps({"holders": holders,
                                     "collisions": collisions}, ensure_ascii=False)); return
        if p == "/state":
            # Live state of EVERY agent, derived from graph_events.jsonl
            # (node_enter/node_exit already carry node=<agent>). No extra logging.
            # reason carries the exit cause ("timeout" / error text) for the UI.
            st = {a: {"busy": False, "text": "", "at": 0.0, "ok": None, "reason": ""}
                  for a in AGENTS}
            gf = DATA / "graph_events.jsonl"
            if gf.exists():
                for ln in gf.read_text().splitlines()[-2000:]:
                    try: e = json.loads(ln)
                    except Exception: continue
                    a = e.get("node")
                    if a not in st: continue
                    ts = e.get("ts", 0)
                    if e.get("event") == "node_enter":
                        st[a].update(busy=True, text=e.get("task", ""), at=ts, ok=None, reason="")
                    elif e.get("event") == "node_exit":
                        st[a].update(busy=False, at=ts, ok=e.get("ok"),
                                     reason=e.get("reason", ""))
            now = time.time()
            for a in st:                       # stale busy (>120s) → treat as timeout
                if st[a]["busy"] and (now - st[a]["at"]) > 120:
                    st[a].update(busy=False, ok=False, reason="timeout")
            self._s(200, json.dumps({"now": now, "agents": st}, ensure_ascii=False)); return
        if p == "/api/activity":
            # Single consolidated live view for the main-page indicator:
            # who's running now, the current stage, the wait timer, the last
            # few execution steps, and an explicit blocked/timeout alert that
            # would otherwise only live in replies.jsonl.
            STALE = 120
            evs = []
            gf = DATA / "graph_events.jsonl"
            if gf.exists():
                for ln in gf.read_text().splitlines()[-400:]:
                    try: evs.append(json.loads(ln))
                    except Exception: continue
            now = time.time()
            STEP = {"node_enter", "node_exit", "delegation", "run_start", "run_end"}
            def label(e):
                ev, n = e.get("event"), e.get("node", "")
                if ev == "node_enter":
                    t = (e.get("task") or "").strip()
                    return f"▶ {n}" + (f": {t[:60]}" if t else "")
                if ev == "node_exit":
                    if e.get("ok") is False:
                        return f"✗ {n}: {(e.get('reason') or 'fail')[:50]}"
                    return f"✓ {n}"
                if ev == "delegation":
                    return f"↪ {e.get('src','')}→{e.get('dst','')}"
                if ev == "run_start": return "● run start"
                if ev == "run_end":   return "■ run end"
                return ev or "?"
            steps = [e for e in evs if e.get("event") in STEP]
            events = [{"t": e.get("t", ""), "at": e.get("at") or e.get("ts", 0),
                       "text": label(e)} for e in steps[-3:]]
            # current agent: most recent node_enter with no later node_exit, fresh.
            current, stage, at = None, "", 0.0
            open_at = {}
            for e in evs:
                a = e.get("node")
                if a not in AGENTS: continue
                if e.get("event") == "node_enter":
                    open_at[a] = (e.get("ts", 0), e.get("task", ""))
                elif e.get("event") == "node_exit":
                    open_at.pop(a, None)
            if open_at:
                a = max(open_at, key=lambda k: open_at[k][0])
                ts, task = open_at[a]
                if (now - ts) <= STALE:
                    current, stage, at = a, task, ts
            try:
                d = json.loads(STATUS.read_text()) if STATUS.exists() else {}
            except Exception:
                d = {}
            if d.get("busy") and (now - d.get("at", 0)) <= STALE:
                stage = d.get("text") or stage
                at = at or d.get("at", 0)
            # alert: the most recent terminal failure, unless work resumed after it.
            alert = None
            for e in reversed(evs):
                ev = e.get("event")
                if ev in ("node_enter", "run_start"):
                    break
                if ev == "node_exit" and e.get("ok") is False:
                    reason = (e.get("reason") or "").lower()
                    if "timeout" in reason:
                        kind = "timeout"
                    elif any(w in reason for w in ("permission", "denied", "blocked", "not allowed")):
                        kind = "blocked"
                    else:
                        kind = "error"
                    alert = {"kind": kind, "agent": e.get("node", ""),
                             "reason": e.get("reason") or kind, "t": e.get("t", "")}
                    break
            self._s(200, json.dumps({
                "current": current, "stage": stage, "at": at, "now": now,
                "events": events, "alert": alert,
            }, ensure_ascii=False)); return
        if p == "/feedback":
            ts = q.get("ts", ""); v = q.get("v", "")
            if ts and v in ("up", "down"):
                now = time.time()
                (DATA / "feedback.jsonl").open("a").write(
                    json.dumps({"ts": ts, "v": v, "at": now,
                                "at_hh": time.strftime("%H:%M:%S")}) + "\n")
                jid = None; project = None; model = None
                try:
                    if REPLY.exists():
                        for ln in REPLY.read_text().splitlines()[-200:]:
                            try: r = json.loads(ln)
                            except Exception: continue
                            if r.get("ts") == ts:
                                model = r.get("model") or r.get("role"); break
                except Exception: pass
                try:
                    ap = DATA / "active_project"
                    project = ap.read_text().strip() if ap.exists() else ""
                    bp = DATA / "brain" / project / "main.jsonl"
                    if bp.exists():
                        for ln in bp.read_text().splitlines()[-300:]:
                            try: j = json.loads(ln)
                            except Exception: continue
                            if j.get("ts") == ts and (not model or j.get("model","claude") == model):
                                jid = j.get("id"); break
                except Exception: pass
                if jid:
                    rel = "thumbs_up" if v == "up" else "thumbs_down"
                    weight = 1.0 if v == "up" else -1.0
                    try:
                        (DATA / "relations.jsonl").open("a").write(json.dumps({
                            "ts": time.strftime("%H:%M:%S"), "at": now,
                            "from_kind": "user", "from_id": "you",
                            "to_kind": "judgment", "to_id": jid,
                            "rel": rel, "weight": weight,
                            "source": "user", "project": project,
                        }, ensure_ascii=False) + "\n")
                    except Exception: pass
            self._s(200, json.dumps({"ok": True})); return
        if p == "/hide":
            # Append-only tombstone: never delete a row, just mark ts hidden.
            # Reversible — a later {hidden:false} un-hides (0 Landauer cost).
            ts = q.get("ts", ""); v = q.get("v", "1")
            if ts:
                (DATA / "hidden.jsonl").open("a").write(json.dumps({
                    "ts": ts, "hidden": v != "0", "at": time.time(),
                    "at_hh": time.strftime("%H:%M:%S")}, ensure_ascii=False) + "\n")
            self._s(200, json.dumps({"ok": True})); return
        if p == "/ctx":
            ap = DATA / "active_project"
            name = ap.read_text().strip() if ap.exists() else ""
            cf = DATA / "ctx" / (name + ".md")
            self._s(200, json.dumps({"project": name,
                "text": cf.read_text() if cf.exists() else ""}, ensure_ascii=False)); return
        if p == "/api/shared_context":
            self._s(200, json.dumps({
                "text": SHARED_CONTEXT.read_text(errors="ignore") if SHARED_CONTEXT.exists() else ""
            }, ensure_ascii=False)); return
        if p == "/log":
            # Reconstruct chronological order across midnight boundaries.
            # Entries with `at` (epoch float) are sorted by it directly.
            # Entries without `at` keep their file-append position as a tiebreaker.
            all_entries = []
            for path, role in ((INBOX, "user"), (REPLY, "claude")):
                if not path.exists(): continue
                day_offset = 0.0
                prev_sec = -1.0
                for idx, ln in enumerate(path.read_text().splitlines()):
                    try:
                        o = json.loads(ln); o.setdefault("role", role)
                        at = o.get("at")
                        if at:
                            sort_key = float(at)
                        else:
                            ts = o.get("ts", "00:00:00")
                            try:
                                h, m, s = (int(x) for x in ts.split(":"))
                                sec = h * 3600 + m * 60 + s
                            except Exception:
                                sec = 0
                            if sec < prev_sec - 3600:   # midnight crossed
                                day_offset += 86400
                            prev_sec = sec
                            sort_key = day_offset + sec + idx * 0.001
                        all_entries.append((sort_key, idx, o))
                    except Exception: pass
            all_entries.sort(key=lambda x: x[0])
            hidden = set()
            hf = DATA / "hidden.jsonl"
            if hf.exists():
                for ln in hf.read_text().splitlines():
                    try:
                        h = json.loads(ln); t = h.get("ts")
                        if not t: continue
                        hidden.add(t) if h.get("hidden", True) else hidden.discard(t)
                    except Exception: pass
            items = [o for _, _, o in all_entries if o.get("ts") not in hidden]
            self._s(200, json.dumps(items[-60:], ensure_ascii=False)); return
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

        if p == "/api/graph":
            # LangGraph execution events — last N for visualization
            gf = DATA / "graph_events.jsonl"
            rows = []
            if gf.exists():
                for ln in gf.read_text().splitlines()[-1000:]:
                    try: rows.append(json.loads(ln))
                    except Exception: pass
            self._s(200, json.dumps(rows, ensure_ascii=False)); return

        if p == "/api/graph/clear":
            (DATA / "graph_events.jsonl").write_text("")
            self._s(200, json.dumps({"ok": True})); return

        if p == "/critique":
            cr = DATA / "critique.md"
            self._s(200, json.dumps({"text": cr.read_text() if cr.exists() else ""},
                                    ensure_ascii=False)); return
        if p == "/api/commands":
            cf = DATA / "commands.json"
            self._s(200, cf.read_text() if cf.exists() else "[]"); return
        if p == "/api/brain/list":
            name = q.get("p", "")
            # No project specified → list ALL projects that have any brain data
            if not name:
                projects = []
                bd_root = DATA / "brain"
                if bd_root.is_dir():
                    for sub in sorted(bd_root.iterdir()):
                        if sub.name not in PROJECTS: continue
                        if sub.is_dir() and any(sub.glob("*.jsonl")):
                            projects.append(sub.name)
                self._s(200, json.dumps(projects, ensure_ascii=False)); return
            # Project specified → list graph files inside it
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
                    # Prefer main.jsonl by name (stable); fall back to newest mtime
                    main = bd / "main.jsonl"
                    if main.is_file():
                        bf = main
                    else:
                        files = sorted(bd.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
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
        if p == "/api/relations":
            # Unified edge store: data/relations.jsonl
            # Filters: ?p=<project>&node=<id>&rel=<rel>&limit=200
            rf = DATA / "relations.jsonl"
            items = []
            if rf.is_file():
                for ln in rf.read_text().splitlines():
                    try: items.append(json.loads(ln))
                    except Exception: pass
            pf, nf, rf_ = q.get("p","").strip(), q.get("node","").strip(), q.get("rel","").strip()
            if pf: items = [e for e in items if e.get("project") == pf]
            if nf: items = [e for e in items if e.get("from_id") == nf or e.get("to_id") == nf]
            if rf_: items = [e for e in items if e.get("rel") == rf_]
            try: lim = max(1, min(2000, int(q.get("limit","500"))))
            except Exception: lim = 500
            self._s(200, json.dumps(items[-lim:], ensure_ascii=False)); return
        if p == "/api/distill":
            # On-the-fly markdown distillation via bin/distill.py
            # ?p=PROJECT (default: active project)
            import subprocess
            project = q.get("p", "").strip()
            if not project:
                ap = DATA / "active_project"
                project = ap.read_text().strip() if ap.exists() else ""
            if not project or project not in PROJECTS:
                self._s(400, json.dumps({"error":"unknown project"})); return
            try:
                r = subprocess.run(
                    ["python3", str(BASE / "bin" / "distill.py"), "--stdout", project],
                    capture_output=True, text=True, timeout=15)
                md = r.stdout or r.stderr or "(пусто)"
            except Exception as exc:
                self._s(500, json.dumps({"error": str(exc)})); return
            # plain markdown response — let the client render
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(md.encode("utf-8"))
            return
        if p == "/api/meta_facts":
            # Predictive / compressed facts produced by bin/meta_compact.py
            # Filters: ?p=<project>&kind=<kind>&limit=100
            mf = DATA / "meta.jsonl"
            items = []
            if mf.is_file():
                for ln in mf.read_text().splitlines():
                    try: items.append(json.loads(ln))
                    except Exception: pass
            pf, kf = q.get("p","").strip(), q.get("kind","").strip()
            if pf: items = [m for m in items if m.get("project") == pf]
            if kf: items = [m for m in items if m.get("kind") == kf]
            try: lim = max(1, min(500, int(q.get("limit","100"))))
            except Exception: lim = 100
            self._s(200, json.dumps(items[-lim:], ensure_ascii=False)); return
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

        # ── LangGraph Orchestrator API ──
        if HAS_ORCHESTRATION:
            if p == "/orchestration.html":
                self._s(200, ORCHESTRATION_HTML, "text/html; charset=utf-8"); return
            
            if p == "/api/orchestration/start":
                try:
                    result = handle_orchestration_start(q)
                    self._s(200, json.dumps(result, ensure_ascii=False)); return
                except Exception as e:
                    self._s(500, json.dumps({"error": str(e)})); return
            
            if p == "/api/orchestration/graph":
                try:
                    result = handle_orchestration_graph(q)
                    self._s(200, json.dumps(result, ensure_ascii=False)); return
                except Exception as e:
                    self._s(500, json.dumps({"error": str(e)})); return
            
            if p == "/api/orchestration/llm":
                try:
                    result = handle_orchestration_llm(q)
                    self._s(200, json.dumps(result, ensure_ascii=False)); return
                except Exception as e:
                    self._s(500, json.dumps({"error": str(e)})); return

        self._s(404, "no")

    def do_POST(self):
        if not self._guard():
            return
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path == "/hook":
            try:
                n = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(n) if n > 0 else b""
                ctype = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                text = q.get("text") or q.get("message") or ""
                imgs = []
                source = q.get("source", "hook")

                if raw:
                    if ctype == "application/json":
                        d = json.loads(raw.decode("utf-8"))
                        if isinstance(d, dict):
                            text = str(d.get("text") or d.get("message") or text or "")
                            img_val = d.get("imgs", d.get("img", []))
                            if isinstance(img_val, str):
                                imgs = [img_val]
                            elif isinstance(img_val, list):
                                imgs = [str(x) for x in img_val]
                            source = str(d.get("source") or source)
                    elif ctype == "application/x-www-form-urlencoded":
                        form = urllib.parse.parse_qs(raw.decode("utf-8"))
                        text = form.get("text", form.get("message", [text]))[0]
                        imgs = form.get("img", []) + form.get("imgs", [])
                        source = form.get("source", [source])[0]
                    elif not text:
                        text = raw.decode("utf-8", "replace")

                ok = receive_site_message(text, imgs, source)
                self._s(200, json.dumps({"ok": ok, "source": source}, ensure_ascii=False))
            except Exception as e:
                self._s(400, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            return
        if u.path == "/api/shared_context":
            try:
                n = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(n) if n > 0 else b""
                ctype = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                text = ""
                if ctype == "application/json" and raw:
                    d = json.loads(raw.decode("utf-8"))
                    text = str(d.get("text", ""))
                else:
                    text = raw.decode("utf-8", "replace")
                SHARED_CONTEXT.write_text(text[:20_000])
                self._s(200, json.dumps({"ok": True, "bytes": len(text.encode("utf-8"))}))
            except Exception as e:
                self._s(400, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            return
        if u.path == "/api/dispatch_mode":
            try:
                n = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(n) if n > 0 else b""
                mode = raw.decode("utf-8", "replace").strip()
                if raw.startswith(b"{"):
                    mode = json.loads(raw.decode("utf-8")).get("mode", "")
                if mode not in ("background", "terminal"):
                    self._s(400, json.dumps({"ok": False, "error": "bad mode"})); return
                DISPATCH_MODE.write_text(mode)
                self._s(200, json.dumps({"ok": True, "mode": mode}))
            except Exception as e:
                self._s(400, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            return
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

    def do_OPTIONS(self):
        if not self._guard():
            return
        self._s(204, b"", "text/plain; charset=utf-8")

    def log_message(self, *_): pass

class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

if __name__ == "__main__":
    print(f"iProject Manager on http://0.0.0.0:{PORT}/  (LAN: http://192.168.1.103:{PORT}/)", flush=True)
    S(("0.0.0.0", PORT), H).serve_forever()

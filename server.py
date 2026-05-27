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
import http.server, socketserver, urllib.parse, pathlib, json, time, subprocess, re, os, datetime

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
    "depz-toolkit":    pathlib.Path("/home/wera_n/GIT/depz-toolkit"),
    "istereolab-sdk":  pathlib.Path("/home/wera_n/GIT/istereolab-sdk"),
    "iproject_menger": BASE,   # self-managed: the manager is itself a project
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

        if p.startswith("/up/"):
            f = (DATA / "uploads" / p[4:]).resolve()
            if str(f).startswith(str((DATA / "uploads").resolve()) + os.sep) and f.is_file():
                self._s(200, f.read_bytes(), "image/" + (f.suffix[1:].lower() or "png")); return
            self._s(404, "no"); return

        # ── pair chat ──
        if p == "/say":
            t = q.get("text", "").strip()
            if t:
                with INBOX.open("a") as f:
                    f.write(json.dumps({"ts": time.strftime("%H:%M:%S"), "text": t}, ensure_ascii=False) + "\n")
                NEW.write_text(t)
                m = re.match(r"^\[([^\]]+)\]", t)   # [project] prefix → active project
                if m: (DATA / "active_project").write_text(m.group(1).strip())
                set_status(True, "получил сообщение, думаю…")
            self._s(200, json.dumps({"ok": bool(t)})); return
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
        if p == "/critique":
            cr = DATA / "critique.md"
            self._s(200, json.dumps({"text": cr.read_text() if cr.exists() else ""},
                                    ensure_ascii=False)); return
        if p == "/api/commands":
            cf = DATA / "commands.json"
            self._s(200, cf.read_text() if cf.exists() else "[]"); return
        if p == "/api/brain":
            name = q.get("p", "")
            nodes = []; edges = []
            bf = DATA / "brain" / (name + ".jsonl")
            if name in PROJECTS and bf.is_file():
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
                ctx = q.get("p", "")
                with INBOX.open("a") as f:
                    f.write(json.dumps({"ts": time.strftime("%H:%M:%S"),
                        "text": "📎 " + fname + ((" [" + ctx + "]") if ctx else ""),
                        "img": rel}, ensure_ascii=False) + "\n")
                NEW.write_text("[file] " + str(BASE / rel) + ((" [" + ctx + "]") if ctx else ""))
                set_status(True, "смотрю присланный файл…")
                self._s(200, json.dumps({"ok": True, "path": rel}))
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

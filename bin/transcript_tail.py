#!/usr/bin/env python3
"""
Tail Claude Code's current-session transcript and stream Claude's internal
state (thinking, text, tool_use) into data/graph_events.jsonl so the site
can render Claude's reasoning live, not just the final reply.

Adds project/file/line attribution to tool_use events so the UI can deep-link
into the on-site file tree (/p?name=...&file=...&line=...).

Usage:
  bin/transcript_tail.py            # daemon, tails the most recent session
  bin/transcript_tail.py --once     # one pass over new lines, then exit
"""
import json, time, sys, pathlib, re

SLUG    = "-home-wera-n-GIT-iproject-menger"
SESDIR  = pathlib.Path.home() / ".claude" / "projects" / SLUG
EVENTS  = pathlib.Path("/home/wera_n/GIT/iproject_menger/data/graph_events.jsonl")
STATE   = pathlib.Path("/home/wera_n/GIT/iproject_menger/data/transcript_tail.state")
LOG     = pathlib.Path("/home/wera_n/GIT/iproject_menger/data/transcript_tail.log")

PROJECTS = [
    ("depz-toolkit",        pathlib.Path("/home/wera_n/GIT/depz-toolkit")),
    ("istereolab-sdk",      pathlib.Path("/home/wera_n/GIT/istereolab-sdk")),
    ("ifirmware-stereocam", pathlib.Path("/home/wera_n/GIT/ifirmware-stereocam")),
    ("iproject_menger",     pathlib.Path("/home/wera_n/GIT/iproject_menger")),
]

ONCE = "--once" in sys.argv

def log(msg):
    try: LOG.open("a").write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except: pass

def latest_session():
    files = sorted(SESDIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None

def load_state():
    if not STATE.exists(): return {}
    try: return json.loads(STATE.read_text())
    except: return {}

def save_state(st):
    try: STATE.write_text(json.dumps(st))
    except: pass

def fmt_ts(iso):
    try:
        import datetime as dt
        return dt.datetime.fromisoformat(iso.replace("Z","+00:00")).astimezone().strftime("%H:%M:%S")
    except: return iso[11:19] if iso else "?"

def project_for(abspath):
    """Return (project_name, relpath) for an absolute file path, or (None, abspath)."""
    if not abspath: return None, ""
    try:
        p = pathlib.Path(abspath).resolve()
    except Exception:
        p = pathlib.Path(abspath)
    for name, root in PROJECTS:
        try:
            rel = p.relative_to(root)
            return name, str(rel)
        except ValueError:
            continue
    return None, str(p)

TOOL_ICON = {
    "Bash":"⚡","Read":"📖","Edit":"✏️","Write":"📝","Agent":"🤖",
    "Grep":"🔎","Glob":"🗂","WebFetch":"🌐","WebSearch":"🔍",
}

def parse_tool(tool, inp):
    """Return dict with at least 'what'; optionally 'project','file','line','snippet'."""
    out = {"what": ""}
    if not isinstance(inp, dict): return out
    if tool in ("Read","Edit","Write","NotebookEdit"):
        fp = inp.get("file_path","")
        proj, rel = project_for(fp)
        out["what"]    = rel if proj else pathlib.Path(fp).name
        out["project"] = proj
        out["file"]    = rel if proj else None
        if "line" in inp:        out["line"] = inp.get("line")
        if "line_number" in inp: out["line"] = inp.get("line_number")
        if "offset" in inp:      out["line"] = inp.get("offset")
    elif tool == "Bash":
        cmd_full = (inp.get("command") or "").replace("\n"," ")
        out["what"] = cmd_full[:100]
        # regex on the FULL command (not the truncated UI string) so paths near the cap aren't cut mid-word
        m = re.search(r"(/home/wera_n/GIT/[^\s'\"<>;|`)]+)", cmd_full)
        if m:
            proj, rel = project_for(m.group(1))
            if proj:
                out["project"] = proj
                out["file"]    = rel
    elif tool in ("Grep","Glob"):
        out["what"] = (inp.get("pattern") or inp.get("query","") or "")[:60]
        sub = inp.get("path") or inp.get("glob")
        if sub:
            proj, rel = project_for(sub)
            if proj:
                out["project"] = proj
                out["file"]    = rel
    elif tool in ("WebFetch","WebSearch"):
        out["what"] = (inp.get("url") or inp.get("query","") or "")[:80]
    elif tool == "Agent":
        out["what"] = (inp.get("description") or inp.get("prompt","") or "")[:70]
    else:
        out["what"] = str(inp)[:60]
    return out

def emit(ev):
    try: EVENTS.open("a").write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception as e: log(f"emit error: {e}")

def parse_line(line):
    try: d = json.loads(line)
    except: return []
    t = d.get("type")
    msg = d.get("message")
    iso = d.get("timestamp","")
    hh  = fmt_ts(iso)
    out = []
    if t == "assistant" and isinstance(msg, dict):
        for c in (msg.get("content") or []):
            if not isinstance(c, dict): continue
            k = c.get("type")
            if k == "thinking":
                txt = (c.get("thinking") or "").strip()
                if not txt: continue
                out.append({"ts":time.time(),"t":hh,"event":"think",
                            "text":txt[:240],"len":len(txt)})
            elif k == "text":
                txt = (c.get("text") or "").strip()
                if not txt: continue
                out.append({"ts":time.time(),"t":hh,"event":"say_assistant",
                            "text":txt[:240],"len":len(txt)})
            elif k == "tool_use":
                tool = c.get("name","?")
                parsed = parse_tool(tool, c.get("input",{}))
                ev = {"ts":time.time(),"t":hh,"event":"tool_use",
                      "tool":tool, "icon":TOOL_ICON.get(tool,"🔧")}
                ev.update({k:v for k,v in parsed.items() if v not in (None,"",)})
                out.append(ev)
    return out

def process_file(path, last_lineno):
    new_count = last_lineno
    try:
        with path.open() as f:
            for i, line in enumerate(f):
                if i < last_lineno: continue
                for ev in parse_line(line):
                    emit(ev)
                new_count = i + 1
    except FileNotFoundError:
        return last_lineno
    return new_count

def main():
    st = load_state()
    while True:
        sess = latest_session()
        if not sess:
            log("no session jsonl yet")
            if ONCE: return
            time.sleep(2); continue
        key = sess.name
        last = int(st.get(key, 0))
        new  = process_file(sess, last)
        if new != last:
            st[key] = new
            save_state(st)
            log(f"{key}: {last}→{new}")
        if ONCE: return
        time.sleep(1.5)

if __name__ == "__main__":
    main()

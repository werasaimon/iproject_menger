#!/usr/bin/env python3
"""bin/write implementation — reads stdin = new file content,
emits file_change event with unified diff to graph_events.jsonl."""
import sys, os, json, time, pathlib, hashlib, difflib

if len(sys.argv) < 2:
    print("usage: bin/write <path>  (content via stdin)"); sys.exit(2)
target = pathlib.Path(sys.argv[1]).resolve()
new_content = sys.stdin.read()

D = pathlib.Path("/home/wera_n/GIT/iproject_menger/data")
EVENTS = D / "graph_events.jsonl"
DIFFS  = D / "diffs"; DIFFS.mkdir(parents=True, exist_ok=True)

PROJECTS = [
    ("depz-toolkit",        pathlib.Path("/home/wera_n/GIT/depz-toolkit")),
    ("istereolab-sdk",      pathlib.Path("/home/wera_n/GIT/istereolab-sdk")),
    ("ifirmware-stereocam", pathlib.Path("/home/wera_n/GIT/ifirmware-stereocam")),
    ("iproject_menger",     pathlib.Path("/home/wera_n/GIT/iproject_menger")),
]
def project_for(p):
    for n, root in PROJECTS:
        try: return n, str(p.relative_to(root))
        except ValueError: continue
    return None, str(p)

before = ""
if target.exists():
    try: before = target.read_text()
    except Exception: before = "(binary)"

target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(new_content)

diff_lines = list(difflib.unified_diff(
    before.splitlines(keepends=True),
    new_content.splitlines(keepends=True),
    fromfile=str(target)+".before",
    tofile=str(target)+".after", n=3,
))
diff_text = "".join(diff_lines)
added   = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))

now = time.time(); hh = time.strftime("%H:%M:%S")
proj, rel = project_for(target)
diff_id = hashlib.sha1(f"{now}:{target}".encode()).hexdigest()[:12]

(DIFFS / f"{diff_id}.json").write_text(json.dumps({
    "id": diff_id, "ts": now, "t": hh, "path": str(target),
    "project": proj, "file": rel,
    "before_len": len(before), "after_len": len(new_content),
    "added": added, "removed": removed, "diff": diff_text,
}, ensure_ascii=False))

ev = {
    "ts": now, "t": hh, "event": "file_change",
    "id": diff_id, "path": str(target),
    "project": proj, "file": rel,
    "added": added, "removed": removed,
    "before_len": len(before), "after_len": len(new_content),
    "preview": "".join([l for l in diff_lines[:30]])[:1200],
}
EVENTS.open("a").write(json.dumps(ev, ensure_ascii=False) + "\n")
print(f"wrote {target}  ({len(new_content)}b, +{added} −{removed})  diff_id={diff_id}")

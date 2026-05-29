#!/usr/bin/env python3
"""Visible terminal worker for one agent.

The server can run in terminal dispatch mode, where it only writes jobs to
data/agent_queues/<agent>.jsonl. This worker is meant to be run in a visible
terminal pane. It reads its queue, runs the same CLI adapter as the background
dispatcher, and writes replies/brain/events back to the shared server memory.
"""
import importlib.util
import json
import pathlib
import sys
import time
import traceback

BASE = pathlib.Path("/home/wera_n/GIT/iproject_menger")
DATA = BASE / "data"
QUEUES = DATA / "agent_queues"
QUEUES.mkdir(parents=True, exist_ok=True)

spec = importlib.util.spec_from_file_location("lgd", BASE / "bin" / "langgraph_dispatch.py")
lgd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lgd)

def _queue_path(agent):
    return QUEUES / f"{agent}.jsonl"

def _state_path(agent):
    return QUEUES / f"{agent}.offset"

def _read_offset(agent):
    try:
        return int(_state_path(agent).read_text().strip() or "0")
    except Exception:
        return 0

def _write_offset(agent, offset):
    _state_path(agent).write_text(str(offset))

def _pending_jobs(agent, offset):
    q = _queue_path(agent)
    if not q.exists():
        return []
    lines = q.read_text(errors="ignore").splitlines()
    jobs = []
    for idx, line in enumerate(lines[offset:], start=offset + 1):
        if not line.strip():
            continue
        try:
            jobs.append((idx, json.loads(line)))
        except Exception:
            continue
    return jobs

def _run_job(agent, job):
    task = (job.get("task") or "").strip()
    source = job.get("source", "server")
    if not task:
        return
    print(f"\n[{time.strftime('%H:%M:%S')}] {agent} <= {source}: {task}", flush=True)
    lgd.emit("node_enter", node=agent, task=task[:80], source=source, mode="terminal")
    lgd.set_status(True, f"{agent}: {task[:50]}...")
    try:
        out = lgd._run_cli_agent(agent, task)
        print(f"\n[{time.strftime('%H:%M:%S')}] {agent} =>\n{out}\n", flush=True)
        lgd.write_reply(out[:3000], agent)
        lgd._append_brain(lgd._active_project(), agent, out[:3000])
        lgd.emit("node_exit", node=agent, ok=True, chars=len(out), mode="terminal")
        for target, delegated_task in lgd._delegations_from(out, agent):
            enqueue(target, delegated_task, f"{agent}:delegate")
    except Exception as exc:
        err = f"({agent} error: {exc})"
        print(err, flush=True)
        traceback.print_exc()
        lgd.write_reply(err, agent)
        lgd.emit("node_exit", node=agent, ok=False, reason=str(exc)[:120], mode="terminal")
    finally:
        lgd.set_status(False)

def enqueue(agent, task, source="server"):
    if agent not in lgd.AGENTS:
        raise SystemExit(f"unknown agent: {agent}")
    rec = {"ts": time.time(), "source": source, "task": task}
    with _queue_path(agent).open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: agent_worker.py <agent>")
    agent = sys.argv[1].strip().lower()
    if agent not in lgd.AGENTS:
        raise SystemExit(f"unknown agent: {agent}")
    print(f"{lgd.AGENTS[agent]['label']} worker ready; queue={_queue_path(agent)}", flush=True)
    offset = _read_offset(agent)
    while True:
        for idx, job in _pending_jobs(agent, offset):
            _run_job(agent, job)
            offset = idx
            _write_offset(agent, offset)
        time.sleep(1)

if __name__ == "__main__":
    main()

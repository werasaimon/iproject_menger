# iProject Manager

A small, standalone local tool for **researching git projects** and pair-working
with the Claude Code agent over a browser (desktop or phone on the LAN).

It is deliberately its **own** project: the server, HTML and all manager code live
here only. It reads the managed repositories **from the outside, read-only** — it
never injects any of its code into them, so the managed projects stay clean.

## What it does

- Registers local git repos as **projects** (see `PROJECTS` in `server.py`).
- Per project, a read-only research view:
  - commit history as a **state/delta timeline** (click a commit → `git show --stat`),
  - working-tree **status**,
  - a **file tree** browser.
- A **pair chat**: the browser sends text; the Claude Code agent watches
  `data/inbox_new`, replies into `data/replies.jsonl`, and can update the UI.

## Run

```bash
python3 server.py        # http://localhost:8078/  (LAN: http://<host>:8078/)
```

Open `http://localhost:8078/` on this machine, or `http://<lan-ip>:8078/` from a
phone/tablet on the same network.

## Layout

```
server.py        # HTTP server: project registry + read-only git API + chat
index.html       # project cards + chat
project.html     # one project's research view (timeline / status / files) + chat
data/            # runtime chat state (inbox/replies) — git-ignored
graphs/          # ad-hoc visual pages (private) — git-ignored
```

## Safety

Read-only by design: only whitelisted git subcommands (`log`/`status`/`show`),
projects must be in the registry, commit SHAs are hex-validated, and file paths
are confined to each project's root. Intended for localhost / trusted LAN only.

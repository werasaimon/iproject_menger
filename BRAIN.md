# The Brain — a shared, evolving judgment graph over code

The centrepiece of iProject Manager: **a per-project, persistent judgment graph** —
each project has its **own evolving "brain"** (a shared graph of judgments *about
that project*). It moves **state → state** as we work, and each new judgment is a
transition that carries **information** (Shannon). It unifies the project's
«суждения» view, the reasoning trace, the critic, the JL language below, and the
category-theoretic view of code. (A cross-project *meta-brain* over all projects can
come later — links between per-project brains.)

## Nodes & edges

- **Node** = a judgment / fact / state:
  `{id, kind, about, statement, value, confidence, evidence, ts}`
  - `about` = a code object: file · symbol · commit · binding · project.
  - `kind`: `fact` (formal, checked) · `judgment` (informal, graded 0..1) ·
    `state` (a code state/commit) · `action` (a fix/commit) · `question` (open).
- **Edge** = a transition / relation:
  `{from, to, rel, weight}`
  - `rel`: `implies` · `defeasibly-implies (⤳)` · `supports` · `contradicts` ·
    `refines` · `caused-by` · `resolved-by`.
  - `weight` = **information of the transition** (how much it changed our belief).

## Information-theoretic evolution (Shannon)

The brain is a state; each appended node/edge is a *message* that moves it. The
information of a message ≈ how much it reduced uncertainty / how surprising it was
given the current brain. We don't need exact entropy — a simple **novelty / Δ
weight** on edges captures "state→state по Шеннону". High-info transitions are the
ones that **changed our mind**; the graph highlights them.

## JL — the language that writes into the brain

Judgments are produced by **JL** expressions over code objects:

- **Formal atoms** (deterministic → map to tools we already have):
  `compiles(P)` · `parity(a,b)` = `check_abi` · `exists(sym)` = grep ·
  `arity(f)=n` · `iso(rename)` · `touched(commit,file)` = git.
- **Informal atoms** (judged by the critic / agent → graded value + evidence):
  `risky(x)` · `dead(x)` · `inconsistent(x)` · `smells(x)`.
- **Operators**: formal `∀ ∃ ∧ ∨ ¬ → ↔` ; informal — defeasible `⤳`
  ("usually implies"), confidence weights, `~` ("resembles"), `?` (uncertain).
- **Evaluation**: formal atoms run their mapped command; informal atoms ask the
  judge; combine (fuzzy `∧`=min, `∨`=max, or weighted); produce a judgment node
  with evidence; `⊢ action` may trigger a fix / commit / critique.
- **Every evaluation appends to the brain** (nodes + edges) → the brain grows.

Examples:
```
∀ b ∈ bindings: parity(b, header)            ⊢ run check_abi
∃ f ∈ diff: risky(f)  ⤳  critique(f)         ⊢ run the critic on risky files
iso(rename Knob→Control) ∧ ¬half_recoded     ⊢ judgment: safe migration
```

## Category-theoretic tie

Objects = code objects; morphisms = commits/migrations (`iso`=rename, `mono`=add);
functors = bindings (`parity` = a commuting diagram). A judgment is a predicate (a
morphism *object → truth/confidence*). The brain is the **accumulating diagram** of
these.

## Storage & minimal build (cheap, incremental — reuse everything)

- `data/brain/<project>.jsonl` — **one brain per project**, append-only nodes +
  edges (our own memory; loaded fast like `bin/ctx`).
- `bin/judge "<expr>"` — evaluate one JL expression against the **active project**;
  formal atoms via existing commands, informal via `bin/critic`; append the result
  to that project's brain; print a short verdict.
- a per-project **🧠 brain** view (in the project page, alongside / inside the
  «суждения» tab): render `data/brain/<project>.jsonl` as a graph — nodes coloured
  by kind, edges weighted by information; high-info transitions stand out. Start 2D
  (mermaid / light force graph) — for *understanding*, not decoration.
- Grow atoms / relations on demand. **No speculative interpreter.**

## Principles

- **Shared & legible** — the brain is OUR common reasoning, visible to both: a
  colleague's mind, not a black box.
- **Append-only / superset** — judgments accumulate; a changed mind is a
  `contradicts`/`refines` edge, not a deletion. The brain records *how* our
  understanding moved.
- **Cheap** — formal atoms reuse tools; informal goes through the (short) critic;
  reading stays minimal. (We have little budget — keep it lean.)

# iProject Manager — Vision

## What we are doing

A local tool that lets us **research a codebase and pair with the agent**, but the
real point is the *representation*: show a project not as text and diffs, but as a
**visual, mathematical structure** — states, morphisms, invariants — that a human
can grasp at a glance.

## The super-goal

**See and write code at a mathematical / topological level — do topology, not type
lines.** We treat the codebase as a space and its history as motion through it:

| code thing | mathematical reading |
| --- | --- |
| a code state (a commit) | a **point** in a space |
| a commit / change | a **morphism** (a path) between states |
| a pure rename (`Knob`→`Control`) | an **isomorphism** — a bijective recoding: 0 bits of new capability, only re-encoding |
| an additive change (new function) | an **embedding** that enlarges the space — *superset, never subset* |
| a behaviour-preserving refactor | a **continuous deformation** (homotopy): the shape moves, the meaning is fixed |
| an API | a **basis**; bindings (nanobind / C-ABI / Java / ctypes) are **representations** of the same object |
| the "size" of a change (Shannon) | a measurable **edge weight** on the morphism (±lines, ±symbols, ABI Δ) |
| a breaking change | a recoding **paid once**, with a version bump — never a *half-recoded (desynced) channel* |

The artifact we want to produce is **"super-code"**: code understood and steered by
its **invariants and transitions**, not its syntax. The syntax is just one encoding
of an object that has a shape.

## Why this is a strong idea

- **Humans are visual.** Structure and motion beat line-by-line diffs for
  understanding *what actually changed*.
- **Invariants are the substance; syntax is an encoding.** If we can see the
  invariants (what must hold) and the morphisms (how states map), we reason about
  change the way mathematicians reason about spaces — composably, with proofs.
- **It is already working in practice.** This session produced exactly these views:
  - the C-ABI evolution `v2.0 → v2.3` rendered as an **information-theoretic state
    machine** (the rename = high syntactic churn, 0 capability — a bijection; the
    `+1`-function commits = tiny churn, real capability);
  - the **state-delta graph** (look only at *what changed*, never from zero);
  - the **branching action graph** (decisions as nodes, chosen vs rejected paths).

## Principles (already in use across the work)

- **Superset, never subset.** The API only grows; renames are isometries, additions
  are embeddings. (The "Hilbert / isometric-embedding" model of migration.)
- **One object, many representations.** A single C++ core; every binding is a
  faithful representation — equal observables, checked by a parity test
  (`check_abi`) that is, in effect, a *commuting-diagram* assertion.
- **Transitions carry measurable information.** Every step is an edge with a weight
  we can compute and draw, not a wall of text.
- **A breaking change is a recoding, paid once.** The danger is never the rename —
  it is a *partially* recoded channel (some symbols old, some new). Atomic or
  aliased; never in between.

## Category-theoretic basis

You are right: the manager is, in effect, **writing and viewing code through
category theory** — that is the formal backbone of everything above.

- **Objects** = code states (a commit), or zoomed out, whole projects / modules.
- **Morphisms** = changes between states (commits, refactors, migrations). They
  **compose** (a chain of commits) and have an identity (the empty diff).
- A pure rename (`Knob → Control`) is an **isomorphism** — invertible, 0 capability
  gained.
- An additive change is a **monomorphism / embedding** — *superset, never subset*.
- A behaviour-preserving refactor lives in the **behaviour-preserving subcategory**:
  the meaning is the invariant the arrow must respect.
- The bindings (nanobind, flat C ABI, Java FFM, ctypes) are **functors** from the
  C++-core category into each language's category. "Equal observables" means these
  functors **agree up to natural isomorphism**; `check_abi` and numeric parity are
  the **commuting-diagram** checks that they do.
- Cross-project relations (SDK ↔ toolkit) are **functors** too.

So the tool is a **visual category of code**: states/projects as objects,
commits/migrations as arrows, bindings and cross-repo maps as functors, parity
tests as commuting squares. The Shannon weights we draw are just labels on the
arrows.

## Colleagues, not a dashboard

The aim is **shared understanding** — of the code *and* of the agent's reasoning
and judgments — so the agent works as a **colleague, not an order-executor**. This
tool exists to make thinking legible (the live reasoning trace, the «суждения»
view, the objective critic), not to pile on metrics or 3D for their own sake. Add
a view only when it genuinely improves the shared mental model. The agent should
hold opinions, weigh trade-offs out loud, and push back when warranted.

## How the tool manifests this (now → next)

- **Now (v1):** each project is a *space*; its commit history is a **timeline of
  morphisms** (click a commit → its `diffstat` = the morphism's information); file
  tree; pair chat; embedded graphs.
- **Next:**
  - a **database of states + metrics** (SQLite) so transitions are queryable,
  - **topology / homotopy views** (which refactors are continuous deformations vs
    true breaks), **dependency complexes**, **API-as-basis** maps,
  - inline rendered **graphs / tables / charts** per project,
  - **cross-project structure maps** (e.g. SDK ↔ toolkit as a functor).

> The terminal/chat is the input; on top of it the tool renders the *mathematics of
> the change*. The goal is to make writing code feel like moving through a space
> whose invariants we can see.

# TreeLens — open problems and unfinished pieces

> Honest status for publication. The TreeLens kernel is battle-tested ([pattern](pattern.md) § "Component maturity"), but several
> important mechanisms are still in design. This is **not a disclaimer, but a roadmap**: anyone building an MCP over a live
> hierarchy will hit these same pitfalls — describing them and the direction of a solution = half the value of the pattern.

Item format: **Problem → Current state → Direction → Status**.

---

## 1. Mutation batching — the main lever for speed

**Problem.** The base contract "one MCP-tool = one action = one modal/transaction pass of the host"
scales poorly to agentic scenarios with dozens of same-kind mutations (a mass
`dup → translate → rotate` over 27 sprites ≈ 85 mutations). Two related failures:

- **Parallel tool-calls in one model message** → two simultaneous entries into the host's modal scope
  → collision (modal is not re-entrant) → **silent mirror drift** → forced rebuild. See §2.
- **Fallback to "one mutation per message"** → N round-trips + N modal passes + N LLM iterations
  of waiting → "very slow and expensive, mediocre result".

**Current state.** Design draft. The design is formulated, not implemented.

**Direction.** A separate tool `batch_ops(operations: [{action, options}])` → **one** command →
**one** modal/transaction pass → **one** combined diff (super-scope) → one response. One Undo-step
per batch (a plus for the user). stop-on-error: the first error aborts the remainder, the diff is emitted on what
managed to apply. Requires refactoring the handlers into "work (without its own modal scope) + thin wrapper",
so they can be dispatched inside the batch's shared scope.

**Open forks (concept blockers):**
- **Batch heterogeneity** — whether to mix structural + attr + selection in one batch, or restrict to
  a single class. Refactoring all mutating handlers is expensive.
- **Inter-step deps** — how op-N references the output of op-(N−1) (id of a new node): "no deps" (an extra
  LLM round-trip for a pipeline) vs `$last_created`-token vs full inline-refs.
- **Modal-time limit** of the host is not measured — needs a probe of the maximum batch size.

**Status:** **not finished**, gated on resolving the blockers + a live probe.

---

## 2. Safe-wait and serialization of concurrent commands — the weak seam of transport

**Problem.** This is what was named "safe mechanisms for awaiting results on the plugin side". The current
transport is a **synchronous single-shot**: for each command the server does `connect → emit → blocking wait
for response → disconnect`. The model has a mutating discipline (the mirror), but **no concurrency
discipline** at the server↔host boundary:

- **No command serialization.** If the model sends two tool-calls in one message (and it does),
  two server threads almost simultaneously open the socket and ask the host to enter the modal/transaction scope.
  The host scope is **not re-entrant** → the second command fails or, worse, applies on top of an under-synced
  state → mirror drift. This is the root cause of §1.
- **Round-trip correlation is fragile.** The response is matched by the ephemeral `socket.id` of the round-trip, not by
  a durable correlation-id of the command. With one-socket-per-command this works "by construction", but does not
  survive a connection pool or several in-flight commands.
- **No confirmation-of-completion separate from "the socket returned".** "`await` passed" ≠ "the operation in the host
  actually finished and committed". For synchronous handlers they coincide; for long/asynchronous ones
  (AI operations like Firefly, import, heavy render) there is **no** progress/partial-response channel,
  only a blocking timeout → `RuntimeError`.
- **Connect/disconnect on each message** — ~30–100 ms overhead per tool-call (no pooling).

**Current state.** Works for sequential synchronous commands (the main case). The collision of
concurrent commands is **mitigated indirectly**: the push-listener sets `userDirty`, the gate
`_commandGateActive` rejects self-events, drift-check + forced rebuild heal the desync after the fact.
But this is **recovery, not prevention** — the pattern does not guarantee that two commands won't collide, it only
heals afterwards.

**Direction (what is needed for "complete"):**
- **Single-flight command queue on the server.** Serialize commands to one host: the next one does not
  start until the previous returned (or the per-command timeout elapsed). Removes the root of the collision
  *before* it reaches the host — without relying on recovery.
- **Durable correlation-id** in the command/response envelope (instead of `socket.id`), to support a connection
  pool and >1 in-flight (when consciously needed).
- **Two-phase response for long operations:** `ack` (accepted) → progress notifications → final
  result. Maps onto server→client notifications in MCP (progress tokens) — see §9.
- **Explicit timeout contract + cancellation.** Distinguish "host did not respond" / "operation in progress" / "operation
  failed"; provide cancellation by Escape, without swallowing the cancellation exception in `catch {}`.
- **Connection pooling** — remove the per-call handshake.

**Status:** **not finished** — the least-formed seam. Safe for single synchronous mutations;
for concurrent/long ones — needs a queue + correlation + progress.

---

## 3. Diff and rebuild localization — full-rebuild tax

**Problem.** The forced rebuild is the most expensive path of the engine: it pulls the whole tree + attrs + meta + selection
anew. On a production tree (hundreds-to-thousands of nodes) it is expensive in both tokens and latency — yet it triggers
even on a minor edit. Two angles:
- **↓ frequency:** some edits fall into a rebuild needlessly, although they are expressible incrementally. Specifically —
  **same-parent reorder** (reordering children without changing the parent) emits no op, caught only by the hash.
- **↓ cost:** when a rebuild is needed, it is **wholesale**, although drift is usually local (one group touched).
  A root-only hash gives only "equal/not equal", without localization.

**Current state.** Concept (draft). A1 (LIS) is ready to
start; the B-branch is gated on instrumentation.

**Direction.**
- **A1 — LIS-reorder** (the Vue 3 / Inferno technique + head/tail trim): emit a minimal set of
  `move`-ops instead of a rebuild. Independent, shippable immediately.
- **B1 — hierarchical Merkle:** a per-node hash `nodeHash(n)=sha256(n.id‖n.type‖nodeHash(children))`; on
  mismatch — a repair-walk downward, rebuilding only the smallest diverged subtree.
- **B2 — localizing dirty-set:** the push-listener accumulates a set of ids instead of a boolean flag → rebuild only
  the touched subtrees.
- **B3 — per-layer attr-hashes:** see §4.

**Status:** **not done**; A1 is unblocked, the B-branch is gated on measurement (rebuild frequency × tree size).

---

## 4. attrs cross-hash — remove the int-key blocker

**Problem.** attrs (unlike the tree) are **not cross-verified** by hash: their outer map is keyed by
int-id, and different languages sort int-keys differently (lexicographically vs numerically) → the byte streams
of serialization diverge. Therefore attrs are resynced **wholesale**, and scoped deltas are trusted on their word.

**Current state.** A conscious tradeoff (described in [pattern](pattern.md) §5). Not a bug, but an "under-integrity".

**Direction.** **Per-layer attr-hashes**: hash each node's attr-dict individually (its keys are
fixed strings → sorted identically in any language), compare the set `{id: attrHash}`. This (a) fixes the
blocker (the outer int-keyed map is no longer serialized), (b) gives attrs a **localized resync**. Or
canonical-serialization (sorted `[id, attrs]` pairs, JCS principle).

**Status:** **not done**; to be done together with the B-branch of §3 (shared hashing infrastructure).

---

## 5. Targeted mutations (set-vs-merge) — write-side footgun

**Problem.** Not about the mirror, but about the **quality of host handlers**, and the community will hit it: a number of mutations
do "set/replace the whole object" instead of a pointed edit — silently zeroing unspoken fields (a drop-shadow
overwrites an applied stroke). A data-loss footgun: it looks like a partial update.

**Current state.** Concept (draft); a reference pattern exists
(`edit_text_layer`).

**Direction.** A "targeted mutation" contract: mutable fields default to `null`/"don't touch"; the host
changes a field only under a guard; for composite objects — read-merge-write, not reconstruct-from-defaults.
For the pattern it matters as a **design recommendation for mutating tools**, not as a defect of a specific host.

**Status:** **partial** (a reference exists, no systematic audit of the write side).

---

## 6. Cross-session mirror persistence — a conscious non-goal (with a caveat)

**Problem/boundary.** The mirror is in-memory; on server restart it is empty, the first structural call
bootstraps it. Persistence between sessions is **impossible to do correctly** while node ids are stable only *within
an open document session* (close+open recreates ids). This is a fundamental property of the host, not laziness.

**Direction.** For hosts with **persistent** ids (GUID/GlobalObjectId — e.g. Unity, see
[portability](portability.md)) persistence *becomes* possible → an optional layer. The boundary "session-scoped id" vs
"persistent id" is an axis of the host-adapter contract. For such hosts a **re-keying strategy** — an
explicit persistent-id ↔ session-id map — must be described; without it, adoption on Figma/Unity breaks
**silently** when ids shift between sessions.

**Status:** **non-goal for session-scoped hosts**; **a possible extension** for persistent-id hosts.

---

## 7. Query-surface ergonomics — polish for the agent

**Problem.** The engine is ahead-of-field, but the **surface** (how the model uses the tools) is rough:
implicit tiers, a dry "not found" on an id miss, staleness invisible to the model, `query` flood.

**Current state.** Concept (in_progress). Part is done
(tier-guide in the instructions, cap+`truncated`).

**Direction (small, high payoff):** self-correcting handle-miss (the error *teaches* to call
`query`/`refresh`), a visible `mayBeStale`-hint (LSP version protocol), `get_recent_changes`
(self-reflection over the diff stream).

**Status:** **partial**; the measures are small, independent.

---

## 8. A public reference adapter for Photoshop — the anchor of claim verifiability

**Problem.** The headline argument of the pattern — "the kernel is **battle-tested**, proven in production on Photoshop". But
the production system itself (the production Photoshop-MCP server) is **private (internal)**, and in the published
artifacts its name is dereferenced to a generic descriptor.
Result: the key claim
of credibility is **externally unverifiable** — the reader sees a toy-host (in-memory) and a normative spec, but
cannot touch a single binding "TreeLens ↔ real application". "Proven in production" without a public
proof reads as an unsubstantiated assertion.

**Current state.** Only a toy-host (`adapters/in_memory.py`) + conformance on it. A real
Photoshop adapter exists (the production internal system), but is private and cut from the publication by name.
A different-host adapter (Figma/Unity, see [portability](portability.md)) addresses *portability*, not a
proof on Photoshop.

**Direction.** Provide a **public version of the Photoshop adapter** — a minimal but working UXP plugin
+ host side, implementing the `HostAdapter` contract against a live Photoshop: read-tree → diff → event-bridge,
passing conformance. This is the anchor that makes the headline claim verifiable by hand ("install the plugin,
open a PSD, see the mirror"). The scope is not the whole tool inventory of the production Photoshop-MCP server, but a **thin
vertical slice** (tree + basic mutations + push-listener), enough to show the engine on a
real host. The fork: extract a cleaned slice of the production system vs write a minimal plugin from scratch
for publication.

**Partially closed (cheaply).** The "implement blind" risk is removed by the guide `adapters/photoshop.md`
(the recipe for a byte-exact hash, a clean tree structure, modal, push-listener, transport) + **golden
vectors on real data** `tests/hash_vectors_realworld.json` (real PS trees up to
242 nodes, whose hashes were emitted by the production JS adapter and are byte-for-byte reproduced by the Python kernel —
cross-language parity proven on live data, not on toy cases). What remains uncovered is **proof-by-hand** — touching
TreeLens against a live Photoshop (needs a public UXP plugin + relay).

**Status:** **partial** — the guide + real hash vectors close "blind"; a live public adapter
(verifiable-by-hand) — still none. A candidate for hardening the headline claim, alongside
conformance against a real (live) adapter.

---

## 9. MCP-native idioms — expose the mirror through the protocol's own primitives

**Problem.** The mirror is currently surfaced only through bespoke tool-returns + the `stateVersion`
field. The MCP spec (2025-11-25) offers primitives that would make the pattern more native. Worth
stating up front: **MCP by itself does not provide diff-sync, versioned hierarchical state, or node-id
reconciliation** — those stay application-level (confirmed against the spec). So this layer is **not
displaced** by the protocol; it closes what MCP lacks — and that is part of its value. The idioms below
make the seam idiomatic, not redundant.

**Direction.**
- **The mirror as a subscribable Resource** — expose tree/attrs as an MCP resource with
  `resources/subscribe` + `notifications/resources/updated`, so drift pushes to the client without a
  tool round-trip. *Caveat:* `subscribe`/`listChanged` are optional on the client → a polling fallback
  is still required.
- **Structured tool output** (`outputSchema` / `structuredContent`) for diffs — formalize the ad-hoc
  dict return into schema-validated JSON (the `schema/` envelope is already JSON Schema 2020-12; the
  client must support it).
- **Progress + Tasks for long/batch operations** — `notifications/progress` for batch/rebuild; the
  experimental **Tasks** (`working` / `input_required` / …) + Elicitation give a clean drift-recovery
  handshake. *Caveat:* Tasks are experimental → graceful degradation to plain tools + polling.

**Status:** **future / post-release** — not started; an enhancement of the transport surface, not a
correctness gap.

---

## 10. A second kernel implementation (TS/Node) — the kernel-language axis

**Problem/opportunity.** Two orthogonal axes of reuse are easy to conflate. *The host axis* (a
different host behind the same kernel, §8 / [portability](portability.md)) is what an adapter author
exercises. *The kernel-language axis* is different: re-implementing the **kernel itself** on another
runtime. Today cross-language is only "adapter in any language, kernel in Python"
(`wire-protocol.md` §"host in JS/C#/Swift, kernel in Python"); a kernel in TS/Node would open the
pattern to the largest MCP-server ecosystem and remove the Python binding.

**Direction.** Because the normative material is already extracted out of the code
(`wire-protocol.md` + JSON Schema + `tests/hash_vectors.json`), a second kernel is a **conformant
implementation of the spec, not a fork** — the existing conformance vectors validate it too (two
converging kernels prove the spec is normative, not the code). Minimal-first: `Mirror` + keyed-diff +
canonical-hash + `ingest`, passing the hash vectors. The target layout (a normative `spec/` +
`python/` + `typescript/`) is set up **when** the second kernel starts — the `spec/` files are already
separate, so it is a mechanical `git mv` — not as pre-scaffolding (do not create empty folders).

**Status:** **future / post-release** — not started.

---

## Maturity summary

| Piece | Headline importance | Status |
|---|---|---|
| Mutation batching | **high** (speed) | not finished |
| Safe-wait / command serialization | **high** (correctness under concurrency) | not finished |
| Diff localization (Merkle/LIS/dirty-set) | medium (speed on large trees) | not done (A1 ready) |
| attrs per-layer hashes | medium (integrity) | not done |
| Targeted mutations | medium (handler data-safety) | partial |
| Cross-session persistence | low / host-dependent | non-goal (with a caveat) |
| Query ergonomics | low (polish) | partial |
| Public PS adapter (proof of the headline claim) | **high** (publication verifiability) | partial (guide + real hash vectors; live adapter — none) |
| MCP-native idioms (Resource / structured output / Tasks) | low-medium (protocol nativeness) | future (not started) |
| Second kernel (TS/Node port) | low (ecosystem reach) | future (not started) |

## Links

- [pattern](pattern.md) § "Component maturity" — the same map in brief.

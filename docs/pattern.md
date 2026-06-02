# TreeLens — pattern: a server-side mirror of the hierarchy for the agent

> Reference design of the pattern. Describes **how it works**, grounded in a proven implementation in
> a production Photoshop MCP server. Part of the TreeLens documentation — for the map and pitch see [README](../README.md).
> What is still **not finished** (batching, safe-wait, diff localization) — [open-problems](open-problems.md);
> what is **portable** to other hosts — [portability](portability.md).

## Why

An LLM agent drives an application that has a **large, live, mutable, hierarchical** structure: the
Photoshop layer tree, the Figma scene-graph, the Unity GameObject hierarchy. The naive bridge
"MCP tool ↔ host API" forces the agent to pay the **full token cost of the structure on every turn**:
either a mutation returns the full tree snapshot (tens of KB for 200–300 nodes), or the agent itself
pulls the tree anew before every decision. Over a long agent session this:

- **bloats context** — full trees accumulate in the conversation history, crowding out what's useful;
- **is latency-expensive** — a round-trip to the host on every read;
- **is fragile** — the agent reasons over a stale tree from the history without knowing it's outdated.

TreeLens solves this with a **server-side mirror** of the structure, synced by **deltas** rather than
re-snapshotting it wholesale.

And the protocol does not solve it for you: **MCP by itself provides neither diff-sync, nor versioned
hierarchical state, nor node-id reconciliation** — those stay application-level. So this is not a layer
the protocol displaces; it is exactly the gap MCP leaves open.

## The idea in one paragraph

The MCP server holds an **in-memory copy** of the hierarchy (the mirror). The host is the **single
writer** and the source of truth; the server is a **read-replica**. Each mutation returns not the full
state but a **diff** (what was added/removed/moved/changed-attribute); the server applies the diff to
the mirror and **strips the heavy payload** from the model's response. The model **navigates the mirror
with query tools** (search by name, subtree, path, node attributes) — without a round-trip to the host.
Integrity is held by a **tree hash**, cross-checked between host and server; on desync → forced rebuild.
Manual user edits (outside the agent) are caught by the host's **push listener**, which marks the mirror
stale.

## Required precondition: a single active writer

The pattern delivers the expected result **only under a serializable timeline of mutations — a single
active writer at any moment.** This is not a "nice-to-have" but a **precondition of applicability**:
outside it, the mirror does not deliver its value. The spectrum:

- **pure solo** (only the agent mutates) — the ideal case;
- **solo + rare manual edits** (typical Photoshop: the user occasionally touches the PSD) — **ok**: the
  push listener marks the mirror dirty, the agent's next turn absorbs the edit via a full rebuild.
  External edits are **rare and serialized** between the agent's turns;
- **continuous concurrent multi-actor mutation** (realtime multiplayer, e.g. collaborative Figma) —
  **out of scope**: the mirror is in permanent drift, rebuild thrashing, and the pattern **has no merge
  story**.

Hence too — why we disqualify CRDT/OT (Yjs/Automerge): they solve *multi-writer convergence*, a problem
that we **take out of scope with this precondition**, not one that "doesn't exist." For a realtime
collaborative host the problem is real — and **TreeLens gives no answer to it**.

> **Honest about multiplayer.** There is **currently no** architectural understanding of how to support
> concurrent collaborative mutation; work in that direction **has not been done**. This is **out of the
> current scope** — we do not exclude future attempts, but presenting it as a ready path would be
> dishonest: today a mirror over a realtime multiplayer host is an **unsolved research problem**, not a
> supported scenario. A related (but **different**) axis — concurrency of *commands from one agent* —
> [open-problems](open-problems.md) §2.

## Topology

TreeLens is **three layers** and two boundaries. The proxy relay is needed because the host's plugin
sandbox can usually only be a socket **client**, not a server (so it is with Photoshop UXP; similarly
for many host extensions).

```
  ┌─────────────┐  stdio (MCP)   ┌──────────────────┐  ws/socket.io   ┌──────────┐  ws   ┌───────────────┐ host API ┌──────────┐
  │   Agent     │───────────────▶│   MCP server     │────────────────▶│  Relay   │──────▶│ Plugin (host) │─────────▶│   Host   │
  │ (LLM client)│◀───────────────│  + MIRROR        │◀────────────────│ (Node)   │◀──────│   adapter     │◀─────────│ (PS/...) │
  └─────────────┘  tool calls    └──────────────────┘   command/      └──────────┘       └───────────────┘          └──────────┘
                   and responses   Mirror (kernel)        response+delta
```

- **Agent ↔ MCP server** — standard MCP (stdio). This is where **all the token economics** live: the
  agent sees only deltas and query results, not raw trees.
- **MCP server** — holds the mirror (`Mirror`), applies deltas, computes/cross-checks the hash, recovers
  from drift. This is the **host-agnostic kernel** (see [portability](portability.md)).
- **Relay** — a thin Node bridge (≈140 lines): one socket server, routing by `application`.
- **Plugin/adapter** — inside the host; the **authoritative source**: reads the real structure, mutates
  it, computes the "before vs after" diff and the hash, sends push notifications about manual edits. This
  is the **host-specific adapter** (see [portability](portability.md)).

## Vocabulary

The pattern is an **assembly of three textbook techniques**, each from its own field (full prior-art
analysis lives in the research notes). Naming it precisely matters: it gives a vocabulary for the docs
and cuts off whole classes of unneeded solutions.

- **Single-writer / primary-replica (leader-follower) replication.** The host is the *primary*
  (authoritative); the server is a *read replica / cache*. Writes go **from one place in one direction**.
  The classic hard problem of replicas — *concurrent conflicting writes* — is **taken out of scope by the
  single-active-writer precondition** (see §"Required precondition"), not "doesn't exist universally."
  This is why CRDT/OT (Yjs/Automerge) are disqualified: they solve multi-writer convergence — a problem
  we don't take on — at a constant metadata cost. TreeLens is in the same class as a DB read-replica, a
  virtual-DOM reconciler, an LSP server's document-mirror.
- **Operation-based propagation + state-based fallback.** On the happy path we send *diffs* (ops
  add/remove/move/typeChange); on desync — a full rebuild.
- **Keyed tree reconciliation.** Diff of two trees by **stable node identity** (id), not by position.
  The same family as virtual-DOM reconcilers (React/Vue), but children are treated as an unordered
  **keyed set** (we diff membership+parentage, ordering separately). A stable id collapses the expensive
  node *matching* stage (the very thing tree-edit-distance / GumTree exists for); hence we don't need
  off-the-shelf TED.
- **Integrity = root-hash (= Merkle of depth 0).** `treeHash` = sha256 over the **canonical
  serialization** of the tree — exactly the root of a Merkle tree. We compare roots; on mismatch we only
  know "something diverged somewhere" → repair degrades into a **full rebuild** (see
  [open-problems](open-problems.md) on localization).
- **Token-economics strategies (S1–S6).** The field "agent over large mutable state" reduces to six
  orthogonal strategies; TreeLens relies on four as its kernel — **S1** server-side mirror, **S2**
  handle/ref addressing (a stable id instead of re-describing the node), **S3** slice/query (fetch
  *parts*, not a dump), **S5** **diff-as-response** (mutations return only what changed) — plus partially
  **S4** tiered disclosure (S6 — context offloading to external memory — is out of scope). **S5 over a
  large long-lived tree is the rarest and sharpest differentiator**: Blender/Unity MCP round-trip; LSP
  syncs flat text (range-based, not a tree by id); Playwright MCP, even though it sends incremental
  snapshots, holds **snapshot-scoped refs** — there are no durable session-stable handles or a mirror
  *between* snapshots. Among those surveyed we did not encounter anyone who returns structural patches by
  stable id over a large mutable tree, protected by an integrity hash.

## Components

### 1. The mirror (Mirror) — split by the nature of the data

The per-document mirror is **four sub-stores**, split by how they change:

| Sub-store | Shape | Sync granularity |
|---|---|---|
| **tree** | `{id, type, children}` — *pure structure* | incremental ops (add/remove/move/typeChange) |
| **attrs** | `{node_id: {fields...}}` per-node | scoped deltas (attrSet/attrDelete) + wholesale rebuild |
| **meta** | flat document-header dict | wholesale replace (small, nothing to diff) |
| **selection** | `{active, bounds}` | wholesale replace |

**Key decision: the tree is pure structure.** Name/visibility/opacity/mode/bounds/effects live in
**attrs**, not in the tree node. Why: these fields change *often* and *independently* of the structure;
keep them in the tree and any name edit would jolt the tree-diff. By splitting, we give "rename" the
status of an **attr-mutation**, not a structural operation. The tree node stays `{id, type, children}` —
diffs cheaply and cross-language-deterministically (no int-keyed maps → no key-ordering problem).

The mirror is **in-memory only**. Node ids are stable **within an open document session** — the whole
correctness of the diff rests on this (see §4). Between server restarts the store is empty; the first
structural call **bootstraps** the mirror via a full fetch from the adapter.

> Grounding: the server-side mirror of the production Photoshop MCP server (module-scope dicts per
> sub-store — tree/attrs/meta/selection + an id index + a version counter).

### 2. Action classification — the diff shape picks the code, not the model

Every mutating tool **must pick a class**; the class determines which delta the adapter emits and how the
server applies it. This decision is made by the **code**, not the model.

| Class | What it changes | Emits |
|---|---|---|
| **structural** | the tree structure | `treeChanges` (ops) + `treeHash` + structural-seed `attrChanges` for new nodes |
| **attr-mutation** | node attributes | `attrChanges` (scoped attrSet/attrDelete), **no** treeChanges |
| **rebuild** | everything (new doc / flatten / active switch / crop) | `treeChanges:[{op:"rebuild"}]` + full bootstrap of attrs+meta+selection |
| **doc-meta** | only the header (save) | `metaChanges` |
| **selection** | the selection marquee | `selectionChanges` |
| **read-only / lifecycle / image** | nothing in the mirror | **only** `response` + `status` |

Read-only tools (`get_*`, query) and product ones (export, render bytes) **do not push the tree** — the
model reads the structure from the mirror. This is precisely the rejection of the auto-context push model
in favor of pull via query.

> Grounding: the action-class sets (`STRUCTURAL` / `ATTR_MUTATION` / `REBUILD` / `DOC_META` /
> `SELECTION`) + the attr-scope table in the adapter of the production Photoshop MCP server.

### 3. Diff-as-response — a mutation returns a delta, not state

The host-side adapter:
1. **before** the handler, captures pre-state in the declared scope (for structural — the whole tree; for
   attr-mutation — `{layerIds, fields}` of the affected nodes);
2. performs the mutation;
3. **after**, captures post-state and computes the **diff**;
4. returns the delta + `treeHash` (for structural/rebuild) + `scopeId` (the scope identifier —
   document/scene/file).

The server applies the delta to the mirror, stamps `stateVersion`, and **strips the heavy payload**
(`tree`, bootstrap `attrs`) from the model's response — the mirror has already absorbed it.

**The tree-op vocabulary** (symmetric: the host computes, the server applies):

```
{op:"add",        id, type, parentId, index, children?}     // new node (group = add with type=GROUP + children)
{op:"remove",     id}                                        // node removed
{op:"move",       id, toParent, newIndex}                    // changed parent/position
{op:"typeChange", id, from, to}                              // e.g. rasterization SO→PIXEL
{op:"rebuild",    tree}                                       // degenerate: full replace
```

**The attr-op vocabulary:** `{op:"attrSet", id, key, value}` / `{op:"attrDelete", id, key}` /
`{op:"attrsRebuild", attributes}`.

The adapter's response envelope:
```
{ status: "SUCCESS"|"FAILURE", response: <handler payload>, scopeId,
  treeChanges?, treeHash?, attrChanges?, metaChanges?, selectionChanges?, message? }
```

> Grounding: the adapter's response wrapper (capture before/after, compute tree-/attr-diff) ↔ the
> server's sync kernel (apply + strip + version annotation).

### 4. Keyed reconciliation — matching strictly by stable id

The diff aligns children **by stable `id`**, never by position. This is not a detail but a **correctness
invariant**: `[A,B,C] → [B,C,D]` under a keyed diff yields `remove(A) + add(D)` (B,C preserved); under a
positional one — three replacements (data corruption + excess payload). The stable id is the pattern's
**high-value asset**; all synchronization rests on it. Break the invariant (the host reuses an id) — and
consumers silently corrupt.

The `move` op **moves the node by reference**, does not copy — `_id_index` stays valid. A known gap of
the current implementation: **reorder within a single parent** (without a parent change) emits no op —
caught by the hash → forced rebuild (cured by the LIS technique, see [open-problems](open-problems.md)).

> Grounding: compute on the adapter side ↔ apply in the kernel, matching by the node's stable id;
> the contract — `.claude/rules/uxp-handlers.md`.

### 5. Integrity + drift recovery

The adapter sends `treeHash` — sha256 over the **canonical serialization** of the tree. After applying
the delta, the server cross-checks it against the mirror's local hash. Mismatch → **forced rebuild**: the
server pulls the fresh tree via an internal structural read and **reseeds attrs+meta+selection** (any tree
rebuild reseeds all sub-stores).

**The hard part — cross-language determinism of serialization.** The hash is cross-checked between two
implementations (host language ↔ server language), so the byte stream of the canonical serialization must
match. This works for the tree because the node is `{id, type, children}` with fixed string keys and an
ordered list of children (no language-native maps). **attrs are NOT cross-checked**: their outer map is
keyed by int-id, and JS and Python sort int keys differently (lexicographically vs numerically) → the
hashes diverge. So attrs are resynced **wholesale** on rebuilds, and scoped deltas are **trusted** (drift
is caught on rebuild events / by the push listener). This is an honest tradeoff, not a bug (the fix —
per-layer hashes, [open-problems](open-problems.md)).

Applying the delta is **atomic**: on an error in any op — roll back the mirror to pre-state and re-raise.

> Grounding: forced rebuild (tree/attrs/meta/selection) in the server's sync kernel; atomic apply-diff
> with rollback in the mirror.

### 6. Push listener — catching edits outside the agent

The hardest-to-detect drift: **the user edits the document by hand** between the agent's turns (moves a
layer in the UI). The key empirical observation: **the host does NOT send notifications for the plugin's
own operations** (inside its modal/transaction scope), only for *real* user edits. So any received
mutating notification = "the user touched the document" → the mirror is stale.

The adapter attaches a listener to the host's mutating events (`move/set/make/delete/...`;
navigation/select excluded — activating a node does not change the mirror). The event sets a `userDirty`
flag. The next **non-internal** command **eats the flag and substitutes** its incremental diff with a
full rebuild of the current host (which already reflects both the user's edit and the command's effect).
The backup gate `_commandGateActive` ignores events during its own command.

> Grounding: drift-listener registration + the set of mutating events + the `userDirty` branch in the
> adapter of the production Photoshop MCP server; history is in the archived drift-listener concept.

### 7. Query layer + tiers + staleness

The model navigates the mirror, **does not** dump it. Tools read from the store, without a round-trip;
live-fallback only on a miss:

- `query(pattern, type_filter, limit)` — regex search (+ a `limit` cap and a `truncated` flag against
  flooding on large docs);
- `subtree(id, depth)` — a subtree N levels deep (default shallow);
- `path(id)` — the path from the root;
- `get_attrs(id, fields)` — node attributes (name/visibility/bounds/effects/mask by the requested
  fields);
- `state_info` — version/hash/sizes of the mirror; `refresh(scope)` — an explicit forced rebuild.

**Tiers (S4, progressive disclosure):** "cheap → expensive" — *structure* (query/subtree) → *basic
props* (get_attrs) → *heavy attrs (effects/mask)* (get_attrs with those fields) → *pixels* (render
bytes). The cheapest sufficient tier is suggested to the model in the instructions.

**Staleness signal:** a read-from-mirror response carries `source: "mirror"|"live"` + `stateVersion` (on
a snapshot hit) — the model correlates responses. On a live fallback the version is omitted (otherwise it
would lie "stale" on fresh data).

> Grounding: the query tools of the production Photoshop MCP server (the origin names are layer-centric:
> `query_layers` / `get_layer_subtree` / `get_layer_path` / `get_layer_info`+`get_layer_bounds` /
> `get_snapshot_attrs` / `snapshot_info` / `refresh_layer_snapshot`); the surface ergonomics
> (tiers/staleness/recovery) — the snapshot-query-ergonomics concept.

## Command flow (end-to-end)

Take a structural mutation "create a layer" as an example:

1. The agent calls an MCP tool → the server assembles `{action, options}` and sends it through the relay.
2. The adapter: snapshot-before of the tree → runs the handler in modal/transaction → snapshot-after →
   computeTreeDiff(before, after) → `treeChanges:[{op:"add", id:789, ...}]` + `treeHash` + structural-seed
   `attrChanges` for the new node + `scopeId`.
3. The server: applies the diff to the mirror → cross-checks `treeHash` (mismatch → forced rebuild) →
   stamps `stateVersion` → **strips** the bulky payload → returns a thin response to the model
   (`{response, status, stateVersion}`).
4. The agent sees "layer 789 added, version N" — **~200–500 bytes**, not a tree of ~30 KB.
5. From there the agent **navigates** via `query`/`subtree` — reads the mirror, without the host.

## Why exactly this (rationale for hand-rolled)

Triangulating three independent prior-art analyses gave a **unanimous verdict: there is no adoptable
library, we keep it hand-rolled**:

- **Tree-diff:** cross-language diff libs are either single-language (jsondiffpatch=JS, DeepDiff=Py), or
  positional (RFC 6902 JSON Patch addresses by index → loses our `id` key, reopens the int-key ordering
  problem). Our id-keyed format with a structure/attrs split is **better tailored** to the domain.
- **Replica-sync:** CRDT (Yjs/Automerge) solve *multi-writer* — a problem we don't take on under the
  single-writer precondition (§"Required precondition"); Merkle libs are list/blockchain-oriented and
  don't solve the hard part (cross-language-deterministic serialization of *our* node shape).
- **Agent-context:** we found no ready reusable framework "mirror-a-tree-for-agent" (the browser/
  engine/IDE/design MCPs surveyed all hand-roll on top of the MCP SDK). The closest pattern reference —
  **Playwright MCP** (a11y-snapshot + ref), but its refs are **snapshot-scoped** (regenerated on every
  snapshot) — it has no durable mirror with stable handles, on which our query layer rests.

That is, the architecture is a **deliberate choice**, not over-engineering and not NIH. This is exactly
what makes it worth publishing as a **pattern**: anyone building an MCP over a large hierarchy otherwise
rediscovers it from scratch.

## Component maturity

Not everything is equally "ready." "Proven in production" below means the component is proven in the
**production system** the pattern was extracted from; the reference skeleton (this repo) is an
extraction of it, validated by the conformance suite — not the production binary. The map (details and
design directions — [open-problems](open-problems.md)):

| Component | Status |
|---|---|
| Mirror (tree/attrs/meta/selection) | **proven in production** |
| Diff-as-response + action classification | **proven** |
| Keyed reconciliation (add/remove/move/typeChange) | **proven**; gap: same-parent reorder |
| Root-hash drift + forced rebuild | **proven**; localization (Merkle) — not done |
| Push listener for manual edits | **proven** (live-probe) |
| Query layer + tiers + staleness | **proven**; part of the ergonomics — in progress |
| Targeted mutations (set-vs-merge) | **partial** — a reference exists, no audit of the write side |
| **Mutation batching** (1 tool-call → 1 modal pass) | **not finished** — the main speed lever |
| **Safe-wait / serialization of concurrent commands** | **not finished** — a weak seam of the transport |
| attrs cross-hash (per-layer) | **not done** — attrs are resynced wholesale |
| Mirror persistence between sessions | **non-goal** (id is stable only within a session) |

## Links

- [README](../README.md) — bundle map and pitch.
- [portability](portability.md) — host-agnostic kernel vs adapter; Photoshop/Figma/Unity mapping.
- [open-problems](open-problems.md) — the unfinished pieces (batching, safe-wait, diff localization) — in depth.- Grounding: the server-side mirror + sync kernel + host adapter (structural read, tree-/attr-diff
  compute) of the production Photoshop MCP server.
- Prior-art / rationale: the research notes (tree-diffing / replica-sync / agent-context).
- Internal source concepts: the archived snapshot-store / snapshot-attributes / snapshot-drift-listener concepts.

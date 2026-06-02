# TreeLens — portability: kernel vs adapter

> What in the pattern is **host-agnostic** (the reusable kernel) and what the host **must** implement
> itself (the adapter). Plus an honest mapping onto Photoshop (proven), Figma and Unity — with the
> **verified** constraints of each. Reference design — [pattern](pattern.md); what is unfinished —
> [open-problems](open-problems.md).
>
> Facts about Figma/Unity/MCP were checked by web research (2026-05-28); places where the primary
> source gave no unambiguous answer are marked **(unconfirmed)** — we do not pass off a hypothesis as
> fact.

## Three linchpin axes

Portability of the pattern onto a given host is decided by **three axes**. The diff mechanics and query
layer port cleanly almost always; what usually breaks is one of these axes:

1. **Node id stability.** Keyed reconciliation rests on it. Stability is needed **within a session,
   including undo/redo**. Stability **across sessions** is a separate, weaker requirement (needed only
   for persistence of the mirror across restarts).
2. **Execution model.** Do mutations need a special scope (modal / main-thread / transaction)? Can N
   mutations be collapsed into one undo-step? This **does not touch the kernel** — it lives in the
   adapter, but it is most often what forces compromises (batching, atomicity).
3. **Event model.** Can the host **push** a notification about an edit made *outside* the agent (a user
   touched the document by hand)? Without it the mirror cannot learn that it went stale from a manual
   edit — all that remains is the integrity hash, which catches drift on the agent's next mutation.

## Host-adapter contract

The minimal interface the host must give the kernel. **Mandatory** — without it the mirror is incorrect
or impossible; **optional** — degrades gracefully.

| # | Capability | Status | Why |
|---|---|---|---|
| **A** | **Stable node id** | **mandatory** | The whole diff matches nodes by id, not by position. Positional matching mis-diffs `[A,B,C]→[B,C,D]` as 3 replacements instead of remove+add. Stability is needed within the session + across undo/redo. Without it the mirror silently corrupts. |
| **B** | **Structural read** | **mandatory** | Read the whole tree as `{id,type,children}` on request. This is the primitive of **bootstrap** and **drift-recovery**: even a perfect diff stream needs something to seed from and something to fall back to on mismatch. |
| **C** | **"mutation → diff" hook** | **mandatory** | After a mutation, return *what changed*, not full state. If the host can only return full state, the pattern **degrades** to "rebuild on every mutation" (the kernel digests this through the single-`rebuild`-op path), losing the token win of diff-as-response. |
| **D** | **Integrity hash** | **mandatory (de-facto)** | Root hash over the canonical serialization of the tree, cross-checked host↔server; mismatch → force rebuild. Formally the pattern *works* without it, but then bugs in the incremental diff and the host's blind spots accumulate unnoticed. For production — mandatory. **Only the structural** hash is cross-checked (on attrs — see [pattern](pattern.md) §5). |
| **E** | **Attribute read** | **optional (tiered)** | Per-node properties (name/visibility/bounds/effects/…) separate from structure. Enables tiered disclosure. Without it the mirror degrades to structure-only navigation (still useful). |
| **F** | **Push notification of an edit** | **optional, but a correctness linchpin** under human-in-the-loop | A signal that the host changed *outside* the agent's commands. Without it the mirror does not know about a manual edit; the only safety net is the hash (D), which catches drift on the agent's *next* mutation. For headless/agent-only hosts — genuinely optional. |
| **G** | **Execution scope / transaction** | **optional; form host-specific** | modal / main-thread / undo-grouping. Does not touch the kernel; entirely in the adapter. But this is where batching/atomicity most often breaks — assess per host. |

**Contract in one line:** *give the kernel **(A)** stable ids, **(B)** a full structural read and
**(C)** a per-mutation diff (or a fallback to full state), back it with **(D)** a root hash; everything
else (attrs, events, scope) is graceful-degradation territory.*

## Kernel (host-agnostic) vs adapter (host-specific)

### Kernel — reusable library (written once)

Nothing here names a concrete host. Its grounding is the server-side mirror + sync kernel of the
production Photoshop MCP server.

- **Store** — per-document tree `{id,type,children}` + id index + attrs + meta + selection + a shared
  version counter. Pure data structures.
- **Diff-apply** — keyed reconciliation by id; tree ops `add/remove/move/typeChange` (a group is an `add` with `type="GROUP"`, there is no separate `addGroup` op),
  attr ops `attrSet/attrDelete`, wholesale `rebuild`/`attrsRebuild`/`metaRebuild`/`selectionSet`.
  **Atomic with rollback** (deep-copy → replay → restore on failure of any op).
- **Integrity hash** — sha256 over the canonical serialization of the tree.
- **Query layer** — regex search / subtree(depth) / path / attributes; all from the store, no round-trip.
- **Drift orchestration** — routing the response by the change channel, applying the diff, checking the
  hash, triggering a rebuild on mismatch, annotating the version, **stripping the heavy payload** before
  handing it to the model. (Triggering the rebuild — kernel; fetching the fresh tree itself — adapter.)

### Adapter — host-specific (rewritten for each host)

Its grounding is the host adapter (command handlers + entry wiring) of the production Photoshop MCP
server.

- **Structural read** — traverse the host scene-graph → `{id,type,children}`.
- **Diff-compute** — `computeTreeDiff(before, after)` + capture attrs before/after. *The diff algorithm
  itself is host-agnostic and can live in the kernel; in the production Photoshop MCP server it is on the
  host side so that a diff already goes over the wire. A host that can only return full state returns it
  — and the kernel diffs on its own end.*
- **Mutating handlers** — one async function per action, inside the execution scope (G).
- **Action classification** — which operations belong to which channel (structural / attr / rebuild
  / meta / selection). Host-specific.
- **Host hash** — over the canonical serialization of the host tree; **byte-for-byte** identical to the kernel's.
- **Event bridge** — a listener on the host's mutating events → dirty flag → swap to rebuild.
- **Fetch transport** — the internal round-trips by which the kernel pulls the fresh tree/attrs/meta/selection.

**Clean seam:** the kernel knows only `{id,type,children}`, attr dicts and the diff-op vocabulary. The
adapter knows Photoshop/Figma/Unity. The contract between them is the **channels**
(`treeChanges`/`attrChanges`/`metaChanges`/`selectionChanges`) + `treeHash` + `scopeId`. Normative
description — `wire-protocol.md`.

## Mapping per host

### Photoshop (production Photoshop MCP server) — **proven, reference implementation**

- **id:** `layerId` is stable within a session, survives undo/redo and the working session.
- **execution:** `executeAsModal` (the `execute()` wrapper); N operations → one modal/undo-step; **not
  re-entrant**.
- **events:** `action.addNotificationListener` on mutating events; PS **does not send notifications for
  the plugin's own `executeAsModal` operations** → any received event = a user edit. This is the
  empirical anchor of the push-listener.

> Practical implementation guide for the Photoshop adapter (recipe for the byte-exact hash, clean tree
> structure, modal, push-listener, transport relay) — `adapters/photoshop.md`.
- **Main pitfall:** `executeAsModal` is **not re-entrant** — `captureAttrs`/`getStructuralTree` must run
  *outside* the handler's `execute()`, not nested.
- **Verdict:** clean (the pattern was grown on it).

### Figma — **needs-adaptation** (maps onto the **Plugin API**, not the official MCP server)

- **id:** node ids (`"1:3"`) are stable **within a session**, serializable; **not guaranteed across
  sessions** (confirmed — they go stale on reopen); `getNodeByIdAsync` → null if the node was deleted.
  → persistence of the mirror across sessions requires re-keying.
- **execution:** **no modal scope**; mutations auto-group into one undo-step, `figma.commitUndo()` places
  checkpoints along the way (confirmed — mid-batch is possible). The API is **async-first**; nodes outside
  the active page — only through `getNodeByIdAsync`.
- **events:** `figma.on('documentchange')` — 6 types (CREATE/DELETE/PROPERTY_CHANGE + style variants)
  tagged `LOCAL`/`REMOTE` → an excellent drift signal. **BUT there are confirmed blind spots:** nested
  child operations emit only the **parent** CREATE/DELETE; **Variables emit no event at all**; a change of
  text indentation/`listOptions` — no event. → drift detection **cannot** be built on the event alone, the
  integrity hash (D) is mandatory as a safety net.
- **Important:** the **official Figma MCP server is deliberately stateless/sparse** (`get_metadata` →
  `get_design_context`/`get_screenshot` by nodeId, with no server-side mirror). That is **a different
  goal**, not ours. The TreeLens pattern maps onto the **Plugin API + a local mirror**, not their MCP
  server.
- **Realtime multiplayer — outside the pattern's envelope.** A `documentchange` tagged `REMOTE` = edits by
  *other collaborators* in real time. This violates [pattern](pattern.md) §"Required precondition" (a single active
  writer): a rare local edit the mirror absorbs with a rebuild, but a **continuous concurrent stream of
  REMOTE mutations** keeps it in permanent drift, and the pattern has no merge history. A solo session
  (one collaborator + agent) is fine; collaborative realtime editing is **outside the current scope, not
  designed** (future attempts are not ruled out, but there is no ready path).
- **Unconfirmed:** threading/parallelism of `getNodeByIdAsync`; conflict resolution of `setPluginData` in
  multiplayer (shared Figma is last-write-wins, but for plugin-data it is undocumented); atomicity/
  batching of the official MCP server's write tools for ordinary design operations.
- **Verdict:** needs-adaptation **for a solo session** (a single active writer). Diff/query port cleanly;
  the undo/transactional model is simpler, but without an atomic multi-op scope; cross-session id and the
  event blind spots are the main pitfalls; realtime multiplayer is out of scope.

### Unity (Editor) — **needs-adaptation → hard**

- **id:** `instanceID` is **session-scoped**, **invalidated by a domain reload** (script recompilation /
  entering Play-mode) — unfit for persistence; `GlobalObjectId` is stable across sessions, **but only
  after the scene has been saved to disk** (confirmed: an unsaved scene → `assetGUID` = null). → for live
  unsaved work — only `instanceID` + rebuild-on-reload.
- **execution:** **main-thread only**; all Editor API via `EditorApplication.delayCall` /
  `EditorCoroutineUtility`. "One undo-step" = `Undo.RecordObject` + `Undo.CollapseUndoOperations` —
  **ties batch atomicity to the undo history**, there is no clean modal scope.
- **events:** `ObjectChangeEvents.changesPublished` (14 `ObjectChangeKind` types, per-object) + a coarse
  `EditorApplication.hierarchyChanged`; **both frame-batched/deferred**.
- **Main pitfall:** **a domain reload wipes in-memory state** — along with the server's mirror. Confirmed:
  all 3 surveyed Unity MCPs ([CoderGamester/mcp-unity](https://github.com/CoderGamester/mcp-unity),
  [CoplayDev/unity-mcp](https://github.com/CoplayDev/unity-mcp),
  [NoSpoonLab/unity-mcp](https://github.com/NoSpoonLab/unity-mcp)) chose
  a **stateless round-trip precisely to work around it**. The mirror here is a **tradeoff** (rebuild-on-reload),
  not a clean win.
- **Unconfirmed:** coalescence of `ObjectChangeEvents` on create-delete-recreate in one frame;
  ordering/re-entrancy of `EditorApplication.delayCall`; cross-frame safety of
  `Undo.CollapseUndoOperations`.
- **Verdict:** needs-adaptation → hard. The tree/diff mechanics port, but the lifetime hazard (domain
  reload) and id history put the mirror in question on large scenes — honestly present it as a compromise.

## Summary table of verdicts

| Host | id stability | Execution model | Event model | Verdict | Main pitfall |
|---|---|---|---|---|---|
| **Photoshop** | in session + undo/redo ✓ | `executeAsModal`, 1 undo-step, not re-entrant | listener; own ops not notified → any event = user edit | **clean** | `executeAsModal` not re-entrant |
| **Figma** | in session ✓; across sessions ✗ | no modal; auto undo-group + `commitUndo()`; async | `documentchange` (6 types, LOCAL/REMOTE) — **with blind spots** | **needs-adaptation** | event blind spots + cross-session id (map onto Plugin API, not the MCP server) |
| **Unity** | `instanceID` session ✗reload; `GlobalObjectId` only saved-scene | main-thread; undo-grouping (not modal) | `ObjectChangeEvents` (14 types) — **frame-batched** | **needs-adaptation → hard** | domain reload wipes the mirror |

**Generalization:** Photoshop scores high on all three axes — which is why it became the proving ground.
The typical failure elsewhere is **not** the diff mechanics (those port), but **(1) id instability across
sessions** (Figma, Unity) and **(2) the state lifetime hazard** (Unity domain reload). The event model is
the most *available* axis, but the most *incomplete* one (Figma's blind spots, Unity's frame-batching).

## Links

- [pattern](pattern.md) — reference design and vocabulary.
- [open-problems](open-problems.md) — cross-session persistence (§6) — runs straight into the cross-session id of this axis.- This repo — the reference skeleton: `treelens/adapter.py` (the contract ABC), `wire-protocol.md` (the seam).

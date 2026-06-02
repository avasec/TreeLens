---
type: reference
subtype: stack
updated: 2026-06-02
---

# TreeLens — an adapter for Photoshop (UXP): a practical guide

The TreeLens kernel (`treelens/`) is host-agnostic. To stand up a mirror over Photoshop, you implement
**`HostAdapter`** (`treelens/adapter.py`) against the UXP API. This guide is a distillation of how the
**production Photoshop MCP server** — from which the pattern was extracted — does it: what is thin on the
PS side and where the pitfalls are. The normative seam contract — [wire-protocol](../wire-protocol.md);
the portability axes — [portability](../docs/portability.md).

> **Why this guide exists.** There is no public reference PS adapter in the repo (this is an honest
> open-problem — [open-problems](../docs/open-problems.md) §8: the production system is private). The guide +
> **golden vectors on real data** (`tests/hash_vectors_realworld.json`) partially substitute for it: real
> tree shapes + verifiable byte-for-byte hash parity. "Take it and implement against the contract", rather
> than "implement blind".

`HostAdapter` is five methods; below each is projected onto UXP reality.

## 0. Transport: why a relay is needed

A UXP plugin can only be a **socket.io client**, not a server. So a thin Node relay is placed between the
Python kernel and the plugin (one socket server, routing by `application`). The kernel sends a command →
relay → the plugin executes it in PS → the response envelope ([wire-protocol](../wire-protocol.md) §1)
goes back the same way.

```
kernel (Python) ──ws──▶ relay (Node) ──ws──▶ UXP plugin ──Photoshop API──▶ Photoshop
```

## 1. `read_tree(scope_id)` → the pure structure `{id, type, children}`

Walk the document's layer tree and return **only the structure**: `{id, type, children}` per node, with
the root sentinel on top. `id` is `layer.id` (see "id stability"). `type` is the layer category
(`GROUP` / `PIXEL` / `TEXT` / `SMARTOBJECT` / …).

> **The root sentinel's type.** The spec (`schema/node.schema.json`) requires `type: "ROOT"` on the root.
> The production Photoshop server historically returned `"DOCUMENT"` (visible in
> `tests/hash_vectors_realworld.json`) — the hash doesn't suffer from this (the kernel is type-agnostic),
> but the schema validator will reject such a root. **Normalize the root to `"ROOT"`** on the adapter side.
> (The spec choice itself — fixed `ROOT` vs host-defined root — is still open.)

**Hard rule: name/visibility/opacity/bounds/effects are NOT placed in a tree node** — they live in attrs
(§3). The reason is twofold: (1) these fields change often and independently of structure — keep them in
the tree, and any rename would jiggle the tree-diff; (2) a pure `{id,type,children}` serializes
**cross-language-deterministically** (fixed string keys, no int-keyed maps) — the hash rests on this (§2).
A rename in TreeLens is an **attr mutation**, not a structural one.

## 2. `canonical_hash(tree)` → a byte-exact recipe (the thinnest spot)

The adapter computes `treeHash = sha256(canonicalSerialize(tree))` on the host side; the kernel, after
applying the delta, recomputes its own hash and compares. **Mismatch → forced rebuild.** So your
`canonicalSerialize` must produce a byte-for-byte identical stream to the kernel's
([wire-protocol](../wire-protocol.md) §7), otherwise drift "fires" out of nowhere and the mirror rebuilds
constantly.

The recipe (matches the kernel's `stable_serialize`): **object keys in sorted order, primitives via
`JSON.stringify`, recursively, no whitespace**:

```javascript
const stableSerialize = (v) => {
  if (v === null || v === undefined) return "null";
  const t = typeof v;
  if (t === "number" || t === "boolean" || t === "string") return JSON.stringify(v);
  if (Array.isArray(v)) return "[" + v.map(stableSerialize).join(",") + "]";
  if (t === "object") {
    return "{" + Object.keys(v).sort()
      .map((k) => JSON.stringify(k) + ":" + stableSerialize(v[k])).join(",") + "}";
  }
  return JSON.stringify(v);
};
// treeHash = sha256Hex(utf8(stableSerialize(tree)))
```

UXP doesn't give a stable `crypto.subtle`, and dragging npm into the plugin is painful — the production
adapter carries a **pure-JS sha256** (FIPS 180-4, sync, no dependencies, auditable by eye).

**Why this only works on the pure structure.** The keys `{id, type, children}` are fixed strings, sorted
identically in any runtime. But attrs are keyed by **int-id**, and JS sorts int keys lexicographically,
Python numerically → the byte streams diverge. That's why TreeLens **cross-checks only the tree hash**, and
resyncs attrs wholesale (§3; [open-problems](../docs/open-problems.md) §4 — the plan to lift this with
per-layer hashes).

> **Adapter validation.** Run your `canonicalSerialize` against `tests/hash_vectors.json` (toy vectors)
> **and** `tests/hash_vectors_realworld.json` (real PS trees, up to 242 nodes, whose hashes the production
> JS adapter emitted and the Python kernel reproduces). Byte-for-byte match — your hash seam is compatible;
> no match — the adapter is incompatible, fix the serialization, not the kernel.

## 3. `read_attrs(scope_id, node_ids)` → attributes separate from the structure

Per-node properties: `name` / `visible` / `opacity` / `blendMode` / `bounds` / `isClippingMask` —
**DOM-cheap**, read synchronously. `effects` (via batchPlay `get layerEffects`) and `hasMask`
(`get hasUserMask`) are **expensive async** batchPlay; don't read them until the model has requested the
corresponding tier (S4 progressive disclosure).

attrs are **not cross-hashed** (see §2) — scoped deltas are trusted on their word, a full attrs resync is
done on rebuild. Don't try to fold attrs under the common tree hash.

## 4. `transaction(fn)` → `executeAsModal`

All Photoshop mutations go inside a modal scope: wrap `fn` in `executeAsModal` (the helper wrapper
`execute()`). Two rules:

- **Always `await`** — without it the mutations silently go nowhere.
- **Modal is not re-entrant** — you cannot nest one `executeAsModal` inside another. The concurrency
  limitation grows from this too: two simultaneous tool calls both ask to enter modal → collision →
  silent mirror drift. Right now the pattern **cures** this with a forced rebuild (recovery), rather than
  preventing it — a command queue (single-flight) is still in design ([open-problems](../docs/open-problems.md) §2).

## 5. `on_external_change(callback)` → a notification listener for manual edits

The user can edit the PSD by hand, around the agent. Subscribe to mutating PS events
(`action.addNotificationListener`) and on each one — mark the scope "dirty" (`userDirty`); the next command
will replace its incremental diff with a full rebuild of the current state.

**The key insight this rests on:** Photoshop **does not send notifications for the plugin's own ops**
(inside `executeAsModal`). So any *received* event = a **user** edit, not an echo of your own command. That
is why the listener catches exactly external edits. (select/navigation is excluded from the event set —
these are not mirror mutations.)

## 6. Diff: compute it yourself or hand it to the kernel

Two paths, chosen by the adapter's `returns_full_state` flag:

- **`returns_full_state = false`** (like the production server): the adapter computes the delta itself —
  snapshot-before of the tree → runs the handler in modal → snapshot-after → `compute_tree_diff` → sends
  `treeChanges` + `treeHash`. More efficient (only the delta goes over the wire), but more UXP code.
- **`returns_full_state = true`**: the adapter just hands the full post-state in `treeAfter`, **the kernel
  diffs it itself** and stamps the hash ([wire-protocol](../wire-protocol.md) §2). Less code on the UXP side.

**Recommendation:** start with `returns_full_state = true` (minimum UXP code, the kernel takes the diffing
on itself), switch to host-side diff when you hit an efficiency wall.

## A minimal slice for a proof-of-concept

To prove the pattern against a live Photoshop with minimal code: **`read_tree` + `canonical_hash` +
`on_external_change` + a query surface**, `returns_full_state = true`, no mutations. The scenario: "install
the plugin, open a PSD, watch the mirror bootstrap; edit a layer by hand — the mirror resyncs; navigate via
`query`/`subtree`/`path` — without a round-trip to PS". Mutations (creating/deleting/transforming layers)
are an extension on top of this skeleton.

## Pitfalls (summary)

- **`layer.id` stability** — keyed reconciliation rests on it ([portability](../docs/portability.md), axis 1).
  Stability within a session is required, **including undo/redo**. An unstable id → the diff silently
  corrupts the mirror.
- **Byte-exactness of the hash** (§2) — the most common integration error; caught by the golden vectors.
- **`executeAsModal` re-entry** (§4) — don't nest; concurrency is on recovery for now, not prevention.
- **Bulk mutations** (dup+translate ×N) — batching is not implemented ([open-problems](../docs/open-problems.md) §1);
  for now "one tool = one modal pass".

## Links

- [wire-protocol](../wire-protocol.md) — the normative contract of the envelope/ops/hash (§7 — the hash contract).
- [portability](../docs/portability.md) — the three linchpin axes (id stability, transaction model, push events).
- [open-problems](../docs/open-problems.md) — §1 batching, §2 safe-wait/concurrency, §4 attrs cross-hash, §8
  (this guide as a partial substitute for a public PS adapter).
- `treelens/adapter.py` — the `HostAdapter` ABC (the executable form of the contract).
- `adapters/in_memory.py` — the toy host (a runnable example without Photoshop).
- `tests/hash_vectors.json` + `tests/hash_vectors_realworld.json` — vectors for validating your
  `canonical_hash`.

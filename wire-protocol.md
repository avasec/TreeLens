---
type: reference
subtype: architecture
updated: 2026-06-02
---

# TreeLens — wire protocol (normative)

The single source of truth for the **seam between the adapter (host) and the kernel (server)**. An adapter
author for Photoshop/Figma/Unity/their own host implements exactly this contract; the kernel (`treelens/`)
already knows how to consume it (`treelens/lens.py`). For semantics and rationale see [pattern](docs/pattern.md);
for the executable form of the contract — `treelens/adapter.py`.

The keywords **MUST/SHOULD/MAY** are used in the RFC 2119 sense.

## 0. Glossary (canonical vocabulary)

A single vocabulary for the whole pattern — the spec, the code, and the prose all hold to these names.

| Term | What it is |
|---|---|
| **TreeLens** | The pattern, the repository, and the facade/orchestrator class. The brand lives **only** here. |
| **kernel** | The host-agnostic library as a whole (the `treelens/` package). |
| **Mirror** | The in-memory replica of state on the server (tree + attrs + meta + selection). The "mirror" metaphor = state. |
| **HostAdapter** | The host seam — the only code the adapter author writes. |
| **scopeId** / `scope_id` | Identifier of the mirrored scope (document / scene / file are **examples** of a scope). `scopeId` on the wire (camelCase), `scope_id` in Python. Generalizes the host-specific `documentId`. |
| **stateVersion** | An opaque, monotonically increasing per-scope correlation token that the kernel stamps into the response (semantics — §9). |
| **canonical serialization** | Byte-deterministic serialization of the tree over which the integrity hash is computed. The concept = "canonical serialization"; the JS example is `canonicalSerialize` (§7); the Python implementation is `stable_serialize`. |
| **tree-op / attr-op / envelope / node** | Neutral names for the protocol primitives — NOT branded (no `LensNode`/`TreeLensEnvelope`). |

Deliberately NOT pattern terms: **store** (an internal detail of the Mirror — its sub-stores, not a public concept), **core** (too generic), **snapshot** as a subsystem/field name (the word "snapshot" — only for a full capture of state).

## 1. Response envelope

For each command the adapter returns one JSON object:

```jsonc
{
  "status": "SUCCESS" | "FAILURE",
  "response": <any serializable handler payload, or null>,
  "scopeId": <document/scene/file id>,           // MUST on any mutation that changes the mirror
  "treeChanges":      [ <tree-op>, ... ],        // structural channel (opt.)
  "treeHash":         "<sha256 hex>",            // MUST with treeChanges; with treeAfter the kernel computes it (§2) — the adapter MAY omit it
  "treeAfter":        <root sentinel>,           // MAY: full post-state INSTEAD of treeChanges (see §2)
  "attrChanges":      [ <attr-op>, ... ],        // attributes channel (opt.)
  "metaChanges":      [ <meta-op>, ... ],        // meta channel (opt.)
  "selectionChanges": [ <selection-op>, ... ],   // selection channel (opt.)
  "message": "<error text>"                      // MUST if status=FAILURE
}
```

- On **FAILURE** the kernel does not touch the mirror and propagates the error; channels are ignored.
- Channels are **independent**: `attrChanges` may arrive with `treeChanges` (structural-seed) or without it
  (scoped attr-mutation). Any of the channels may be absent.
- The kernel **annotates** the response with `stateVersion` (after applying; semantics — §9) and
  `driftRecovered` / `resyncedExternalEdit` on recovery; the adapter does not send these.

## 2. Action classes → which channel

Each mutating action belongs to **exactly one** class; the class chooses the delta form. The choice is made by
the **adapter code**, not the model.

| Class | What it changes | Channels in the envelope |
|---|---|---|
| **structural** | tree structure | `treeChanges` + `treeHash` (+ `attrChanges` structural-seed for new nodes) |
| **attr-mutation** | node attributes | `attrChanges` (scoped), **without** `treeChanges` |
| **rebuild** | everything (new scope / flatten / active switch / global edit) | `treeChanges:[{op:"rebuild",tree}]` + `treeHash` + bootstrap `attrsRebuild` + `metaRebuild` + `selectionSet` |
| **doc-meta** | the header only (save etc.) | `metaChanges` |
| **selection** | the selection marquee | `selectionChanges` |
| **read-only / lifecycle / image** | nothing in the mirror | only `response` + `status` |

**Full-state fallback (simplified on-ramp).** If the host cannot compute a diff cheaply, the adapter
sets `returns_full_state = true` and on structural mutations sends the **full post-tree** in the
`treeAfter` field (root sentinel) instead of `treeChanges`. The kernel itself computes `compute_tree_diff(current
mirror, treeAfter)`, stamps `treeHash`, applies it, and **strips** `treeAfter` from the model's response.
The cost is a full structural read per mutation instead of a small delta; in exchange the adapter writes no
diff code.

## 3. Node form and the id precondition

- A tree node is a **pure structure**: `{"id": <stable>, "type": <str>, "children": [<node>, ...]}`.
  Name/visibility/opacity/bounds/effects/… **MUST** live in attrs, **not** in the tree node.
- The root is a **root sentinel**: `{"id": null, "type": "ROOT", "children": [...]}`. Top-level nodes are
  its `children`. The sentinel is not handed out. The root's `type` is a **fixed protocol marker
  `"ROOT"`** (not a host name): the adapter **MUST normalize** its root to it (e.g. Photoshop's "DOCUMENT"
  → "ROOT"). The kernel is type-agnostic and will accept any root, but the hash includes `type` — so the marker
  is fixed to make the cross-language hash deterministic. (Realworld vectors carry the raw "DOCUMENT" as a
  pre-normalization capture — flagged in `tests/hash_vectors_realworld.json`.)
- **Precondition (MANDATORY):** a node's `id` **MUST** be stable within a session, **including
  undo/redo**. All of keyed reconciliation rests on this. Stability across sessions is a separate, weaker
  requirement (needed only for persistence of the mirror across restarts; for many hosts it does not
  hold — see [portability](docs/portability.md)).
- Node matching in diff/apply is **strictly by `id`**; positional fallback is **forbidden**.

## 4. Tree-op dictionary

Computed by the adapter (`compute_tree_diff`), applied by the kernel (`Mirror.apply_tree_diff`). The symmetry
is mandatory. **This is the complete list** — a container node (group) is an ordinary `add` with `type="GROUP"`
and non-empty `children`; there is no separate `addGroup` op.

```jsonc
{ "op": "add",        "id": <id>, "type": <str>, "parentId": <id|null>, "index": <int>, "children": [<node>] }
{ "op": "remove",     "id": <id> }
{ "op": "move",       "id": <id>, "toParent": <id|null>, "newIndex": <int> }
{ "op": "typeChange", "id": <id>, "from": <str>, "to": <str> }
{ "op": "rebuild",    "tree": <root sentinel> }     // degenerate: full replacement
```

- `add` is emitted only for the **topmost** added node of a subtree; its `children` carry all the new
  structure (descendants are not duplicated as separate `add`s).
- `move` — the node is moved by reference; `parentId/toParent = null` = top-level.
- `typeChange` — atomicity: `from` **MUST** match the node's current type in the mirror, otherwise this is drift
  (the kernel throws → external rebuild).
- `rebuild` — the only op in the array; the kernel absorbs `tree` and **strips** it from the model's response.

## 5. Attr-op dictionary

```jsonc
{ "op": "attrSet",      "id": <id>, "key": <str>, "value": <any> }
{ "op": "attrDelete",   "id": <id>, "key": <str> }
{ "op": "attrsRebuild", "attributes": { "<id>": { "<key>": <value>, ... }, ... } }   // full replacement
```

- `attrChanges` arrives in **three forms**: scoped delta (attr-mutation, without treeChanges);
  structural-seed (alongside treeChanges, the attributes of new nodes); `attrsRebuild` (on rebuild events).
- **The attrs hash is NOT cross-checked.** An external map keyed by int-id serializes differently across
  runtimes (the ordering of int keys) → the hashes diverge. Therefore attrs are resynced **wholesale** on
  rebuild, and scoped deltas are **trusted**. The fix (per-layer hashes) — [open-problems](docs/open-problems.md) §4.

## 6. Meta- and selection-ops (wholesale)

```jsonc
{ "op": "metaRebuild",  "meta": { ... } }            // flat document header
{ "op": "selectionSet", "selection": { ... } }       // selection state
```

Small flat states — replaced in full, without a granular diff.

## 7. Integrity-hash contract

`treeHash = sha256_hex( canonicalSerialize(tree) )`. The adapter, on the host side, **MUST** compute
`canonicalSerialize` so that the byte stream **matches the kernel** — this is the most delicate part of the
cross-language seam (the host may be in JS/C#/Swift, the kernel in Python). The match is required **only**
for the tree (fixed string keys + ordered `children`); it does not extend to attrs (§5).

**Canonicalization (runtime-neutral, in the spirit of RFC 8785 / JCS)** — the specification is **not** in terms
of a single language:

- Output — **UTF-8** bytes.
- Object: `{` + pairs `"<key>":<value>` separated by `,` + `}`; **keys sorted in ascending order of Unicode
  code points (code point)**.
- Array: `[` + elements separated by `,` + `]`; **order is preserved** (for `children` it is significant).
- Strings: in double quotes, minimal JSON escaping; **non-ASCII is NOT escaped** (raw
  UTF-8, i.e. `ensure_ascii=false`).
- **No insignificant whitespace** (neither between tokens nor as indentation).
- A numeric `id` is an integer, **without a decimal point or exponent** (`5`, not `5.0`). `null` is the literal `null`.
- In sum a node serializes exactly as `{"children":[...],"id":<...>,"type":"<...>"}` (key order
  `children` < `id` < `type` by code point).

The kernel reference is `treelens/hashing.py` (`json.dumps(obj, sort_keys=True, ensure_ascii=False,
separators=(",", ":"))`). The minimal JS equivalent for the adapter:

```js
function canonicalSerialize(v) {
  if (Array.isArray(v)) return "[" + v.map(canonicalSerialize).join(",") + "]";
  if (v && typeof v === "object")
    return "{" + Object.keys(v).sort()
      .map((k) => JSON.stringify(k) + ":" + canonicalSerialize(v[k])).join(",") + "}";
  return JSON.stringify(v); // strings/null; integer ids — no fractional part
}
// treeHash = sha256_hex( utf8( canonicalSerialize(tree) ) )
```

(`Object.keys().sort()` sorts by UTF-16 code unit — for the ASCII keys `children/id/type` this coincides
with code point. If an object's keys can contain non-BMP characters — sort by code point.)

**Test vectors — `tests/hash_vectors.json`** (+ `hash_vectors_realworld.json`, real PS trees):
a set of `{tree, sha256}` against which an adapter in **any language** checks byte-for-byte parity with the kernel.
They are run from **both** sides: `tests/test_conformance.py::test_hash_vectors` (the Python kernel) and
`js/check_vectors.js` (an **independent JS implementation**, in CI a live cross-language gate, not Python-against-
itself). If even one vector fails to match → canonicalization has diverged, drift detection is broken (everything will
go into a perpetual full-rebuild — without a visible error). Fix the canonicalization, not the logic. Validate your own
adapter the same way — run its `canonical_hash` against these vectors.

## 8. Drift and recovery (the kernel's responsibility)

1. **Bootstrap.** If the scope is not yet in the mirror but an incremental delta arrives (or `treeAfter`) —
   the kernel pulls the full tree via `adapter.read_tree(scope)` (+ `read_attrs`) and seeds the mirror.
2. **Hash-mismatch.** After applying the delta the kernel checks `treeHash`. No match → `adapter.read_tree`
   → wholesale rebuild; the response is flagged `driftRecovered: true`.
3. **Push-listener (external edit).** If the adapter called `on_external_change(scope)` (the user edited
   the host outside the agent's commands), the kernel on the **next** command does a full rebuild instead of an
   increment; the response is flagged `resyncedExternalEdit: true`. The adapter **SHOULD** not notify about its own
   plugin mutations (then any event = a user edit).

## 9. What the kernel does with the envelope

- Applies the channels to the mirror (atomically, with rollback on an op error). Full-state fallback (`treeAfter`,
  §2): it computes `treeChanges` and `treeHash` itself, then strips `treeAfter`.
- Checks the hash, and where needed — recovery (§8).
- Stamps `stateVersion` — **an opaque, monotonically increasing per-scope token for correlating
  responses**, NOT a change counter: one command may bump the version by several (a rebuild hits
  tree+attrs+meta+selection). Do not build logic on its absolute value or delta.
- **Strips the heavy payload** (`tree` in the rebuild op, bootstrap `attrChanges`, `treeAfter`) before
  returning to the model — the mirror has already absorbed it; the model navigates with query-tools.

## 10. Stage-1 limitation

**Same-parent reorder** (reordering children without changing parent) `compute_tree_diff` **does not emit** —
it is caught by the `treeHash` mismatch → rebuild. A deliberate gap; closing it (LIS-reorder) — [open-problems](docs/open-problems.md) §3.

## Links

- `treelens/adapter.py` — the executable form of the contract (§3-§8).
- `treelens/diff.py` / `treelens/mirror.py` — the op dictionaries (§4-§6) and their application.
- `tests/hash_vectors.json` — the cross-language hash test vectors (§7).
- [portability](docs/portability.md) — the host-adapter contract in prose + host mapping.
- [pattern](docs/pattern.md) — why it is built this way.

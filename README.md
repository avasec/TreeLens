# TreeLens

**A server-side mirror of a live hierarchy for an LLM agent — a pattern for building an MCP over
applications with a large mutable tree** (Photoshop, Figma, Unity, …), plus a minimal portable code
skeleton that runs **without a single real application**.

## The problem in one sentence

An agent drives an application with a large live hierarchy. If every mutation returns the full tree (or
the agent pulls it before every decision) — the context bloats, latency grows, the agent reasons over
stale data. **TreeLens gives the agent a cheap, always-fresh view of the tree by syncing the server copy
with deltas instead of re-snapshotting the whole thing.**

## The idea

```
  Agent ──MCP──▶ MCP server + MIRROR ──▶ relay ──▶ plugin/adapter ──▶ Host
        ◀── deltas,          (Mirror               (authoritative
            query results)    = kernel)             source, diff+hash)
```

- **Single-writer / read-replica.** The host is the source of truth; the server holds an in-memory copy.
- **Diff-as-response.** Mutations return *what changed*, not full state. The server absorbs the delta and
  strips the heavy payload.
- **Navigate, don't dump.** The model reads the mirror with query tools (search/subtree/path/attributes),
  with no round-trip to the host.
- **Integrity + drift.** The tree hash is cross-checked host↔server; manual user edits are caught by the
  push-listener; desync → force rebuild.

Full reference design and vocabulary — **[docs/pattern.md](docs/pattern.md)**.

## Why a separate layer over MCP?

MCP gives an agent the transport and primitives to call an app — but it does **not** provide diff-sync,
versioned hierarchical state, or node-id reconciliation. Those stay application-level. TreeLens is exactly
that layer: it isn't displaced by the protocol, it closes the gap MCP leaves.

## See it run

**30 seconds, no install, no Photoshop.** The whole pattern runs against a toy in-memory host — the kernel
is zero-dependency and self-bootstraps `sys.path`, so a clone is enough:

```bash
python demo.py
```

Abridged real output — the pattern proving itself end-to-end:

```text
1) structural add — diff-as-response (a thin envelope, not the full tree)
   add 'Sky' -> {'treeChanges': [{'op': 'add', 'id': 2, 'type': 'PIXEL', 'parentId': 1, ...}],
                 'stateVersion': 5, 'response': {'createdId': 2}}

2) attr-mutation (rename) — a thin envelope with NO treeChanges (a pure attr edit)
   rename 'Sky' -> {'stateVersion': 8, 'response': None}

3) navigate the mirror — no host round-trip
   query('Sky') -> {'matches': [{'id': 2, 'name': 'Sky Gradient', ...}], 'matchCount': 1}

4) drift via the integrity hash — a SILENT external edit, caught on the next command
   add 'Title' -> {'treeChanges': [...], 'stateVersion': 12, 'driftRecovered': True, ...}

5) drift via the push-listener — a NOTIFIED external edit, resynced
   rename     -> {'stateVersion': 14, 'resyncedExternalEdit': True, 'response': None}
```

Diff-as-response, navigation over the mirror, and **both** drift-recovery paths — exercised without a host.
(Per-stage walkthrough + install/test commands are under [Running](#running).)

> **Two doors:** **run it** → `python demo.py` (above); **understand it** →
> **[docs/pattern.md](docs/pattern.md)**, the reference design and vocabulary.

## Who it's for

- You are building an MCP over a **design/3D/CAD/scene-graph application** with hundreds of nodes and long
  sessions.
- You have hit **context-bloat** from full trees in responses.
- You want a ready **skeleton** grounded in a production system (store + diff + hash + query + drift
  recovery) instead of rediscovering the architecture from scratch.

**Precondition:** a single active writer (the agent + rare manual edits, absorbed by a rebuild).
Realtime multiplayer (collaborative editing) is **outside the current scope, not designed**; details —
[docs/pattern.md](docs/pattern.md) §"Required precondition".

## What's here

The repo is both the **written pattern** (`docs/`) and a **runnable reference skeleton** (the code at the
root): the host-agnostic kernel + an abstract `HostAdapter` + a toy fake-host on which the whole flow runs.

| Where | About |
|---|---|
| **[docs/pattern.md](docs/pattern.md)** | Reference design: topology, vocabulary, components, command flow, rationale for hand-rolling. **Start here.** |
| **[docs/portability.md](docs/portability.md)** | Host-agnostic **kernel** vs host-specific **adapter**; the adapter contract; mapping onto Photoshop / Figma / Unity; the three linchpin axes (id-stability, execution model, event model). |
| **[docs/open-problems.md](docs/open-problems.md)** | An honest status of what is unfinished: **mutation batching**, **safe-wait / concurrency**, diff localization (Merkle), persistence, attrs cross-hash. Problem → current state → direction. |
| **[wire-protocol.md](wire-protocol.md)** | The normative adapter↔kernel contract (prose) + machine-readable `schema/` (JSON Schema, Draft 2020-12). |
| **`treelens/`** | The host-agnostic kernel: `mirror.py` (tree+attrs+meta+selection, atomic apply, query), `diff.py` (keyed reconciliation), `hashing.py` (integrity), `adapter.py` (the `HostAdapter` contract), `lens.py` (envelope ingest, drift detect/recover, payload strip). Zero runtime dependencies. |
| **`adapters/`** | `in_memory.py` — a toy host (runs without a real application); `photoshop.md` — an adapter implementation guide for Photoshop/UXP. |

## Running

From the repo root:

```bash
pip install -e ".[dev]"             # the kernel is zero-dep; dev-extra installs pytest + jsonschema
pytest tests/                       # kernel conformance + schema conformance
python demo.py                      # end-to-end demo on the toy host
node js/check_vectors.js            # cross-language hash parity (a second implementation)
```

The kernel also runs **without installation**: `python tests/test_conformance.py` and `python demo.py` put
the root on `sys.path` themselves. `tests/test_schema.py` requires `jsonschema` (dev-extra); without it the
suite is **cleanly skipped** (exit 0) — the kernel's zero-dep invariant — and the run prints a loud banner
flagging that the contract was not validated (`tests/conftest.py`).

> **A validating run = `pip install -e ".[dev]"`.** Without dev-extra a bare `pytest tests/` will show
> green, but the schema suite is **skipped — the schema contract is not checked**. The skip is **not
> silent**: pytest prints a loud banner at the end of the run flagging this. If you see `N passed, M
> skipped`, the schema was not validated. CI always installs dev-extra, so on every push and pull request
> the schema suite runs automatically as a hard gate.

`demo.py` shows the whole pattern without a real application:
1. **bootstrap + structural mutations** — diff-as-response, thin envelopes (`treeChanges` + `treeHash`);
2. **attr-mutation** (rename) — carries `attrChanges`, **without** `treeChanges`;
3. **navigation over the mirror** (`query` / `subtree` / `path`) — without a round-trip to the host;
4. **drift recovery via the integrity hash** — a "silent" external edit → `driftRecovered: True`;
5. **drift recovery via the push-listener** — a notified edit → `resyncedExternalEdit: True`.

## How to build your own adapter

Implement `treelens.adapter.HostAdapter` for your host (the full contract — [wire-protocol.md](wire-protocol.md) +
[docs/portability.md](docs/portability.md) §contract):

```python
from treelens import HostAdapter, compute_tree_diff, TreeLens

class MyHost(HostAdapter):
    def read_tree(self, scope_id):           # MANDATORY: return {id,type,children} (root sentinel)
        ...
    def read_attrs(self, scope_id, node_ids=None, fields=None):  # OPTIONAL: per-node properties
        ...
    def on_external_change(self, cb):        # OPTIONAL: call cb(scope_id) on a manual user edit
        ...
    # canonical_hash / transaction — have defaults; override for your runtime

lens = TreeLens(MyHost())
env = lens.ingest(my_mutation_returning_envelope())   # env — thin, for the model
lens.query("name regex")                              # navigation over the mirror
```

The minimum for correctness: **(A)** stable node ids, **(B)** `read_tree`, **(C)** mutations
returning a diff envelope (or full state + `returns_full_state=True` → the kernel diffs it itself),
**(D)** `canonical_hash` matching `tree_hash` byte-for-byte. The rest (attrs, events,
transaction scope) — graceful degradation.

Run `tests/test_conformance.py` — the **reference conformance suite for the kernel's key invariants**
(diff-roundtrip, atomic rollback, full-state path, cross-language hash), not an exhaustive correctness
proof. The schema suite validates the **shape** of envelopes/ops, **not the semantics** (id uniqueness,
absence of cycles, applicability of an op, op ordering, the `treeHash`↔`treeChanges` match — that is held
by the kernel, not by JSON Schema). Then write adapter-level tests on your host's real diffs.

## Origin and maturity

The pattern is **extracted from a working system**, not designed in a vacuum: a production Photoshop MCP
server, where the mirror runs on production PSDs (200–300 layers, long agentic sessions). We publish
honestly: the pattern's core is proven in production, this reference is an extraction of it validated by
the conformance suite (not the production binary), and several important pieces (batching, safe-wait) are
still in design — and that is **part of the value**: the list of what remains
([docs/open-problems.md](docs/open-problems.md)) is a ready roadmap for contributors. What exactly is proven
vs unfinished — the maturity table in [docs/pattern.md](docs/pattern.md) § "Component maturity".

The idea took shape while the author was working on a fork of
[mikechambers/adb-mcp](https://github.com/mikechambers/adb-mcp) (MIT). TreeLens shares **no code** with
adb-mcp — the pattern was distilled independently from the author's own work on top of that fork — but
credit goes to that project as where the idea began.

## License

MIT — see [LICENSE](LICENSE) and [NOTICE](NOTICE) (adb-mcp acknowledgment).

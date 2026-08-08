# Changelog

Format — [Keep a Changelog](https://keepachangelog.com/), versioning —
[SemVer](https://semver.org/).

## [0.1.0] — Unreleased

First public slice of the pattern: a host-agnostic kernel (extraction from a production Photoshop MCP
server, validated by the conformance suite) + an adapter contract + a reference skeleton.

### Added
- **Host-agnostic kernel** (`treelens/`): `Mirror` (tree + attrs + meta + selection, atomic apply with
  rollback), `compute_tree_diff` / `compute_attr_diff` (keyed reconciliation), `tree_hash` /
  `stable_serialize` / `sha256_hex` (integrity), `TreeLens` (envelope ingest, drift detection and
  recovery), `HostAdapter` (ABC — the seam with the host). Zero runtime dependencies.
- **Normative wire protocol** (`wire-protocol.md`) + **JSON Schema** (`schema/`, Draft 2020-12): the
  response envelope, the dictionaries of tree-/attr-/meta-/selection-ops, the node shape.
- **Conformance coverage for the `attrDelete` apply path** (delete an existing key, no-op on a
  missing key, no-op on a missing layer, mixed set+delete batch). The branch was previously
  uncovered: zeroing it out left the entire suite green, while `attrDelete` is a live op in the
  production adapter (effect removal, mask deletion). Field-reported by the Photoshop adopter.
- **Cross-language hash contract:** `tests/hash_vectors.json` (toy) + `tests/hash_vectors_realworld.json`
  (real PS trees up to 242 nodes, hashes from the production JS adapter). Parity is checked by a **live
  second implementation** — `js/canonical_hash.js` + `js/check_vectors.js` (Node), not Python-against-itself.
- **Conformance suite** (`tests/test_conformance.py`): keyed-diff roundtrip with deep-structural equality,
  atomic rollback, full-state fallback, drift-recovery (hash-mismatch + apply-failure), topmost-removal.
  Schema conformance (`tests/test_schema.py`) — the shape of envelopes/ops.
- **Toy adapter** (`adapters/in_memory.py`) + **end-to-end demo** (`demo.py`) + **a guide to implementing
  an adapter for Photoshop/UXP** (`adapters/photoshop.md`).
- **CI:** conformance + schema on Python 3.10–3.12 + a cross-language hash job (Node).
- **Adoption guidance** (`docs/portability.md` "Adopting the kernel: conform or reimplement"): the two
  honest adoption paths and the exact conformance surface path (b) imposes on the host emit, with the
  field-observed failure mode for each non-conformance. Distilled from the first adopter.

### Changed
- The meta/selection channel dispatchers now **reject unknown ops** instead of skipping them
  silently, and validate the **whole batch before applying any op** (all-or-nothing, per
  wire-protocol §9). A silently skipped op — or a partially applied batch — is a silently stale
  mirror on channels the integrity hash does not cover. The per-channel reaction to unknown ops is
  now normative in `wire-protocol.md` §6. Field-reported by the Photoshop adopter.

### Fixed
- The push-resync path (`resyncedExternalEdit`) **dropped the command's own `metaChanges` /
  `selectionChanges`** instead of applying them — and returned them unstripped. The wholesale rebuild
  reseeds tree+attrs only, so for the unhashed channels the envelope is the only source of truth:
  dropping it left quietly-stale meta/selection with a version counter that says fresh. Both channels
  now apply on the resync path too (normative in `wire-protocol.md` §8.3); found while auditing the
  kernel against the adoption-guidance table.
- `wire-protocol.md` prose caught up with the kernel and schema (which already agreed): §8 —
  `driftRecovered` is set by a failed incremental apply as well as by a hash mismatch; §9 — the strip
  list now names everything the kernel actually removes from the envelope (`attrChanges` /
  `metaChanges` / `selectionChanges` wholesale, not just the bootstrap payload). Prose-only.

### Known limitations
Roadmap of the unfinished — `open-problems.md`: mutation batching, safe-wait /
serialization of concurrent commands, diff localization (Merkle/LIS), per-layer attr hashes. The kernel
does not emit same-parent reorder (caught by the hash → rebuild) — a deliberate Stage-1 gap.

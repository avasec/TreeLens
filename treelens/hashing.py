# MIT License — TreeLens reference kernel.
"""Canonical serialization + integrity hashing (host-agnostic).

The cross-language determinism of `stable_serialize` is the genuinely hard part
of the pattern: the host (any language) and the server must produce *byte-
identical* serializations of the same tree, or the `tree_hash` drift-check
diverges spuriously.

This holds for the structural tree because a node is `{id, type, children}` —
fixed string keys + an ordered `children` list, no language-native maps whose
key ordering differs between runtimes. It does NOT hold for the attrs store
(an int-id-keyed map): JS sorts int keys lexicographically, Python numerically,
so the byte streams diverge. That is why TreeLens cross-checks ONLY the tree
hash and resyncs attrs wholesale on rebuild (see wire-protocol.md, and the
per-layer-hash fix in ../../open-problems.md §4).
"""

import hashlib
import json


def stable_serialize(obj) -> str:
    """Deterministic JSON: sorted keys, no whitespace, unicode preserved.

    MUST match the host adapter's serialization byte-for-byte for the hash
    cross-check to mean anything. Feed it ONLY pure-structure trees
    ({id,type,children}); never an int-keyed map (see module docstring).
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def tree_hash(tree: dict) -> str:
    """Root hash of a pure-structure tree. This is a depth-0 Merkle root: it
    answers only 'equal / not equal', so a mismatch forces a full rebuild.
    Localized (per-node Merkle) recovery is a documented next step — see
    ../../open-problems.md §3.
    """
    return sha256_hex(stable_serialize(tree))

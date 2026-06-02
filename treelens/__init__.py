# MIT License — TreeLens reference kernel (host-agnostic).
"""TreeLens kernel: a server-side, diff-synced mirror of a host's node hierarchy.

Public surface:
    HostAdapter   — implement this for your host (the only host-specific code)
    TreeLens      — drift orchestration: ingest envelopes, keep the mirror synced
    Mirror        — the mirrored state (tree + attrs + meta + selection), atomic apply
    compute_tree_diff / compute_attr_diff — keyed reconciliation (pure)
    tree_hash / stable_serialize / sha256_hex — integrity hashing

See ../README.md for the kernel/adapter split and ../wire-protocol.md for the
wire contract between them.
"""

from .adapter import HostAdapter
from .diff import compute_attr_diff, compute_tree_diff
from .hashing import sha256_hex, stable_serialize, tree_hash
from .lens import TreeLens
from .mirror import Mirror

__all__ = [
    "HostAdapter",
    "TreeLens",
    "Mirror",
    "compute_tree_diff",
    "compute_attr_diff",
    "tree_hash",
    "stable_serialize",
    "sha256_hex",
]

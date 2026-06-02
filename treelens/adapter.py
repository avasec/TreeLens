# MIT License — TreeLens reference kernel.
"""HostAdapter — the seam between the host-agnostic kernel and a concrete host.

This abstract base IS the core artifact of the pattern: implement it for your
host (Photoshop / Figma / Unity / your app) and the kernel (store + diff + hash
+ query + drift recovery) works unchanged. The contract is described in prose in
../../portability.md "Host-adapter contract"; this file is its executable form.

MANDATORY  : read_tree (B), canonical_hash (D, default provided),
             and a mutation path that yields response envelopes carrying diffs
             (C — lives in your adapter's command handlers, not on this ABC).
OPTIONAL   : read_attrs (E), on_external_change (F), transaction (G).

Returning full state instead of diffs is allowed: set `returns_full_state=True`
and the kernel will diff for you (it just costs a full structural read per
mutation instead of a small delta). See treelens.lens.TreeLens.
"""

from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

from .hashing import tree_hash


class HostAdapter(ABC):
    # If True, mutation envelopes may omit `treeChanges` and instead ship the
    # full post-state in the `treeAfter` field (a root sentinel); the kernel
    # diffs it against the current mirror (see treelens.lens.TreeLens + wire-
    # protocol.md §2). Default: the adapter ships diffs itself (token-cheap).
    returns_full_state: bool = False

    # ── MANDATORY ─────────────────────────────────────────────────────────────
    @abstractmethod
    def read_tree(self, scope_id: Any) -> dict:
        """Return the full hierarchy for `scope_id` as a root sentinel:
        `{"id": None, "type": "ROOT", "children": [ {id,type,children}, ... ]}`.

        PURE STRUCTURE only — no name/visible/etc. (those go through read_attrs).
        This is the bootstrap + drift-recovery primitive: the kernel calls it on
        first sight of a scope and whenever the integrity hash mismatches.
        """
        raise NotImplementedError

    # ── MANDATORY (default impl provided) ──────────────────────────────────────
    def canonical_hash(self, tree: dict) -> str:
        """Root hash over the canonical serialization of a pure-structure tree.
        MUST match treelens.hashing.tree_hash byte-for-byte. Override only if your
        host computes the hash in another language/runtime — then make the two
        serializations identical (this is the genuinely hard part).
        """
        return tree_hash(tree)

    # ── OPTIONAL (tiered disclosure) ────────────────────────────────────────────
    def read_attrs(self, scope_id: Any, node_ids: Optional[list] = None,
                   fields: Optional[list] = None) -> dict:
        """Return `{node_id: {field: value}}`. `node_ids=None` => all nodes;
        `fields=None` => all tracked fields. Default: no attrs (structure-only
        mirror). Override to enable property/effects/etc. tiers.
        """
        return {}

    # ── OPTIONAL (drift detection for out-of-band edits) ───────────────────────
    def on_external_change(self, callback: Callable[[Any], None]) -> None:
        """Register `callback(scope_id)` to fire when the host changed OUTSIDE
        the agent's commands (a human edited the document). The kernel uses it to
        mark the mirror stale and force a rebuild on the next command. Without
        this, the only safety net is the integrity hash on the next mutation.

        Note: a host typically does NOT notify on the plugin's own mutations, so
        any received event ⇒ an external edit. Default: no-op (no push signal).
        """
        return None

    # ── OPTIONAL (execution scope) ─────────────────────────────────────────────
    def transaction(self, fn: Callable[[], Any]) -> Any:
        """Run `fn` inside the host's required execution scope (Photoshop
        executeAsModal, Unity main-thread, ...) and ideally as one undo step.
        Default: run directly. This never touches the kernel — it is purely how
        YOUR adapter must wrap mutations.
        """
        return fn()

# MIT License — TreeLens reference kernel.
"""TreeLens — drift orchestration over a Mirror + a HostAdapter.

Generalized form of the origin production system's sync core: ingest a host RESPONSE ENVELOPE,
keep the mirror in sync, detect drift via the integrity hash, recover via the
adapter, annotate a version, and STRIP the bulky payload before the envelope
goes back to the model.

Envelope shape (normative: ../wire-protocol.md):
    {status, response, scopeId,
     treeChanges?, treeHash?, attrChanges?, metaChanges?, selectionChanges?,
     message?}
The kernel adds `stateVersion` (and `resyncedExternalEdit` when a push signal
forced a rebuild).
"""

from typing import Any, Optional

from .adapter import HostAdapter
from .diff import compute_tree_diff
from .mirror import Mirror


class TreeLens:
    def __init__(self, adapter: HostAdapter, mirror: Optional[Mirror] = None) -> None:
        self.adapter = adapter
        self.mirror = mirror or Mirror()
        self._active_scope: Any = None
        self._dirty: set = set()  # scopes a push signal marked stale (adapter F)
        # Wire the optional external-change push signal.
        self.adapter.on_external_change(self._mark_dirty)

    # ── public query surface (defaults to the active scope) ───────────────────
    @property
    def active_scope(self) -> Any:
        return self._active_scope

    def query(self, name_pattern, type_filter=None, limit=None, scope=None):
        return self.mirror.query(scope or self._active_scope, name_pattern, type_filter, limit)

    def subtree(self, node_id, depth=1, scope=None):
        return self.mirror.subtree(scope or self._active_scope, node_id, depth)

    def path(self, node_id, scope=None):
        return self.mirror.path(scope or self._active_scope, node_id)

    # ── ingest ────────────────────────────────────────────────────────────────
    def ingest(self, env: dict) -> dict:
        """Apply a host response envelope to the mirror and return it thinned."""
        if env.get("status") == "FAILURE":
            return env  # mirror untouched; error propagates as-is

        scope = env.get("scopeId")
        if scope is not None:
            self._active_scope = scope

        # (F) Push-detected external edit since the last command: supersede this
        # command's incremental delta with a full rebuild of current host state
        # (which already reflects both the user's edit and this command).
        if scope is not None and scope in self._dirty:
            self._force_rebuild(scope)
            self._dirty.discard(scope)
            # The rebuild reseeds tree+attrs only (the ABC has no meta/selection
            # reads) — for those channels the command's own envelope is the only
            # source of truth, so they are applied on top, not superseded.
            self._apply_meta_channel(scope, env)
            self._apply_selection_channel(scope, env)
            self._strip(env)
            env["stateVersion"] = self.mirror.version(scope)
            env["resyncedExternalEdit"] = True
            return env

        # tree channel
        tree_changes = env.get("treeChanges")
        if scope is not None and tree_changes is None and self.adapter.returns_full_state \
                and env.get("treeAfter") is not None:
            # Host returned full state instead of a delta — diff it here.
            before = self.mirror.get_tree(scope) or {"id": None, "type": "ROOT", "children": []}
            tree_changes = compute_tree_diff(before, env["treeAfter"])
            env["treeHash"] = self.adapter.canonical_hash(env["treeAfter"])
            env.pop("treeAfter", None)

        if scope is not None and tree_changes is not None:
            is_rebuild = len(tree_changes) == 1 and tree_changes[0].get("op") == "rebuild"
            if self.mirror.get_tree(scope) is None and not is_rebuild:
                # Never seen this scope and the delta is incremental — bootstrap
                # by full read (apply_tree_diff would KeyError otherwise).
                self._force_rebuild(scope)
            else:
                try:
                    self.mirror.apply_tree_diff(scope, tree_changes)
                except Exception:
                    # A malformed / internally-inconsistent incremental delta must
                    # not crash ingest: the host is authoritative, so recover the
                    # mirror wholesale from a fresh read rather than propagate.
                    self._force_rebuild(scope)
                    env["driftRecovered"] = True

            remote = env.get("treeHash")
            if remote and self.mirror.hash(scope) != remote:
                # Drift: incremental apply diverged from the host's truth.
                self._force_rebuild(scope)
                env["driftRecovered"] = True

            env["stateVersion"] = self.mirror.version(scope)
            for op in tree_changes:
                if op.get("op") == "rebuild":
                    op.pop("tree", None)  # strip bulky payload

        # attr channel
        attr_changes = env.get("attrChanges")
        if scope is not None and attr_changes is not None:
            is_attrs_rebuild = len(attr_changes) == 1 and attr_changes[0].get("op") == "attrsRebuild"
            if is_attrs_rebuild or self.mirror.has_attrs(scope):
                self.mirror.apply_attr_diff(scope, attr_changes)
            # else: scoped delta with no attrs in the mirror yet — next rebuild reseeds.
            env["stateVersion"] = self.mirror.version(scope)
            env.pop("attrChanges", None)

        # meta / selection channels (wholesale)
        if scope is not None:
            self._apply_meta_channel(scope, env)
            self._apply_selection_channel(scope, env)

        return env

    # ── internals ──────────────────────────────────────────────────────────────
    def _apply_meta_channel(self, scope, env: dict) -> None:
        """Apply + consume `metaChanges`. Runs on BOTH ingest paths (normal and
        push-resync): recovery cannot reseed meta (no read in the ABC), so the
        envelope is the only source of truth for this channel.

        Unknown ops are rejected, matching the op-vocabulary strictness of
        Mirror's tree/attr dispatchers (at the ingest level the tree channel
        instead recovers via rebuild + driftRecovered; meta and selection have
        no hash to catch drift, so an error is the only honest signal).
        Validation runs over the WHOLE batch before any op is applied: a
        partially applied batch on an unhashed channel is exactly the
        quietly-stale mirror this check exists to prevent (wire-protocol.md §9
        — atomic apply)."""
        meta_changes = env.get("metaChanges")
        if meta_changes is None:
            return
        for op in meta_changes:
            if op.get("op") != "metaRebuild":
                raise ValueError(f"ingest: unknown meta op {op.get('op')!r}")
        for op in meta_changes:
            self.mirror.set_meta(scope, op["meta"])
        env["stateVersion"] = self.mirror.version(scope)
        env.pop("metaChanges", None)

    def _apply_selection_channel(self, scope, env: dict) -> None:
        """Same contract as the meta channel: reject unknown ops, validate the
        whole batch before applying any of it (all-or-nothing)."""
        selection_changes = env.get("selectionChanges")
        if selection_changes is None:
            return
        for op in selection_changes:
            if op.get("op") != "selectionSet":
                raise ValueError(
                    f"ingest: unknown selection op {op.get('op')!r}"
                )
        for op in selection_changes:
            self.mirror.set_selection(scope, op["selection"])
        env["stateVersion"] = self.mirror.version(scope)
        env.pop("selectionChanges", None)

    def _mark_dirty(self, scope_id) -> None:
        self._dirty.add(scope_id)

    def _force_rebuild(self, scope) -> None:
        """Pull a fresh tree (+ attrs if the adapter provides them) and replace."""
        self.mirror.rebuild(scope, self.adapter.read_tree(scope))
        attrs = self.adapter.read_attrs(scope)
        if attrs:
            self.mirror.rebuild_attrs(scope, attrs)

    @staticmethod
    def _strip(env: dict) -> None:
        for op in env.get("treeChanges") or []:
            if op.get("op") == "rebuild":
                op.pop("tree", None)
        env.pop("attrChanges", None)
        env.pop("treeAfter", None)

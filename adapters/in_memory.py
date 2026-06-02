# MIT License — TreeLens reference adapter (toy host).
"""InMemoryHost — a fake host so the kernel runs with ZERO real app installed.

Holds an authoritative in-memory tree + attrs. Agent mutations return response
envelopes (with computed diffs + hash); "external" edits mutate the authoritative
state to simulate a human editing the document — silently (caught by the
integrity hash on the next command) or with a push notification (the F channel).

This is the minimal reference adapter — the shape every real adapter
(Photoshop/Figma/Unity) mirrors.
"""

import copy
from typing import Any, Callable, Optional

from treelens import HostAdapter, compute_attr_diff, compute_tree_diff


class InMemoryHost(HostAdapter):
    def __init__(self, scope_id: str = "doc-1") -> None:
        self.scope_id = scope_id
        self._root = {"id": None, "type": "ROOT", "children": []}
        self._attrs: dict[Any, dict] = {}
        self._counter = 0
        self._on_change: Optional[Callable[[Any], None]] = None

    # ── HostAdapter contract ──────────────────────────────────────────────────
    def read_tree(self, scope_id: Any) -> dict:
        return self._pure(self._root)

    def read_attrs(self, scope_id: Any, node_ids=None, fields=None) -> dict:
        ids = node_ids if node_ids is not None else list(self._attrs)
        out = {}
        for nid in ids:
            a = self._attrs.get(nid)
            if a is None:
                continue
            out[nid] = {k: v for k, v in a.items() if fields is None or k in fields}
        return copy.deepcopy(out)

    def on_external_change(self, callback: Callable[[Any], None]) -> None:
        self._on_change = callback

    # ── agent mutations → response envelopes ──────────────────────────────────
    def add_node(self, parent_id, node_type: str, name: str) -> dict:
        before = self._pure(self._root)
        nid = self._new_id()
        node = {"id": nid, "type": node_type, "children": []}
        self._parent(parent_id)["children"].append(node)
        self._attrs[nid] = {"name": name, "visible": True}
        after = self._pure(self._root)
        return self._env(
            treeChanges=compute_tree_diff(before, after),
            treeHash=self.canonical_hash(after),
            attrChanges=compute_attr_diff({}, {nid: self._attrs[nid]}),  # structural seed
            response={"createdId": nid},
        )

    def remove_node(self, node_id) -> dict:
        before = self._pure(self._root)
        node, parent = self._find(node_id)
        parent["children"] = [c for c in parent["children"] if c["id"] != node_id]
        self._attrs.pop(node_id, None)
        after = self._pure(self._root)
        return self._env(treeChanges=compute_tree_diff(before, after), treeHash=self.canonical_hash(after))

    def move_node(self, node_id, new_parent_id, index: int = 0) -> dict:
        before = self._pure(self._root)
        node, parent = self._find(node_id)
        parent["children"] = [c for c in parent["children"] if c["id"] != node_id]
        self._parent(new_parent_id)["children"].insert(index, node)
        after = self._pure(self._root)
        return self._env(treeChanges=compute_tree_diff(before, after), treeHash=self.canonical_hash(after))

    def rename_node(self, node_id, name: str) -> dict:
        before = {node_id: dict(self._attrs.get(node_id, {}))}
        self._attrs.setdefault(node_id, {})["name"] = name
        after = {node_id: dict(self._attrs[node_id])}
        return self._env(attrChanges=compute_attr_diff(before, after))  # attr-mutation: no treeChanges

    # ── "external" (out-of-band) edits — simulate a human ─────────────────────
    def external_add_silent(self, parent_id, node_type: str, name: str):
        """Edit the authoritative tree WITHOUT emitting an envelope and WITHOUT a
        push signal. The mirror is now stale; the integrity hash on the next
        agent command catches it → forced rebuild (the D channel)."""
        nid = self._new_id()
        self._parent(parent_id)["children"].append({"id": nid, "type": node_type, "children": []})
        self._attrs[nid] = {"name": name, "visible": True}
        return nid

    def external_add_notified(self, parent_id, node_type: str, name: str):
        """Same, but fire the change callback — the F (push-listener) channel.
        The mirror marks the scope dirty and rebuilds on the next command."""
        nid = self.external_add_silent(parent_id, node_type, name)
        if self._on_change is not None:
            self._on_change(self.scope_id)
        return nid

    # ── internals ──────────────────────────────────────────────────────────────
    def _new_id(self) -> int:
        self._counter += 1
        return self._counter

    def _pure(self, node) -> dict:
        return {"id": node["id"], "type": node["type"],
                "children": [self._pure(c) for c in node["children"]]}

    def _parent(self, parent_id) -> dict:
        return self._root if parent_id is None else self._find(parent_id)[0]

    def _find(self, node_id):
        def walk(node):
            for child in node["children"]:
                if child["id"] == node_id:
                    return child, node
                got = walk(child)
                if got is not None:
                    return got
            return None
        found = walk(self._root)
        if found is None:
            raise KeyError(f"InMemoryHost: node {node_id} not found")
        return found

    def _env(self, **channels) -> dict:
        env = {"status": "SUCCESS", "scopeId": self.scope_id, "response": None}
        env.update(channels)
        return env

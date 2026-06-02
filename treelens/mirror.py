# MIT License — TreeLens reference kernel.
"""Mirror — server-side replica of a host's node hierarchy (host-agnostic).

Per scope (a document / scene / file id) holds four sub-stores, separated by how
they change:

    tree       {id, type, children}      pure structure, incremental ops
    attrs      {node_id: {field: ...}}   per-node properties, scoped deltas
    meta       {field: ...}              flat header, wholesale replace
    selection  {...}                     small state, wholesale replace

Nothing here names a concrete host. The seam to Photoshop/Figma/Unity/... is
`treelens.adapter.HostAdapter`, orchestrated by `treelens.lens.TreeLens`.

KEY DESIGN POINT: the tree is PURE STRUCTURE. name/visible/opacity/bounds/...
live in `attrs`, not in tree nodes — so a rename is an attr delta, not a tree
op, and the tree serializes cross-language-deterministically (no int-keyed maps;
see hashing.py). Query resolves name/visible from attrs.

The root sentinel kept per scope has id=None and holds top-level nodes in
`children`; it is never returned to callers.
"""

import copy
import re
from typing import Any, Optional

from .hashing import tree_hash


class Mirror:
    def __init__(self) -> None:
        self._trees: dict[Any, dict] = {}
        self._index: dict[Any, dict[Any, dict]] = {}
        self._attrs: dict[Any, dict[Any, dict]] = {}
        self._meta: dict[Any, dict] = {}
        self._selection: dict[Any, dict] = {}
        self._versions: dict[Any, int] = {}

    # ── versioning ──────────────────────────────────────────────────────────
    def _bump(self, scope) -> int:
        self._versions[scope] = self._versions.get(scope, 0) + 1
        return self._versions[scope]

    def version(self, scope) -> int:
        return self._versions.get(scope, 0)

    # ── tree ────────────────────────────────────────────────────────────────
    def get_tree(self, scope) -> Optional[dict]:
        return self._trees.get(scope)

    def rebuild(self, scope, tree: dict) -> int:
        """Replace the tree for `scope` wholesale (bootstrap / drift recovery)."""
        self._trees[scope] = copy.deepcopy(tree)
        self._index[scope] = self._build_index(self._trees[scope])
        return self._bump(scope)

    def hash(self, scope) -> str:
        tree = self._trees.get(scope)
        return tree_hash(tree) if tree is not None else ""

    def apply_tree_diff(self, scope, ops: list[dict]) -> int:
        """Apply tree ops atomically. On any op failure: restore + re-raise.

        A single `[{op:"rebuild", tree}]` short-circuits to wholesale replace.
        """
        if len(ops) == 1 and ops[0].get("op") == "rebuild":
            tree = ops[0].get("tree")
            if tree is None:
                raise ValueError("apply_tree_diff: rebuild op missing 'tree'")
            return self.rebuild(scope, tree)
        if scope not in self._trees:
            raise KeyError(f"apply_tree_diff: no tree for scope={scope!r}; rebuild required")
        backup = copy.deepcopy(self._trees[scope])
        try:
            for op in ops:
                self._apply_tree_op(scope, op)
        except Exception:
            self._trees[scope] = backup
            self._index[scope] = self._build_index(backup)
            raise
        return self._bump(scope)

    def _apply_tree_op(self, scope, op: dict) -> None:
        kind = op.get("op")
        if kind == "add":
            node = {"id": op["id"], "type": op["type"], "children": op.get("children") or []}
            parent = self._parent_or_root(scope, op.get("parentId"))
            parent.setdefault("children", []).insert(op.get("index", 0), node)
            self._index_subtree(scope, node)
        elif kind == "remove":
            self._remove(scope, op["id"])
        elif kind == "move":
            self._move(scope, op["id"], op.get("toParent"), op.get("newIndex", 0))
        elif kind == "typeChange":
            node = self._index[scope].get(op["id"])
            if node is None:
                raise KeyError(f"typeChange: node {op['id']} not in mirror")
            if node["type"] != op["from"]:
                raise ValueError(
                    f"typeChange: node {op['id']} type mismatch "
                    f"(mirror {node['type']!r} vs op from {op['from']!r}) — drift"
                )
            node["type"] = op["to"]
        else:
            raise ValueError(f"_apply_tree_op: unknown op {kind!r}")

    def _remove(self, scope, node_id) -> None:
        if node_id not in self._index[scope]:
            raise KeyError(f"remove: node {node_id} not in mirror")
        parent = self._find_parent(scope, node_id)
        if parent is None:
            raise KeyError(f"remove: parent of {node_id} not found")
        removed = next((c for c in parent.get("children") or [] if c.get("id") == node_id), None)
        parent["children"] = [c for c in parent.get("children") or [] if c.get("id") != node_id]
        if removed is not None:
            self._unindex_subtree(scope, removed)

    def _move(self, scope, node_id, to_parent, new_index) -> None:
        node = self._index[scope].get(node_id)
        if node is None:
            raise KeyError(f"move: node {node_id} not in mirror")
        old_parent = self._find_parent(scope, node_id)
        if old_parent is None:
            raise KeyError(f"move: parent of {node_id} not found")
        old_parent["children"] = [c for c in old_parent.get("children") or [] if c.get("id") != node_id]
        # Re-insert by reference (not copied) — _index entries stay valid.
        self._parent_or_root(scope, to_parent).setdefault("children", []).insert(new_index, node)

    # ── attrs ───────────────────────────────────────────────────────────────
    def has_attrs(self, scope) -> bool:
        return scope in self._attrs

    def rebuild_attrs(self, scope, attrs: dict) -> int:
        # Coerce top-level keys: a JSON round-trip widens int ids to str.
        self._attrs[scope] = {self._coerce(k): copy.deepcopy(v) for k, v in attrs.items()}
        return self._bump(scope)

    def get_attrs(self, scope, node_id) -> Optional[dict]:
        store = self._attrs.get(scope)
        if store is None:
            return None
        a = store.get(node_id)
        return copy.deepcopy(a) if a is not None else None

    def apply_attr_diff(self, scope, ops: list[dict]) -> int:
        if len(ops) == 1 and ops[0].get("op") == "attrsRebuild":
            attributes = ops[0].get("attributes")
            if attributes is None:
                raise ValueError("apply_attr_diff: attrsRebuild op missing 'attributes'")
            return self.rebuild_attrs(scope, attributes)
        if scope not in self._attrs:
            raise KeyError(f"apply_attr_diff: no attrs for scope={scope!r}; attrsRebuild required")
        backup = copy.deepcopy(self._attrs[scope])
        try:
            for op in ops:
                self._apply_attr_op(scope, op)
        except Exception:
            self._attrs[scope] = backup
            raise
        return self._bump(scope)

    def _apply_attr_op(self, scope, op: dict) -> None:
        kind = op.get("op")
        nid = self._coerce(op["id"])
        if kind == "attrSet":
            self._attrs[scope].setdefault(nid, {})[op["key"]] = op["value"]
        elif kind == "attrDelete":
            attrs = self._attrs[scope].get(nid)
            if attrs is not None:
                attrs.pop(op["key"], None)
        else:
            raise ValueError(f"_apply_attr_op: unknown op {kind!r}")

    # ── meta / selection (wholesale) ──────────────────────────────────────────
    def set_meta(self, scope, meta: dict) -> int:
        self._meta[scope] = copy.deepcopy(meta)
        return self._bump(scope)

    def get_meta(self, scope) -> Optional[dict]:
        m = self._meta.get(scope)
        return copy.deepcopy(m) if m is not None else None

    def set_selection(self, scope, selection: dict) -> int:
        self._selection[scope] = copy.deepcopy(selection)
        return self._bump(scope)

    def get_selection(self, scope) -> Optional[dict]:
        s = self._selection.get(scope)
        return copy.deepcopy(s) if s is not None else None

    # ── query (reads from the mirror — no host round-trip) ────────────────────
    def query(self, scope, name_pattern: str, type_filter: Optional[str] = None,
              limit: Optional[int] = None) -> dict:
        root = self._trees.get(scope)
        if root is None:
            return {"matches": [], "matchCount": 0, "truncated": False}
        rx = re.compile(name_pattern)
        attrs = self._attrs.get(scope) or {}

        def name_of(node) -> str:
            return (attrs.get(node.get("id")) or {}).get("name", "")

        results: list[dict] = []

        def walk(node, parents) -> None:
            for child in node.get("children") or []:
                nm = name_of(child)
                if rx.search(nm) and (type_filter is None or child.get("type") == type_filter):
                    results.append({
                        "id": child.get("id"), "name": nm, "type": child.get("type"),
                        "parentPath": [{"id": p.get("id"), "name": name_of(p)} for p in parents],
                    })
                walk(child, parents + [child])

        walk(root, [])
        total = len(results)
        truncated = False
        if limit and limit > 0 and total > limit:
            results, truncated = results[:limit], True
        return {"matches": results, "matchCount": total, "truncated": truncated}

    def subtree(self, scope, node_id, depth: int = 1) -> Optional[dict]:
        idx = self._index.get(scope)
        if idx is None:
            return None
        node = idx.get(node_id)
        if node is None:
            return None
        attrs = self._attrs.get(scope) or {}

        def trim(n, d) -> dict:
            a = attrs.get(n.get("id")) or {}
            out = {"id": n.get("id"), "type": n.get("type"),
                   "name": a.get("name", ""), "visible": a.get("visible", True)}
            out["children"] = [trim(c, d - 1) for c in n.get("children") or []] if d > 0 else []
            return out

        return trim(node, depth)

    def path(self, scope, node_id) -> Optional[list[dict]]:
        root = self._trees.get(scope)
        if root is None:
            return None
        attrs = self._attrs.get(scope) or {}

        def name_of(node) -> str:
            return (attrs.get(node.get("id")) or {}).get("name", "")

        def find(node, acc):
            for child in node.get("children") or []:
                nxt = acc + [{"id": child.get("id"), "name": name_of(child)}]
                if child.get("id") == node_id:
                    return nxt
                got = find(child, nxt)
                if got is not None:
                    return got
            return None

        return find(root, [])

    # ── index helpers ─────────────────────────────────────────────────────────
    def _build_index(self, tree: dict) -> dict[Any, dict]:
        index: dict[Any, dict] = {}

        def walk(node) -> None:
            nid = node.get("id")
            if nid is not None:
                index[nid] = node
            for child in node.get("children") or []:
                walk(child)

        walk(tree)
        return index

    def _index_subtree(self, scope, node) -> None:
        nid = node.get("id")
        if nid is not None:
            self._index[scope][nid] = node
        for child in node.get("children") or []:
            self._index_subtree(scope, child)

    def _unindex_subtree(self, scope, node) -> None:
        nid = node.get("id")
        if nid is not None:
            self._index[scope].pop(nid, None)
        for child in node.get("children") or []:
            self._unindex_subtree(scope, child)

    def _parent_or_root(self, scope, parent_id) -> dict:
        if parent_id is None:
            return self._trees[scope]
        parent = self._index[scope].get(parent_id)
        if parent is None:
            raise KeyError(f"parent {parent_id} not in mirror")
        return parent

    def _find_parent(self, scope, node_id) -> Optional[dict]:
        def walk(node):
            for child in node.get("children") or []:
                if child.get("id") == node_id:
                    return node
                got = walk(child)
                if got is not None:
                    return got
            return None

        return walk(self._trees[scope])

    @staticmethod
    def _coerce(key):
        """Normalize a node id that may have been widened to str by JSON."""
        if isinstance(key, str) and key.lstrip("-").isdigit():
            return int(key)
        return key

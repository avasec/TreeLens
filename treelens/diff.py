# MIT License — TreeLens reference kernel.
"""Diff computation (keyed tree reconciliation) — pure, host-agnostic.

Either side can run this: an adapter computes the diff host-side so the wire
payload is already a delta (the choice the origin production system made), OR a
host that can only return full state lets the kernel diff here. Both paths feed
the same op vocabulary that `Mirror.apply_*_diff` consumes.

OP VOCABULARY (also normative in ../wire-protocol.md)
  tree:  {op:"add",        id, type, parentId, index, children?}
         {op:"remove",     id}
         {op:"move",       id, toParent, newIndex}
         {op:"typeChange", id, from, to}
         {op:"rebuild",    tree}                         # wholesale replacement
  attr:  {op:"attrSet",    id, key, value}
         {op:"attrDelete", id, key}
         {op:"attrsRebuild", attributes}                 # wholesale replacement

INVARIANT: nodes are matched strictly by stable `id`, never by position.
Positional matching mis-diffs [A,B,C]->[B,C,D] as three replaces instead of
remove(A)+add(D). The whole design rests on id stability within a session.

STAGE-1 LIMITATION (deliberate, as in the origin production system): same-parent reorder (children
reordered without a parent change) is NOT emitted here. It is caught by the
`tree_hash` drift-check and recovered via rebuild. Closing it with an LIS-based
reorder op is a documented next step — ../../open-problems.md §3 (A1).
"""

from typing import Any


def _index_with_parents(root: dict) -> dict[Any, dict]:
    """Flatten a root-sentinel tree to id -> {node, parent, index, type}.

    The root sentinel (id=None) is not indexed; its children carry parent=None
    (top-level). Pure structure expected: {id, type, children}.
    """
    out: dict[Any, dict] = {}

    def rec(node: dict, parent_id) -> None:
        for i, child in enumerate(node.get("children") or []):
            cid = child.get("id")
            out[cid] = {
                "node": child,
                "parent": parent_id,
                "index": i,
                "type": child.get("type"),
            }
            rec(child, cid)

    rec(root, None)
    return out


def _pure(node: dict) -> dict:
    """Strip a subtree to pure structure {id, type, children} for an `add` op."""
    return {
        "id": node.get("id"),
        "type": node.get("type"),
        "children": [_pure(c) for c in node.get("children") or []],
    }


def compute_tree_diff(before_root: dict, after_root: dict) -> list[dict]:
    before = _index_with_parents(before_root)
    after = _index_with_parents(after_root)
    ops: list[dict] = []

    # Removed: present before, gone after. Emit `remove` only for the TOPMOST
    # removed nodes (parent not itself removed) — symmetric with `add` below.
    # apply_tree_diff un-indexes descendants recursively, so emitting a remove
    # for a descendant of an already-removed parent would KeyError on the second
    # op. (Invariant shared with the production adapter's tree_diff.)
    removed = {nid for nid in before if nid not in after}
    for nid in before:
        if nid in removed and before[nid]["parent"] not in removed:
            ops.append({"op": "remove", "id": nid})

    added = {nid for nid in after if nid not in before}
    # Emit `add` only for the TOPMOST added nodes (parent not itself added) and
    # carry the full new subtree inline, so descendants are not double-emitted.
    for nid, info in after.items():
        if nid in added and info["parent"] not in added:
            ops.append(
                {
                    "op": "add",
                    "id": nid,
                    "type": info["type"],
                    "parentId": info["parent"],
                    "index": info["index"],
                    "children": _pure(info["node"]).get("children") or [],
                }
            )

    # Common nodes: type change and/or parent change (cross-parent move).
    for nid, a in after.items():
        if nid in before:
            b = before[nid]
            if b["type"] != a["type"]:
                ops.append(
                    {"op": "typeChange", "id": nid, "from": b["type"], "to": a["type"]}
                )
            if b["parent"] != a["parent"]:
                ops.append(
                    {"op": "move", "id": nid, "toParent": a["parent"], "newIndex": a["index"]}
                )
            # same-parent reorder (b["parent"]==a["parent"], index differs) is
            # intentionally NOT emitted — see module docstring (Stage-1 limit).

    return ops


def compute_attr_diff(before: dict, after: dict) -> list[dict]:
    """Diff two `{node_id: {field: value}}` maps into attrSet/attrDelete ops.

    Node-level removals are handled by the tree `remove` op (which drops the
    node's attrs); this only diffs fields of nodes present in `after`.
    """
    ops: list[dict] = []
    for nid, a_attrs in after.items():
        b_attrs = before.get(nid, {})
        for key, value in a_attrs.items():
            if key not in b_attrs or b_attrs[key] != value:
                ops.append({"op": "attrSet", "id": nid, "key": key, "value": value})
        for key in b_attrs:
            if key not in a_attrs:
                ops.append({"op": "attrDelete", "id": nid, "key": key})
    return ops

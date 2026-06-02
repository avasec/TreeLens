# MIT License — TreeLens reference conformance tests.
"""Host-agnostic conformance suite for the kernel.

This is the trust anchor for reuse: it exercises
the kernel with NO host — pure diff sequences in, asserted query results + hash
out — plus the full-state-fallback path and the cross-language hash vectors.

    python tests/test_conformance.py      (from the reference/ directory, or any)
    # or: pytest tests/                    (functions are test_*-named)
"""

import json
import pathlib
import sys

# Put reference/ on the import path so `treelens` resolves regardless of cwd /
# whether we are run directly or collected by pytest (the script's own dir,
# tests/, is what Python adds by default — not reference/).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from treelens import HostAdapter, TreeLens, Mirror, compute_tree_diff, tree_hash  # noqa: E402

SCOPE = "s"


def _node(nid, ntype, children=None):
    return {"id": nid, "type": ntype, "children": children or []}


def _root(children):
    return {"id": None, "type": "ROOT", "children": children}


def _struct(node):
    """Normalize to pure structure {id, type, children} for deep-equality —
    independent of the hash, so a symmetric compute/apply bug can't hide."""
    return {
        "id": node.get("id"),
        "type": node.get("type"),
        "children": [_struct(c) for c in node.get("children") or []],
    }


def test_keyed_diff_roundtrip():
    """compute_tree_diff(before, after) applied to a `before` mirror yields a
    mirror byte-identical (by hash) to `after` — for add/remove/cross-parent
    move/typeChange. (Same-parent reorder is a documented Stage-1 gap.)"""
    before = _root([_node(1, "GROUP", [_node(2, "PIXEL")]), _node(3, "PIXEL")])
    after = _root([_node(1, "GROUP", [_node(4, "PIXEL")]), _node(2, "TEXT")])
    #   removed 3 · added 4 under 1 · moved 2 to root · typeChange 2 PIXEL->TEXT

    ops = compute_tree_diff(before, after)
    # Pin op TARGETS, not just kinds — a kinds-only check passes even if an op
    # points at the wrong node. (Structural deep-equality below is the final
    # oracle; this localizes a diff regression to the offending op.)
    by_kind = {op["op"]: op for op in ops}
    assert sorted(by_kind) == ["add", "move", "remove", "typeChange"], [op["op"] for op in ops]
    assert by_kind["remove"]["id"] == 3, by_kind["remove"]
    assert by_kind["add"]["id"] == 4 and by_kind["add"]["parentId"] == 1, by_kind["add"]
    assert by_kind["move"]["id"] == 2 and by_kind["move"]["toParent"] is None, by_kind["move"]
    assert by_kind["typeChange"]["id"] == 2 and by_kind["typeChange"]["to"] == "TEXT", by_kind["typeChange"]

    m = Mirror()
    m.rebuild(SCOPE, before)
    m.apply_tree_diff(SCOPE, ops)
    assert m.hash(SCOPE) == tree_hash(after), "mirror diverged from `after` (hash)"
    # Deep structural equality — NOT just the hash. The hash compares kernel-to-
    # kernel; a symmetric bug in compute+apply could pass it. This pins the
    # actual tree shape against `after`.
    assert _struct(m.get_tree(SCOPE)) == _struct(after), "mirror diverged from `after` (structure)"


def test_subtree_removal():
    """Removing a non-empty group emits ONE topmost `remove`, not one per node.
    Regression for the extraction bug where compute_tree_diff emitted a remove
    for every disappeared id — the second crashed apply (KeyError) because the
    parent's removal had already un-indexed the descendant."""
    before = _root([_node(1, "GROUP", [_node(2, "PIXEL"), _node(3, "TEXT")]), _node(4, "PIXEL")])
    after = _root([_node(4, "PIXEL")])  # whole group 1 (with 2,3) gone

    ops = compute_tree_diff(before, after)
    removes = [op["id"] for op in ops if op["op"] == "remove"]
    assert removes == [1], f"expected only topmost remove [1], got {removes}"

    m = Mirror()
    m.rebuild(SCOPE, before)
    m.apply_tree_diff(SCOPE, ops)  # must NOT raise KeyError on descendants
    assert _struct(m.get_tree(SCOPE)) == _struct(after)
    assert m.hash(SCOPE) == tree_hash(after)

    # Edge case: remove EVERYTHING (root → empty) — the original repro.
    m2 = Mirror()
    m2.rebuild(SCOPE, _root([_node(1, "GROUP", [_node(2, "PIXEL")])]))
    m2.apply_tree_diff(SCOPE, compute_tree_diff(_root([_node(1, "GROUP", [_node(2, "PIXEL")])]), _root([])))
    assert _struct(m2.get_tree(SCOPE)) == _struct(_root([]))


def test_atomic_rollback():
    """A failing op in the batch restores the pre-batch state (tree + index)."""
    before = _root([_node(1, "GROUP", [_node(2, "PIXEL")])])
    m = Mirror()
    m.rebuild(SCOPE, before)
    h0, v0 = m.hash(SCOPE), m.version(SCOPE)

    raised = False
    try:
        m.apply_tree_diff(SCOPE, [
            {"op": "remove", "id": 2},          # valid
            {"op": "remove", "id": 999},        # invalid -> raises, rolls back
        ])
    except KeyError:
        raised = True
    assert raised, "expected KeyError on unknown node"
    assert m.hash(SCOPE) == h0, "rollback did not restore the tree"
    assert m.version(SCOPE) == v0, "version bumped on a failed batch"
    assert m.subtree(SCOPE, 2) is not None, "rolled-back node missing from index"


def test_attr_diff_and_query():
    """attrs feed name resolution; query/subtree/path read from the mirror."""
    tree = _root([_node(1, "GROUP", [_node(2, "PIXEL"), _node(3, "TEXT")])])
    m = Mirror()
    m.rebuild(SCOPE, tree)
    m.rebuild_attrs(SCOPE, {
        1: {"name": "Group", "visible": True},
        2: {"name": "Sky", "visible": True},
        3: {"name": "Title", "visible": False},
    })

    res = m.query(SCOPE, "Sky")
    assert res["matchCount"] == 1 and res["matches"][0]["id"] == 2, res
    assert [p["name"] for p in res["matches"][0]["parentPath"]] == ["Group"], res

    # attr-mutation: rename via a scoped delta. ID-pinned, not count-only — else
    # applying the set to the wrong node still yields one "Gradient" match.
    m.apply_attr_diff(SCOPE, [{"op": "attrSet", "id": 2, "key": "name", "value": "Sky Gradient"}])
    renamed = m.query(SCOPE, "Gradient")
    assert renamed["matchCount"] == 1 and renamed["matches"][0]["id"] == 2, renamed

    sub = m.subtree(SCOPE, 1, depth=1)
    assert {c["id"] for c in sub["children"]} == {2, 3}
    assert [p["id"] for p in m.path(SCOPE, 3)] == [1, 3]

    # type_filter + result cap
    assert m.query(SCOPE, ".", type_filter="TEXT")["matchCount"] == 1
    capped = m.query(SCOPE, ".", limit=1)
    assert capped["truncated"] is True and len(capped["matches"]) == 1


class _FullStateHost(HostAdapter):
    """Adapter that ships full post-state via `treeAfter` (returns_full_state)."""

    returns_full_state = True

    def __init__(self):
        self._tree = {"id": None, "type": "ROOT", "children": []}

    def read_tree(self, scope_id):
        import copy
        return copy.deepcopy(self._tree)

    def snapshot(self):
        import copy
        return copy.deepcopy(self._tree)


def test_full_state_fallback():
    """returns_full_state + `treeAfter` -> kernel diffs against the mirror.

    Covers the easy on-ramp path (wire-protocol.md §2) that has no diff code on
    the host side. First ingest bootstraps; the second exercises the real
    full-state -> compute_tree_diff -> apply path on a seeded mirror.
    """
    host = _FullStateHost()
    m = TreeLens(host)

    host._tree["children"].append({"id": 1, "type": "PIXEL", "children": []})
    e1 = m.ingest({"status": "SUCCESS", "scopeId": SCOPE, "treeAfter": host.snapshot()})
    assert [c["id"] for c in m.mirror.get_tree(SCOPE)["children"]] == [1]
    assert "treeAfter" not in e1, "treeAfter must be stripped from the model-facing envelope"

    host._tree["children"].append({"id": 2, "type": "TEXT", "children": []})
    e2 = m.ingest({"status": "SUCCESS", "scopeId": SCOPE, "treeAfter": host.snapshot()})
    assert {c["id"] for c in m.mirror.get_tree(SCOPE)["children"]} == {1, 2}
    assert "driftRecovered" not in e2, "clean full-state apply should not trigger drift recovery"


def test_full_state_fallback_subtree_removal():
    """Removing a non-empty group via the full-state path must NOT crash ingest,
    and the mirror must reflect the removal. The topmost-remove invariant itself
    is pinned by test_subtree_removal; THIS is the end-to-end path test (was the
    happy-path repro of the bug — kernel computes the diff internally here)."""
    host = _FullStateHost()
    m = TreeLens(host)
    host._tree["children"].append(
        {"id": 1, "type": "GROUP", "children": [{"id": 2, "type": "PIXEL", "children": []}]}
    )
    m.ingest({"status": "SUCCESS", "scopeId": SCOPE, "treeAfter": host.snapshot()})  # bootstrap
    assert [c["id"] for c in m.mirror.get_tree(SCOPE)["children"]] == [1]

    host._tree["children"] = []  # delete the whole group (with its child)
    m.ingest({"status": "SUCCESS", "scopeId": SCOPE, "treeAfter": host.snapshot()})  # must not raise
    assert m.mirror.get_tree(SCOPE)["children"] == [], "group removal did not apply"


def test_hash_vectors():
    """Determinism / regression pin for the kernel's own canonical hash, and the
    CONTRACT fixture other-language adapters must hit (wire-protocol.md §7).

    NOTE: this Python check alone is not cross-language evidence — both sides run
    the Python kernel, so it only proves the kernel is stable against its frozen
    output. The LIVE cross-language gate is js/check_vectors.js (an independent JS
    implementation reproducing these same vectors, run in CI); the realworld
    vectors additionally carry sha256 originally emitted by the production JS adapter."""
    path = pathlib.Path(__file__).resolve().parent / "hash_vectors.json"
    vectors = json.loads(path.read_text(encoding="utf-8"))
    assert vectors, "hash_vectors.json is empty"
    for v in vectors:
        assert tree_hash(v["tree"]) == v["sha256"], f"vector mismatch for {v['tree']}"


def test_hash_vectors_realworld():
    """Real Photoshop tree structures captured from a production session (incl.
    a 242-node document). The sha256 of each was emitted by the production
    server's JS adapter; the kernel must reproduce it byte-for-byte — proving
    cross-language canonical-serialization parity on real-world, deep trees,
    not just hand-written toy cases. See adapters/photoshop.md."""
    path = pathlib.Path(__file__).resolve().parent / "hash_vectors_realworld.json"
    vectors = json.loads(path.read_text(encoding="utf-8"))
    assert vectors, "hash_vectors_realworld.json is empty"
    for v in vectors:
        assert tree_hash(v["tree"]) == v["sha256"], f"realworld vector mismatch: {v['label']}"


def test_ingest_recovers_from_bad_apply():
    """A malformed-but-recoverable incremental delta must NOT crash ingest: the
    host is authoritative, so the kernel recovers wholesale via rebuild."""
    host = _FullStateHost()
    host._tree["children"].append({"id": 1, "type": "PIXEL", "children": []})
    m = TreeLens(host)
    m.ingest({"status": "SUCCESS", "scopeId": SCOPE,
              "treeChanges": [{"op": "rebuild", "tree": host.snapshot()}],
              "treeHash": tree_hash(host.snapshot())})
    assert [c["id"] for c in m.mirror.get_tree(SCOPE)["children"]] == [1]

    # remove of a node that isn't in the mirror -> apply raises -> recover
    e = m.ingest({"status": "SUCCESS", "scopeId": SCOPE,
                  "treeChanges": [{"op": "remove", "id": 999}],
                  "treeHash": tree_hash(host.snapshot())})
    assert e.get("driftRecovered") is True, "bad apply should recover via rebuild, not raise"
    assert [c["id"] for c in m.mirror.get_tree(SCOPE)["children"]] == [1], "mirror diverged from host"


def test_ingest_recovers_from_hash_mismatch():
    """Delta applies CLEANLY but its treeHash disagrees with the post-apply state
    → kernel force-rebuilds from the host, OVERRIDING the bad incremental result.
    Distinct from the apply-exception path above: here apply never raises, the
    integrity hash is what catches the divergence (lens.py drift branch)."""
    host = _FullStateHost()
    host._tree["children"].append({"id": 1, "type": "PIXEL", "children": []})
    m = TreeLens(host)
    m.ingest({"status": "SUCCESS", "scopeId": SCOPE,
              "treeChanges": [{"op": "rebuild", "tree": host.snapshot()}],
              "treeHash": tree_hash(host.snapshot())})

    # Host truth gains node 2; the delta adds a DIFFERENT node (99) with a wrong
    # hash. Clean apply -> mirror {1,99}; hash mismatch -> rebuild from host {1,2}.
    host._tree["children"].append({"id": 2, "type": "TEXT", "children": []})
    e = m.ingest({"status": "SUCCESS", "scopeId": SCOPE,
                  "treeChanges": [{"op": "add", "id": 99, "type": "PIXEL", "parentId": None, "index": 1, "children": []}],
                  "treeHash": "0" * 64})
    assert e.get("driftRecovered") is True, "hash mismatch should force a rebuild"
    assert {c["id"] for c in m.mirror.get_tree(SCOPE)["children"]} == {1, 2}, \
        "recovery must restore host truth {1,2}, not the bad apply {1,99}"


def test_nested_add_collapses():
    """An added subtree is ONE topmost `add` carrying nested children inline."""
    before = _root([])
    after = _root([_node(1, "GROUP", [_node(2, "PIXEL"), _node(3, "GROUP", [_node(4, "TEXT")])])])
    ops = compute_tree_diff(before, after)
    assert [o["op"] for o in ops] == ["add"] and ops[0]["id"] == 1, ops
    m = Mirror(); m.rebuild(SCOPE, before); m.apply_tree_diff(SCOPE, ops)
    assert _struct(m.get_tree(SCOPE)) == _struct(after)


def test_cross_parent_move_with_index():
    """Cross-parent move lands at the right parent and index."""
    before = _root([_node(1, "GROUP", [_node(2, "PIXEL")]), _node(3, "GROUP", [_node(4, "TEXT"), _node(5, "TEXT")])])
    after = _root([_node(1, "GROUP", [_node(2, "PIXEL"), _node(5, "TEXT")]), _node(3, "GROUP", [_node(4, "TEXT")])])
    ops = compute_tree_diff(before, after)
    assert any(o["op"] == "move" and o["id"] == 5 and o["toParent"] == 1 for o in ops), ops
    m = Mirror(); m.rebuild(SCOPE, before); m.apply_tree_diff(SCOPE, ops)
    assert _struct(m.get_tree(SCOPE)) == _struct(after)


def test_noop_diff_empty():
    """Identical before/after yields no ops, and applying an empty batch leaves
    the TREE unchanged. version still advances — it's a monotonic correlation
    token (NOT a change counter), so an empty apply is not version-inert; this
    pins that, rather than implying an invariance the kernel doesn't hold."""
    t = _root([_node(1, "GROUP", [_node(2, "PIXEL")])])
    assert compute_tree_diff(t, t) == []
    m = Mirror(); m.rebuild(SCOPE, t); v0 = m.version(SCOPE)
    m.apply_tree_diff(SCOPE, [])
    assert _struct(m.get_tree(SCOPE)) == _struct(t), "empty apply changed the tree"
    assert m.version(SCOPE) > v0, "version is monotonic (correlation token, not a change counter)"


def test_deep_tree_roundtrip():
    """A deeply nested chain diffs + applies without depth issues."""
    def chain(n):
        node = _node(n, "GROUP")
        cur = node
        for i in range(1, 20):
            child = _node(n * 100 + i, "GROUP" if i % 2 else "PIXEL")
            cur["children"] = [child]; cur = child
        return node
    before = _root([chain(1)])
    after = _root([chain(1), _node(2, "PIXEL")])  # add a sibling at root
    ops = compute_tree_diff(before, after)
    m = Mirror(); m.rebuild(SCOPE, before); m.apply_tree_diff(SCOPE, ops)
    assert _struct(m.get_tree(SCOPE)) == _struct(after)
    assert m.hash(SCOPE) == tree_hash(after)


def test_full_state_uses_diff_not_rebuild():
    """The full-state path must DIFF (compute_tree_diff), not silently rebuild.
    Pins the mechanism, not just the outcome."""
    import treelens.lens as lensmod
    host = _FullStateHost()
    host._tree["children"].append({"id": 1, "type": "PIXEL", "children": []})
    m = TreeLens(host)
    m.ingest({"status": "SUCCESS", "scopeId": SCOPE, "treeAfter": host.snapshot()})  # bootstrap

    calls = []
    orig = lensmod.compute_tree_diff
    lensmod.compute_tree_diff = lambda b, a: (calls.append(1), orig(b, a))[1]
    try:
        host._tree["children"].append({"id": 2, "type": "TEXT", "children": []})
        m.ingest({"status": "SUCCESS", "scopeId": SCOPE, "treeAfter": host.snapshot()})
    finally:
        lensmod.compute_tree_diff = orig
    assert calls, "full-state ingest did not call compute_tree_diff (silent rebuild?)"
    assert {c["id"] for c in m.mirror.get_tree(SCOPE)["children"]} == {1, 2}


def test_diff_realworld_roundtrip():
    """Diff BETWEEN real Photoshop trees (from hash_vectors_realworld) round-trips:
    compute_tree_diff(A, B) applied to a mirror of A reaches B exactly, and the
    resulting hash equals B's sha256 — which was emitted by the production JS
    adapter. Pins the diff algorithm on real, deep shapes (incl. bulk removal of a
    242-node document → 18 nodes) against a cross-language hash anchor — the
    realworld-scale stress the toy fixtures don't cover."""
    path = pathlib.Path(__file__).resolve().parent / "hash_vectors_realworld.json"
    vecs = json.loads(path.read_text(encoding="utf-8"))
    for a in vecs:
        for b in vecs:
            ops = compute_tree_diff(a["tree"], b["tree"])
            m = Mirror()
            m.rebuild(SCOPE, a["tree"])
            m.apply_tree_diff(SCOPE, ops)
            label = f"{a['label']} -> {b['label']}"
            assert _struct(m.get_tree(SCOPE)) == _struct(b["tree"]), f"{label}: structure"
            assert m.hash(SCOPE) == b["sha256"], f"{label}: hash (JS-anchored)"


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} conformance tests passed.")


if __name__ == "__main__":
    _run()

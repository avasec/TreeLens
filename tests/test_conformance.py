# MIT License — TreeLens reference conformance tests.
"""Host-agnostic conformance suite for the kernel.

This is the trust anchor for reuse: it exercises
the kernel with NO host — pure diff sequences in, asserted query results + hash
out — plus the full-state-fallback path and the cross-language hash vectors.

    python tests/test_conformance.py      (from the repo root, or any cwd)
    # or: pytest tests/                    (functions are test_*-named)
"""

import json
import pathlib
import sys

# Put the repo root on the import path so `treelens` resolves regardless of cwd /
# whether we are run directly or collected by pytest (the script's own dir,
# tests/, is what Python adds by default — not the repo root).
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


def test_attr_delete_removes_the_field_and_tolerates_misses():
    """The apply side of `attrDelete` — the op a host emits when a field stops
    existing (an effect cleared, a mask removed).

    Deleting must actually delete: a no-op implementation leaves the mirror
    claiming a property the host no longer has, and nothing downstream can tell,
    because attrs are not covered by the integrity hash. The two miss cases are
    part of the contract too — a delta may reference a node or key the mirror
    never had, and that must not abort the batch.
    """
    m = Mirror()
    m.rebuild(SCOPE, _root([_node(1, "PIXEL")]))
    m.rebuild_attrs(SCOPE, {1: {"name": "Sky", "visible": True}})

    m.apply_attr_diff(SCOPE, [{"op": "attrDelete", "id": 1, "key": "visible"}])
    assert m.get_attrs(SCOPE, 1) == {"name": "Sky"}, m.get_attrs(SCOPE, 1)

    # missing key: no-op, and the rest of the node survives untouched
    m.apply_attr_diff(SCOPE, [{"op": "attrDelete", "id": 1, "key": "visible"}])
    assert m.get_attrs(SCOPE, 1) == {"name": "Sky"}

    # missing node: no-op, and no phantom entry is created for it
    m.apply_attr_diff(SCOPE, [{"op": "attrDelete", "id": 999, "key": "name"}])
    assert m.get_attrs(SCOPE, 999) is None
    assert m.get_attrs(SCOPE, 1) == {"name": "Sky"}

    # a delete riding alongside a set in one batch applies both
    m.apply_attr_diff(SCOPE, [
        {"op": "attrSet", "id": 1, "key": "opacity", "value": 50},
        {"op": "attrDelete", "id": 1, "key": "name"},
    ])
    assert m.get_attrs(SCOPE, 1) == {"opacity": 50}


def test_unknown_op_in_mixed_batch_applies_nothing():
    """A batch [valid, bogus] must be all-or-nothing (wire-protocol §9): the
    valid op must NOT be applied — and the version must not move — before the
    bogus one is rejected. A partially applied batch on an unhashed channel is
    exactly the quietly-stale mirror the unknown-op rejection exists to prevent."""
    lens = TreeLens(_FullStateHost())
    lens.ingest({"status": "SUCCESS", "scopeId": SCOPE,
                 "metaChanges": [{"op": "metaRebuild", "meta": {"width": 10}}]})
    version_before = lens.mirror.version(SCOPE)
    try:
        lens.ingest({"status": "SUCCESS", "scopeId": SCOPE, "metaChanges": [
            {"op": "metaRebuild", "meta": {"width": 99}},   # valid — must not land
            {"op": "metaSet", "meta": {"width": 7}},         # bogus — rejects the batch
        ]})
    except ValueError:
        pass
    else:
        raise AssertionError("mixed batch with a bogus op was accepted")
    assert lens.mirror.get_meta(SCOPE) == {"width": 10}, "valid op leaked from a rejected batch"
    assert lens.mirror.version(SCOPE) == version_before, "version moved on a rejected batch"


def test_unknown_meta_or_selection_op_is_rejected():
    """Every channel fails loudly on an op it does not know.

    The tree and attr dispatchers already raise; meta and selection used to skip
    silently, which is worse than a crash: those two channels are NOT covered by
    the integrity hash, so a typo'd op name would leave the mirror serving a
    stale document header (or marquee) with a version counter that says fresh.
    """
    lens = TreeLens(_FullStateHost())
    lens.ingest({"status": "SUCCESS", "scopeId": SCOPE,
                 "metaChanges": [{"op": "metaRebuild", "meta": {"width": 10}}]})

    for channel, op, label in (
        ("metaChanges", {"op": "metaSet", "meta": {"width": 20}}, "unknown meta op"),
        ("selectionChanges", {"op": "selectionToggle"}, "unknown selection op"),
    ):
        try:
            lens.ingest({"status": "SUCCESS", "scopeId": SCOPE, channel: [op]})
        except ValueError as exc:
            assert label in str(exc), f"wrong error for {channel}: {exc}"
        else:
            raise AssertionError(f"{channel} silently accepted {op['op']!r}")

    assert lens.mirror.get_meta(SCOPE) == {"width": 10}, "rejected op must not apply"
    assert lens.mirror.get_selection(SCOPE) is None


class _PushHost(HostAdapter):
    """Adapter with a push signal (`on_external_change` — F in the ABC)."""

    def __init__(self):
        self._cb = None
        self._tree = {"id": None, "type": "ROOT",
                      "children": [{"id": 1, "type": "PIXEL", "children": []}]}

    def read_tree(self, scope_id):
        import copy
        return copy.deepcopy(self._tree)

    def on_external_change(self, cb):
        self._cb = cb

    def push(self, scope):
        self._cb(scope)


def test_external_resync_still_applies_meta_and_selection():
    """A command landing on a push-dirtied scope is superseded by a full rebuild
    — but the rebuild reseeds tree+attrs ONLY (the ABC has no meta/selection
    reads), so the command's own metaChanges/selectionChanges are the freshest
    truth for those channels and must still be applied (wire-protocol §8.3).
    Dropping them leaves quietly-stale meta on an unhashed channel; returning
    them unstripped leaks the payload past the §9 strip."""
    host = _PushHost()
    lens = TreeLens(host)
    lens.ingest({"status": "SUCCESS", "scopeId": SCOPE,
                 "metaChanges": [{"op": "metaRebuild", "meta": {"width": 100}}],
                 "selectionChanges": [{"op": "selectionSet", "selection": [1]}]})

    host.push(SCOPE)  # the user edited the host outside the agent
    env = lens.ingest({"status": "SUCCESS", "scopeId": SCOPE,
                       "metaChanges": [{"op": "metaRebuild", "meta": {"width": 999}}],
                       "selectionChanges": [{"op": "selectionSet", "selection": [2]}]})
    assert env.get("resyncedExternalEdit") is True
    assert lens.mirror.get_meta(SCOPE) == {"width": 999}, "meta lost on the resync path"
    assert lens.mirror.get_selection(SCOPE) == [2], "selection lost on the resync path"
    assert "metaChanges" not in env and "selectionChanges" not in env, \
        "payload leaked past the strip on the resync path"


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


def _seed_all_channels(mirror, scope, width):
    """Populate all four sub-stores of `scope` — what eviction has to clear."""
    mirror.rebuild(scope, _root([_node(1, "GROUP", [_node(2, "PIXEL")])]))
    mirror.rebuild_attrs(scope, {1: {"name": "g"}, 2: {"name": "px"}})
    mirror.set_meta(scope, {"width": width})
    mirror.set_selection(scope, {"active": True, "bounds": [0, 0, width, width]})


def test_forget_evicts_every_channel_of_one_scope_only():
    """`forget(scope)` drops tree, attrs, meta, selection AND the version — and
    touches no other scope.

    Eviction exists because a mirror that keeps answering for a closed document
    lies in the one way nothing downstream can catch: the state looks live. Each
    channel is asserted separately because they are separate stores — clearing
    the tree alone still leaves `get_meta` serving a dead document's header. The
    second scope pins that eviction is scoped, not a disguised reset.
    """
    m = Mirror()
    _seed_all_channels(m, SCOPE, 100)
    _seed_all_channels(m, "other", 200)
    version_before = m.version(SCOPE)
    assert version_before > 0 and m.get_tree(SCOPE) is not None, "fixture did not seed"

    assert m.forget(SCOPE) is True, "forget of a mirrored scope must report it was known"

    assert m.get_tree(SCOPE) is None, "tree survived eviction"
    assert m.has_attrs(SCOPE) is False, "attrs store survived eviction"
    assert m.get_attrs(SCOPE, 1) is None, "attrs survived eviction"
    assert m.get_meta(SCOPE) is None, "meta survived eviction"
    assert m.get_selection(SCOPE) is None, "selection survived eviction"
    assert m.version(SCOPE) == 0, "version survived eviction — a re-seen id would inherit it"
    assert m.hash(SCOPE) == "", "hash still computed for an evicted scope"
    assert m.subtree(SCOPE, 1) is None, "node index survived eviction"
    assert m.path(SCOPE, 2) is None, "path resolved in an evicted scope"
    assert m.query(SCOPE, ".*") == {"matches": [], "matchCount": 0, "truncated": False}

    # The neighbour is untouched — including its version counter.
    assert _struct(m.get_tree("other")) == _struct(
        _root([_node(1, "GROUP", [_node(2, "PIXEL")])])
    ), "eviction of one scope damaged another"
    assert m.get_attrs("other", 2) == {"name": "px"}
    assert m.get_meta("other") == {"width": 200}
    assert m.get_selection("other") == {"active": True, "bounds": [0, 0, 200, 200]}
    assert m.version("other") == version_before, "neighbour's version moved"

    assert m.forget(SCOPE) is False, "forget of an unknown scope must report False"
    assert m.forget("never-seen") is False, "forget must not raise on an unknown scope"


def _scope_keyed_stores(obj, scope):
    """Names of `obj`'s containers that still key `scope`.

    Both dicts and sets: the lens's push-dirty state is a SET, so a dict-only
    sweep would miss exactly the shape that leaks (a set of scopes waiting for
    a rebuild).
    """
    return [name for name, store in vars(obj).items()
            if isinstance(store, (dict, set)) and scope in store]


def test_forget_leaves_no_scope_keyed_store_behind():
    """No per-scope store outlives `forget` — including one added later.

    The behavioural test above can only check the channels that exist today; a
    sub-store added tomorrow and missed in `_scope_stores` would keep answering
    for a dead scope with the whole suite green. This sweeps the instance
    instead, so that bug goes red here.
    """
    m = Mirror()
    _seed_all_channels(m, SCOPE, 100)
    populated = _scope_keyed_stores(m, SCOPE)
    assert len(populated) >= 6, f"fixture seeded too few stores: {populated}"

    m.forget(SCOPE)

    assert _scope_keyed_stores(m, SCOPE) == [], "stores survived eviction"


def test_forget_reports_known_even_when_a_stored_value_is_none():
    """`forget` detects a scope by membership, not by the stored value.

    Today every public mutator bumps the version store, so a pop-based
    "known" check happens to work; the seed below plants a None payload
    WITHOUT a version bump (the same future-mutator bug the store sweep
    above guards against, simulated the same way — via the instance) so
    this test fails on value-based detection rather than relying on that
    side effect.
    """
    m = Mirror()
    m._meta[SCOPE] = None  # deliberate: no _bump, see docstring

    assert m.forget(SCOPE) is True, "a None-valued store must still count as known"
    assert m.forget(SCOPE) is False, "second forget of the same scope must be a no-op"


def test_lens_forget_evicts_lens_level_scope_state_too():
    """`TreeLens.forget` covers what the mirror cannot see: the push-dirty set
    and the active scope.

    A scope id left in the dirty set outlives the document it named. Hosts hand
    ids back, and the next one to be handed this id would ingest through the
    external-edit resync branch — reported as `resyncedExternalEdit` when it is
    really a fresh scope's bootstrap, and skipping the hash check bootstrap
    does. A dead scope left as active is worse still: it is the fallback every
    scope-defaulting query resolves to.
    """
    host = _PushHost()
    lens = TreeLens(host)
    lens.ingest({"status": "SUCCESS", "scopeId": SCOPE,
                 "treeChanges": [{"op": "rebuild", "tree": _root([_node(7, "PIXEL")])}]})
    host.push(SCOPE)  # the user edited outside the agent — scope is now dirty
    assert lens.active_scope == SCOPE and SCOPE in lens._dirty, "fixture did not seed"

    assert lens.forget(SCOPE) is True, "lens.forget must report the mirror held the scope"

    assert lens.mirror.get_tree(SCOPE) is None, "lens.forget did not evict the mirror"
    assert SCOPE not in lens._dirty, "dirty mark outlived the scope it named"
    assert lens.active_scope is None, "a dead scope is still the active one"
    assert _scope_keyed_stores(lens, SCOPE) == [], "lens-level state survived eviction"

    # The next ingest for a re-handed id is a clean bootstrap, not a resync.
    env = lens.ingest({"status": "SUCCESS", "scopeId": SCOPE,
                       "treeChanges": [{"op": "add", "id": 9, "type": "PIXEL",
                                        "parentId": None, "index": 0}]})
    assert "resyncedExternalEdit" not in env, \
        "a re-seen id was reported as an external-edit resync"
    assert _struct(lens.mirror.get_tree(SCOPE)) == _struct(_root([_node(1, "PIXEL")])), \
        "post-eviction delta did not bootstrap from the host"

    assert lens.forget("never-seen") is False, "forget of an unknown scope must not raise"


def test_lens_forget_keeps_a_foreground_scope_active():
    """Evicting a background scope must not blank the active one.

    Closing a document the caller was not looking at is routine; if it cleared
    the active scope anyway, every scope-defaulting query would start answering
    for nothing until something re-set it.
    """
    lens = TreeLens(_PushHost())
    lens.ingest({"status": "SUCCESS", "scopeId": "background",
                 "treeChanges": [{"op": "rebuild", "tree": _root([])}]})
    lens.ingest({"status": "SUCCESS", "scopeId": SCOPE,
                 "treeChanges": [{"op": "rebuild", "tree": _root([_node(7, "PIXEL")])}]})
    assert lens.active_scope == SCOPE

    lens.forget("background")

    assert lens.active_scope == SCOPE, "evicting a background scope blanked the active one"
    assert lens.mirror.get_tree(SCOPE) is not None, "the active scope's mirror was damaged"


def test_forget_then_incremental_delta_bootstraps_instead_of_failing():
    """After eviction an incremental delta for the same id rebuilds from the
    host (§8.1) rather than extending a corpse or crashing ingest.

    This is the production close-then-reopen path: the mirror forgets a closed
    document, the host later hands the id back, and the first delta must land on
    freshly read state. Asserted through the mirror's contents (the host tree),
    not just "no exception".
    """
    host = _PushHost()
    lens = TreeLens(host)
    lens.ingest({"status": "SUCCESS", "scopeId": SCOPE,
                 "treeChanges": [{"op": "rebuild", "tree": _root([_node(7, "PIXEL")])}]})
    assert lens.mirror.get_tree(SCOPE) is not None

    assert lens.mirror.forget(SCOPE) is True

    lens.ingest({"status": "SUCCESS", "scopeId": SCOPE,
                 "treeChanges": [{"op": "add", "id": 9, "type": "PIXEL", "parentId": None,
                                  "index": 0}]})
    # _PushHost.read_tree returns a single node id=1 — proof the state came from
    # the host read, not from the pre-eviction mirror (id=7) or the delta (id=9).
    assert _struct(lens.mirror.get_tree(SCOPE)) == _struct(_root([_node(1, "PIXEL")])), \
        "post-eviction delta did not bootstrap from the host"
    assert lens.mirror.version(SCOPE) > 0, "bootstrapped scope has no version"


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} conformance tests passed.")


if __name__ == "__main__":
    _run()

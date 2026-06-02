# MIT License — TreeLens reference schema-conformance tests.
"""Validate the wire protocol's JSON Schema (../schema/) against reality.

Two trust anchors for adapter authors:
  1. Envelopes a real adapter emits (driven through the toy InMemoryHost) MUST
     validate against envelope.schema.json.
  2. The op examples from wire-protocol.md §4-§6 MUST validate against their op
     schemas, and malformed envelopes MUST be REJECTED (so the schema is a real
     gate, not a rubber stamp).

`jsonschema` is an OPTIONAL dependency: the kernel itself is zero-dependency, so
if the validator is absent this suite SKIPS (exit 0). CI installs `.[dev]` and
runs it as a hard gate.

    python tests/test_schema.py          (from reference/, or any cwd)
    # or: pytest tests/
"""

import json
import pathlib
import sys

REF_DIR = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_DIR = REF_DIR / "schema"
sys.path.insert(0, str(REF_DIR))

try:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    _HAVE_JSONSCHEMA = True
except ImportError:  # zero-dep kernel: skip cleanly when the validator is absent
    _HAVE_JSONSCHEMA = False

# Skip the whole module under pytest when the optional validator is missing
# (the test bodies reference jsonschema names). Direct-run skip is in _run().
try:
    import pytest
    pytestmark = pytest.mark.skipif(not _HAVE_JSONSCHEMA, reason="jsonschema not installed")
except ImportError:
    pass


def _load_schemas():
    """Load every *.schema.json keyed by its $id."""
    out = {}
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        out[schema["$id"]] = schema
    return out


def _validator(schema_id):
    """Validator for one schema, with all schemas registered under their $id so
    relative $refs ("tree-op.schema.json", "node.schema.json#/$defs/node")
    resolve against the referring schema's base URI."""
    schemas = _load_schemas()
    registry = Registry().with_resources(
        [(sid, Resource.from_contents(s)) for sid, s in schemas.items()]
    )
    return Draft202012Validator(schemas[schema_id], registry=registry)


ENVELOPE_ID = "https://treelens.dev/schema/envelope.schema.json"
TREE_OP_ID = "https://treelens.dev/schema/tree-op.schema.json"
ATTR_OP_ID = "https://treelens.dev/schema/attr-op.schema.json"
META_SEL_OP_ID = "https://treelens.dev/schema/meta-selection-op.schema.json"


def test_toy_host_envelopes_validate():
    """Every envelope the reference adapter emits is schema-valid."""
    from adapters.in_memory import InMemoryHost

    v = _validator(ENVELOPE_ID)
    host = InMemoryHost("doc-1")

    envelopes = [
        host.add_node(None, "GROUP", "Background"),
        host.add_node(1, "PIXEL", "Sky"),
        host.move_node(2, None, 0),
        host.rename_node(2, "Sky Gradient"),  # attr-mutation: attrChanges only
        host.remove_node(2),
    ]
    for env in envelopes:
        errors = sorted(v.iter_errors(env), key=str)
        assert not errors, f"envelope rejected: {env}\n  {[e.message for e in errors]}"


def test_op_examples_validate():
    """The op vocabularies from wire-protocol.md §4-§6 validate."""
    tree_v = _validator(TREE_OP_ID)
    for op in [
        {"op": "add", "id": 5, "type": "TEXT", "parentId": None, "index": 1, "children": []},
        {"op": "remove", "id": 3},
        {"op": "move", "id": 2, "toParent": None, "newIndex": 0},
        {"op": "typeChange", "id": 2, "from": "PIXEL", "to": "TEXT"},
        {"op": "rebuild", "tree": {"id": None, "type": "ROOT", "children": []}},
    ]:
        assert tree_v.is_valid(op), f"tree-op rejected: {op}"

    attr_v = _validator(ATTR_OP_ID)
    for op in [
        {"op": "attrSet", "id": 2, "key": "name", "value": "Sky"},
        {"op": "attrDelete", "id": 2, "key": "name"},
        {"op": "attrsRebuild", "attributes": {"2": {"name": "Sky", "visible": True}}},
    ]:
        assert attr_v.is_valid(op), f"attr-op rejected: {op}"

    ms_v = _validator(META_SEL_OP_ID)
    for op in [
        {"op": "metaRebuild", "meta": {"width": 800, "height": 600, "saved": True}},
        {"op": "selectionSet", "selection": {"active": True, "bounds": [0, 0, 10, 10]}},
    ]:
        assert ms_v.is_valid(op), f"meta/selection-op rejected: {op}"


def test_malformed_envelopes_rejected():
    """The schema is a real gate: these MUST fail (else it rubber-stamps)."""
    v = _validator(ENVELOPE_ID)
    bad = {
        "FAILURE without message": {"status": "FAILURE"},
        "treeChanges without treeHash": {"status": "SUCCESS", "scopeId": "s", "treeChanges": []},
        "mutation without scopeId": {
            "status": "SUCCESS",
            "attrChanges": [{"op": "attrSet", "id": 1, "key": "name", "value": "x"}],
        },
        "bad treeHash format": {
            "status": "SUCCESS", "scopeId": "s", "treeChanges": [], "treeHash": "not-a-hash"
        },
    }
    for label, env in bad.items():
        assert not v.is_valid(env), f"schema wrongly ACCEPTED bad envelope: {label}"

    tree_v = _validator(TREE_OP_ID)
    assert not tree_v.is_valid({"op": "frobnicate", "id": 1}), "unknown op accepted"


def test_treeafter_without_treehash_valid():
    """Positive side of the full-state contract: an adapter on the returns_full_state
    path ships `treeAfter` WITHOUT `treeHash` (the kernel computes it — wire §2).
    The schema must ACCEPT this (treeHash is required only with treeChanges)."""
    v = _validator(ENVELOPE_ID)
    env = {
        "status": "SUCCESS",
        "scopeId": "s",
        "treeAfter": {"id": None, "type": "ROOT", "children": []},
    }
    errors = sorted(v.iter_errors(env), key=str)
    assert not errors, f"treeAfter-without-treeHash wrongly rejected: {[e.message for e in errors]}"


def test_hash_vectors_structural():
    """hash_vectors.json trees are valid root sentinels (the same trees the
    cross-language hash conformance pins in test_conformance.py)."""
    node_v = _validator("https://treelens.dev/schema/node.schema.json")
    vectors = json.loads((REF_DIR / "tests" / "hash_vectors.json").read_text(encoding="utf-8"))
    assert vectors, "hash_vectors.json is empty"
    for entry in vectors:
        assert node_v.is_valid(entry["tree"]), f"vector tree not a valid sentinel: {entry['tree']}"


def _run():
    if not _HAVE_JSONSCHEMA:
        print("  skip  jsonschema not installed — schema suite skipped "
              "(install: pip install -e .[dev])")
        return
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} schema tests passed.")


if __name__ == "__main__":
    _run()

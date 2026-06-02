# MIT License — TreeLens reference demo.
"""End-to-end demo: run the treelens kernel against the toy in-memory host.

    python demo.py        (from the reference/ directory)

Shows the whole pattern with no real app installed:
  1. bootstrap + structural mutations (diff-as-response: thin envelopes)
  2. attr-mutation (rename) — carries attrChanges, NO treeChanges
  3. navigation over the mirror (query / subtree / path) — no host round-trip
  4. drift recovery via the integrity hash (a silent external edit)
  5. drift recovery via the push-listener (a notified external edit)
"""

import pathlib
import sys

# reference/ on the import path so `treelens` / `adapters` resolve from any cwd.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from adapters.in_memory import InMemoryHost  # noqa: E402

from treelens import TreeLens  # noqa: E402


def show(label, env):
    keep = {
        k: env[k]
        for k in ("treeChanges", "attrChanges", "stateVersion",
                  "driftRecovered", "resyncedExternalEdit", "response")
        if k in env
    }
    print(f"   {label}\n     -> {keep}")


def main():
    host = InMemoryHost("doc-1")
    lens = TreeLens(host)

    print("1) bootstrap + structural adds - first sight seeds the mirror by full read")
    a = lens.ingest(host.add_node(None, "GROUP", "Background"))
    show("add 'Background'", a)
    bg = a["response"]["createdId"]
    s = lens.ingest(host.add_node(bg, "PIXEL", "Sky"))
    show("add 'Sky' under Background", s)
    sky = s["response"]["createdId"]
    lens.ingest(host.add_node(bg, "PIXEL", "Hills"))

    print("\n2) attr-mutation (rename) - note: attrChanges only, no treeChanges")
    show("rename 'Sky' -> 'Sky Gradient'", lens.ingest(host.rename_node(sky, "Sky Gradient")))

    print("\n3) navigate the mirror (no host round-trip)")
    print("   query('Sky'):", lens.query("Sky"))
    print("   subtree(Background, depth=1):", lens.subtree(bg, depth=1))
    print("   path(Sky):", lens.path(sky))

    print("\n4) drift via integrity hash - a SILENT external edit")
    host.external_add_silent(bg, "PIXEL", "Sneaky Watermark")
    d = lens.ingest(host.add_node(None, "TEXT", "Title"))
    show("add 'Title' (next command after silent edit)", d)
    print("   query('Watermark') after recovery:", lens.query("Watermark")["matchCount"], "match(es)")

    print("\n5) drift via push-listener - a NOTIFIED external edit")
    host.external_add_notified(None, "GROUP", "User Folder")
    p = lens.ingest(host.rename_node(sky, "Sky v2"))
    show("rename (next command after notified edit)", p)
    print("   query('User Folder') after resync:", lens.query("User Folder")["matchCount"], "match(es)")

    print("\nfinal snapshot version:", lens.mirror.version("doc-1"))


if __name__ == "__main__":
    main()

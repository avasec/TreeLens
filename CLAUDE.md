# CLAUDE.md

Guidance for Claude Code (and any AI agent) working in this repository.

## What this repo is

**TreeLens** is a *pattern + reference kernel*, not a product. It documents and implements a
server-side, diff-synced mirror of a host application's live node hierarchy (Photoshop, Figma,
Unity, …) for use behind an MCP server. The repo is two things at once:

- **The written pattern** — `docs/` + `wire-protocol.md` + `schema/` (the normative contract).
- **A runnable reference skeleton** — the host-agnostic kernel (`treelens/`) + an abstract
  `HostAdapter` + a toy in-memory host (`adapters/in_memory.py`) on which the whole flow runs
  with no real application.

Start reading from `README.md` → `docs/pattern.md` → `docs/portability.md` →
`docs/open-problems.md`. The contributor rules live in `CONTRIBUTING.md` and are authoritative;
this file is the operational summary for an agent.

> **This is a public repository.** Never commit anything local, machine-specific, or sensitive
> (paths with usernames, tokens, `.env`, IDE state, `settings.local.json`). See `.gitignore`.

## Layout

| Path | What |
|---|---|
| `treelens/` | The host-agnostic **kernel**. Zero runtime dependencies (stdlib only). |
| `treelens/mirror.py` | Mirrored state (tree + attrs + meta + selection), atomic apply, query. |
| `treelens/diff.py` | Keyed reconciliation (`compute_tree_diff` / `compute_attr_diff`), pure. |
| `treelens/hashing.py` | Integrity hash (`tree_hash` / `stable_serialize` / `sha256_hex`). |
| `treelens/adapter.py` | The `HostAdapter` contract — the only host-specific seam. |
| `treelens/lens.py` | `TreeLens`: envelope ingest, drift detect/recover, payload strip. |
| `adapters/in_memory.py` | Toy fake host — runs the full flow without a real application. |
| `adapters/photoshop.md` | Adapter implementation guide for Photoshop/UXP (prose). |
| `wire-protocol.md` | Normative adapter↔kernel contract (prose). |
| `schema/*.json` | Machine-readable contract (JSON Schema, Draft 2020-12). |
| `js/` | Independent JS hash implementation + cross-language parity check. |
| `tests/` | `test_conformance.py` (kernel invariants), `test_schema.py` (envelope shape). |
| `docs/` | `pattern.md`, `portability.md`, `open-problems.md`. |
| `demo.py` | End-to-end demo on the toy host. |

## Commands

The kernel is zero-dep, but **a validating run requires the dev-extra** (`jsonschema`), otherwise
the schema suite is **silently skipped**.

```bash
pip install -e ".[dev]"        # pytest + jsonschema; the kernel itself needs nothing
pytest tests/                  # kernel conformance + schema conformance
python demo.py                 # end-to-end on the toy host
node js/check_vectors.js       # cross-language hash parity (needs Node)
```

> If `pytest` reports `N passed, M skipped`, the schema contract was **not** validated — you
> forgot the dev-extra. CI always installs it. A real green = `pip install -e ".[dev]"` first.

No installation needed for: `python tests/test_conformance.py` and `python demo.py` (they put
the repo root on `sys.path` themselves).

## Architectural invariants — DO NOT BREAK

These are the load-bearing rules (full text in `CONTRIBUTING.md` § "Architectural invariants"):

1. **Zero-dependency kernel.** `treelens/` imports stdlib only. Any external dep goes in the
   adapter or the dev-extra — never in the kernel.
2. **Clean kernel↔adapter seam.** The kernel knows nothing about any specific host. A host
   connects **only** through `HostAdapter`. The contract is `wire-protocol.md` (normative) +
   `schema/` (machine).
3. **A tree node is a pure structure** `{id, type, children}`. Name/visibility/etc. live in attrs.
4. **The root is the fixed marker `"ROOT"`.** Adapters normalize their own root to `"ROOT"`.
   The kernel is type-agnostic, but the hash includes `type`.
5. **Node matching strictly by `id`.** Positional fallback is forbidden.
6. **Hash canonicalization is runtime-neutral.** If you touch serialization, you MUST regenerate
   `tests/hash_vectors.json` (+ `hash_vectors_realworld.json`), run `pytest` **and**
   `node js/check_vectors.js`, and confirm JS↔Python parity holds.

## Change discipline

- **Contract changes are issue-first.** A new tree-/attr-op, an edit to the envelope / `schema/` /
  hash canonicalization ripples across spec + schema + two languages + frozen vectors. Discuss
  before writing a large PR. Small bug fixes, docs, and tests — PR directly.
- **Keep the contract in sync.** If you change kernel behavior or the contract, update
  `wire-protocol.md` / `schema/` / `CHANGELOG.md` together, in the same change.
- **Tests must go red on a realistic bug.** Pin specific behavior/values, keep an independent
  oracle next to the hash (deep-structural equality), include negative cases. "Didn't crash" is
  not a test. (`CONTRIBUTING.md` § "Tests — the honesty bar".)
- **Unfinished engine work** (batching, safe-wait, diff localization, persistence) belongs in
  `docs/open-problems.md` as roadmap — never pass off a design as done.
- **Adapters for real hosts live in the contributor's own repo**, not here. This repo is
  kernel + contract + conformance + a toy host.

## Conventions

- **English only.** All prose, code comments, commit messages, PRs, and issues are in English.
  (Talking with the user here may be in Russian, but anything written into the repo is English.)
- **License / acknowledgment.** MIT. TreeLens shares no code with `adb-mcp`; `NOTICE` credits it only
  as where the idea took shape, not as a code source — keep that framing accurate, don't reintroduce
  "derived from" / "fork of" / "license preserved" wording.
- Python ≥ 3.10. Match the surrounding code's style (terse, comment-dense at decision points).
- Before committing, re-read what you're about to change against the invariants above; a green
  test run is necessary but not sufficient.

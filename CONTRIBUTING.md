# Contributing to TreeLens

Thanks for your interest in the pattern. TreeLens is extracted from a working system (an MCP server
for Photoshop); the goal is for an outside developer to "just take the kernel and build their own MCP"
over any host with a large live hierarchy.

## What to contribute

The repo is a **validated kernel + contract + conformance**, not a catalog of adapters. In decreasing
order of openness:

- **Tests are the leading goal.** Additional conformance/edge cases, strengthening mutation-resistance.
  Additive, reinforces the trust-anchor, doesn't touch the validated core. The bar is below ("Tests —
  the honesty bar").
- **Docs — clarity and domain knowledge.** Fixes, clarifications, **host mappings written by hand**
  (Figma/Unity specifics from someone who knows the platform). Editing claims/decisions (de-name,
  single-writer, root-norm) ≠ editing clarity — discuss the former first.
- **Bug reports** — via issues, always.
- **Kernel / wire spec / schema — high-bar.** Bug fixes and clarity refinements — PR; **a change to
  kernel behavior or the contract — issue first** (it ripples across all adapters + the hash + two
  languages, this is "the crown"). Engine roadmap (batching, diff localization) — same, issue-first.
  Details — [open-problems.md](docs/open-problems.md).

**Not into this repo:** an adapter under your own host is **using** the pattern in your project (see
"An adapter under your own host"); a reference stub as a portability showcase — by agreement (same
section).

**Language — English.** English is the development language: write everything — prose, code comments,
commit messages, PRs, issues — in English. A contribution that isn't in English is rejected on review.

Doc translations are not accepted or maintained: a stale translation of the normative material
(`wire-protocol.md` / `schema/`) misleads, and a single maintainer can't verify it. Translate for
yourself — MIT permits that; the repo doesn't host translations.

Contributions are accepted under **MIT** (same as the project itself).

## Local run

```bash
pip install -e ".[dev]"      # kernel is zero-dep; the dev-extra installs pytest + jsonschema
pytest tests/                # kernel conformance + schema conformance
node js/check_vectors.js     # cross-language hash parity (needs Node) — an independent implementation
python demo.py               # end-to-end on a toy host, without a real application
```

The kernel (`treelens/`) can also be run **without installation** — `python tests/test_conformance.py`
and `python demo.py` put the root on `sys.path` themselves.

> **The validating run is `pip install -e ".[dev]"`.** Without the dev-extra, `tests/test_schema.py`
> is **silently skipped** (no `jsonschema`): `pytest` is green, but the schema contract is **not
> checked**. If you see `N passed, M skipped` — you did not validate the schema. CI always installs
> the dev-extra.

## Test tiers (and what about live)

Verifiability is a large part of the pattern's value, so be honest about **what** and **how** is verified:

- **Kernel conformance (headless, for everyone).** `pytest tests/` + `node js/check_vectors.js`:
  correctness of diff/mirror, atomic rollback, drift-recovery and **cross-language byte parity of the
  hash on real production trees** (`hash_vectors_realworld.json`). **Photoshop is not needed** — this
  is the runnable engine verification.
- **Toy end-to-end.** `python demo.py` — a full scenario on an in-memory host, without an application.
- **Adapter-level / live.** Tests against a **live** host live in the **adapter itself**, not in this
  repo. When you build an adapter, the kernel conformance suite + frozen vectors are your **trust-anchor**
  (your `canonical_hash` must match them), and on top of that — your tests on real diffs of your host.
- **No bundled live adapter yet.** The repo ships the kernel + a toy adapter + a guide
  (`adapters/photoshop.md`), but not a ready UXP plugin/relay for running against a live Photoshop
  (open-problems §8). If you want to feel the pattern against a real application — that means **building
  an adapter** (the guide + [portability.md](docs/portability.md)), in your repo. If a minimal showcase
  comes out of it — it's a candidate for a **reference stub** (by agreement, see "An adapter under your
  own host"), but the adapter itself lives with you.

## Architectural invariants (do not break)

- **Zero-dependency kernel.** `treelens/` imports only stdlib. Any external dependency goes in the
  adapter or the dev-extra, not the kernel.
- **Clean kernel↔adapter seam.** The kernel knows nothing about any specific host. A host connects
  **only** through `HostAdapter` (`treelens/adapter.py`); the contract is `wire-protocol.md` (normative)
  + `schema/` (machine). Change the contract → edit both + the vectors/tests in sync.
- **A tree node is a pure structure** `{id, type, children}`. Name/visibility/etc. live in attrs.
- **The root is a fixed marker `"ROOT"`.** The adapter **normalizes** its own root (e.g. Photoshop's
  "DOCUMENT") to `"ROOT"` (`wire-protocol.md` §3). The kernel is type-agnostic, but the hash includes `type`.
- **Node matching strictly by `id`.** A positional fallback is forbidden (see `wire-protocol.md` §3).
- **Hash canonicalization is runtime-neutral** (`wire-protocol.md` §7). If you touch serialization,
  update `hash_vectors.json` (+ `hash_vectors_realworld.json`), run `pytest` **and**
  `node js/check_vectors.js`, and make sure JS↔Python parity holds.

## Tests — the honesty bar

Green is not the goal. A test must **go red on a realistic bug** (mutation-resistant): pin **behavior**
(specific values/nodes), not "didn't crash"; keep an **independent oracle** next to the hash
(deep-structural equality — a symmetric compute/apply bug must not hide behind a matching hash);
negative cases (rejection on bad input) — mandatory, not just the happy path.

## An adapter under your own host (this is usage, not a PR here)

An adapter is **how you apply the pattern in your project**; it lives in **your** repo, not this one.
Implement `treelens.HostAdapter` for your host (minimum: stable ids, `read_tree`, mutations that return
a diff envelope, `canonical_hash` byte-for-byte with the kernel, **root normalized to `"ROOT"`**). Kernel
conformance + `node js/check_vectors.js` + frozen vectors are your **trust-anchor** (your `canonical_hash`
must match them); on top — your adapter-level tests on real host diffs. The contract in prose + host
mappings — [portability.md](docs/portability.md); PS specifics — `adapters/photoshop.md`.

**A reference stub (portability showcase) into this repo** is a *potential* contribution (e.g. a minimal
Figma/Unity stub proving the seam on a non-PS host), but **by agreement / on a case-by-case basis**: the
methodology for selecting "what makes an adapter exemplary" (contract coverage, conformance, value
measurements, maintainability) is not yet worked out. Want to propose one — open an issue, we'll discuss
it individually.

## Pull requests

- **Small stuff — PR right away:** bug fix, docs, test. (An adapter under your own host — **not here**,
  see "An adapter under your own host".)
- **Contract change — issue first:** a new tree-/attr-op, an edit to the envelope / `schema/` / hash
  canonicalization. It ripples across the spec + schema + two languages + vectors — discuss the approach
  **before** the code, so you don't rewrite a big PR.
- Tests green (`pytest tests/` + `node js/check_vectors.js`), demo runs through.
- If you change kernel behavior or the contract — update `wire-protocol.md` / `schema/` / `CHANGELOG.md`
  in sync.
- Unfinished engine pieces — into `open-problems.md` as roadmap; don't pass off a design as done.

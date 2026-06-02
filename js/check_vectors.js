// MIT License — TreeLens reference: cross-language hash conformance runner.
//
// Reproduces the frozen hash vectors with the JS implementation and compares
// them to the stored sha256. A mismatch means the JS and Python canonical
// serializations have diverged — the integrity hash would then false-positive
// drift across the host↔kernel seam. Run in CI alongside the Python suite so
// the cross-language parity claim is actually enforced, not just asserted.
//
//   node js/check_vectors.js     (from the reference/ root)

const fs = require("fs");
const path = require("path");
const { treeHash } = require("./canonical_hash.js");

const files = ["hash_vectors.json", "hash_vectors_realworld.json"];
let checked = 0;
let failed = 0;

for (const f of files) {
  const p = path.join(__dirname, "..", "tests", f);
  const vectors = JSON.parse(fs.readFileSync(p, "utf8"));
  if (!vectors.length) {
    console.error(`ERROR: ${f} is empty`);
    process.exit(1);
  }
  for (const v of vectors) {
    checked++;
    const got = treeHash(v.tree);
    if (got !== v.sha256) {
      failed++;
      const tag = v.label ? ` [${v.label}]` : "";
      console.error(`MISMATCH ${f}${tag}: expected ${v.sha256}, got ${got}`);
    }
  }
}

if (failed) {
  console.error(`\nCross-language hash parity FAILED: ${failed}/${checked} vectors diverged (JS vs frozen).`);
  process.exit(1);
}
console.log(`Cross-language hash parity OK: JS reproduces all ${checked} frozen vectors byte-for-byte.`);

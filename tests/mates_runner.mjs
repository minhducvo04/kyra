// Runs tests/data/mates_vectors.json through web/model3d-mates.js and prints one JSON result per case (parity check).
import { readFileSync } from 'node:fs';
import { validate, solve, applyEdit } from '../web/model3d-mates.js';

const vectors = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const out = {};
for (const c of vectors.cases) {
  const result = { reasons: validate(c.assembly) };
  let assembly = c.assembly;
  if (c.edit) {
    const edited = applyEdit(assembly, c.edit);
    result.edit = { ok: edited.ok, reason: edited.reason };
    assembly = edited.assembly;
  }
  result.solve = solve(assembly, c.driven_value === undefined ? null : c.driven_value);
  out[c.name] = result;
}
process.stdout.write(JSON.stringify(out));

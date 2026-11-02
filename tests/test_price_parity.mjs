import assert from 'node:assert/strict';
import fs from 'node:fs';
import { parityCase } from '../frontend/lib/price_math.mjs';

const vectors = JSON.parse(fs.readFileSync(new URL('./fixtures/price_change/shared_parity.json', import.meta.url), 'utf8'));
for (const test of vectors.cases) {
  const result = parityCase(test.current, test.reference);
  if (test.reason) {
    assert.equal(result.status, 'unavailable', test.id);
    assert.equal(result.reason, test.reason, test.id);
  } else {
    assert.equal(result.status, 'available', test.id);
    const expected = Number(test.expected_fraction);
    assert.ok(Math.abs(result.fraction - expected) <= Number(vectors.bound_factor) * Math.max(1, Math.abs(expected)), test.id);
    assert.equal(result.display, test.display, test.id);
    assert.equal(result.current_source, test.current, test.id);
  }
}
console.log(`Shared price parity: ${vectors.cases.length} vectors passed`);

const test = require('node:test');
const assert = require('node:assert/strict');
const {renderCards} = require('../recovery_metrics.js');

test('recovery metric cards render backend values, units, definitions, and denominators', () => {
  const html = renderCards({metrics: {
    recovery_success_rate: {value: .75, numerator: 3, denominator: 4, definition: 'exact restores'},
    rollback_latency_ms: {mean: 12.5, median: 10, p95: null, sample_count: 4,
      p95_unavailable_reason: 'P95 requires 20.', definition: 'restore timing'},
    lost_work_percentage: {value: 25, numerator: 1, denominator: 4, work_unit: 'word', definition: 'word loss'},
    false_rollback_trigger_rate: {value: .2, numerator: 1, denominator: 5, definition: 'healthy controls'},
    post_recovery_quality: {value: 87, baseline_mean: 90, delta_from_baseline: -3, accepted_count: 3, accepted_rate: .75, denominator: 4, definition: 'restored quality'}
  }});
  for (const expected of ['75.0%', '3 / 4 eligible attempts', '12.50 ms', 'P95 requires 20.',
    '25.0%', '1 word lost / 4 created', '20.0%', '1 / 5 healthy controls', '87.0 / 100', 'policy pass 3 / 4', 'rate 75.0%', '-3.0']) {
    assert.ok(html.includes(expected), `expected rendered metric text: ${expected}`);
  }
});

test('missing metrics display no results rather than sample numbers', () => {
  const html = renderCards(null);
  assert.match(html, /No results yet/);
  assert.doesNotMatch(html, /100%|0\.0%/);
});

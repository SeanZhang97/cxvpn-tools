const assert = require('node:assert/strict');
const path = require('node:path');

global.window = global;
require(path.join(__dirname, '..', 'ui', 'routing_telemetry.js'));

const tools = global.RoutingTelemetryTools;
assert.equal(tools.formatRate(0), '0 B/s');
assert.equal(tools.formatRate(1024), '1.00 KB/s');
assert.equal(tools.formatRate(12 * 1024 * 1024), '12.0 MB/s');

assert.deepEqual(tools.normalizeSnapshot({
  phase: 'connected', retry_seconds: 99,
  up: 1024, down: 2048, up_total: 4096, down_total: 8192,
  observability: { active: 2, label: '中文 e\u0301 🇯🇵' },
}), {
  phase: 'connected', retrySeconds: 15,
  up: 1024, down: 2048, upTotal: 4096, downTotal: 8192,
  observability: { active: 2, label: '中文 e\u0301 🇯🇵' },
});
assert.equal(tools.normalizeSnapshot({ phase: 'bad', up: -1 }).phase, 'idle');
assert.equal(tools.normalizeSnapshot({ phase: 'bad', up: -1 }).up, 0);

console.log('routing backend telemetry adapter: ok');

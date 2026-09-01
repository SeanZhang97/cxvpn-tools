const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const app = fs.readFileSync(path.join(__dirname, '..', 'ui', 'app.js'), 'utf8');
const proxy = fs.readFileSync(path.join(__dirname, '..', 'ui', 'proxy.js'), 'utf8');
const telemetry = fs.readFileSync(
  path.join(__dirname, '..', 'ui', 'routing_telemetry.js'), 'utf8');

assert.doesNotMatch(app, /setInterval\s*\(\s*poll/);
assert.doesNotMatch(app, /setTimeout\s*\(\s*poll/);
assert.doesNotMatch(app, /async function poll\s*\(/);
assert.match(app, /wait_ui_state\(uiStateVersion, 25\)/);
assert.match(app, /get_ui_state_snapshot\(\)/);
assert.match(app, /cxvpn:uistate/);
assert.match(app, /uiStateRetryMs = Math\.min\(15000/);
assert.match(proxy, /set_routing_telemetry_active/);
assert.match(proxy, /event\.detail\?\.routing_telemetry/);
assert.doesNotMatch(proxy, /get_routing_observability/);
assert.doesNotMatch(telemetry, /WebSocket|controller_secret|controller_port|token=/);

console.log('versioned UI event stream wiring: ok');

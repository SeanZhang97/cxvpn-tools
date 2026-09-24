const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'ui/index.html'), 'utf8');
const js = fs.readFileSync(path.join(root, 'ui/two_factor.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'ui/two_factor.css'), 'utf8');
const globalCss = fs.readFileSync(path.join(root, 'ui/style.css'), 'utf8');
const section = html.split('<section id="page-two-factor"')[1].split('</section>')[0];
assert.doesNotMatch(section, /收藏|分组|备注/);
assert.doesNotMatch(js, /(?:localStorage|sessionStorage)\s*\.|\.innerHTML\s*=|CFG\[/);
assert.match(js, /RoutingWorkspace\?\.enhanceSelect/);
for (const id of ['otp-algorithm', 'otp-digits']) assert.match(js, new RegExp(id));
const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map(match => match[1]));
for (const [, id] of js.matchAll(/byId\('([^']+)'\)/g)) assert.ok(ids.has(id), `missing ${id}`);
for (const [, file] of css.matchAll(/url\('([^']+)'\)/g)) assert.ok(fs.existsSync(path.join(root, 'ui', file)), `missing ${file}`);
assert.match(css, /scrollbar-gutter:\s*stable/);
assert.doesNotMatch(css, /scrollbar-(?:track|thumb)|::-webkit-scrollbar/);
for (const value of ['--scrollbar-track', '--scrollbar-thumb-hover', '--scrollbar-thumb-active',
  'scrollbar-color:', '::-webkit-scrollbar-button', '::-webkit-scrollbar-corner', 'scrollbar-gutter:']) {
  assert.ok(globalCss.includes(value), `global scrollbar contract missing ${value}`);
}
assert.match(css, /repeat\(3,minmax\(0,1fr\)\)/);
assert.match(css, /repeat\(2,minmax\(0,1fr\)\)/);
assert.match(js, /get_otp_code\(row.id\)/);
assert.match(js, /visibilitychange/);
assert.match(js, /Date.now\(\) - anchor.wall/);
console.log('2FA UI contracts passed');

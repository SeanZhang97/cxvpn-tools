const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const window = {};
vm.runInNewContext(fs.readFileSync(path.join(root, 'ui/two_factor_order.js'), 'utf8'), { window });
vm.runInNewContext(fs.readFileSync(path.join(root, 'ui/two_factor_brands.js'), 'utf8'), { window });
const order = window.TwoFactorOrder;
const plain = value => JSON.parse(JSON.stringify(value));
assert.deepEqual(plain(order.mergeVisible(['a', 'x', 'b', 'y', 'c'], ['c', 'a', 'b'])), ['c', 'x', 'a', 'y', 'b']);
assert.deepEqual(plain(order.move(['a', 'b', 'c'], 'a', 'c', true)), ['b', 'c', 'a']);
assert.deepEqual(plain(order.move(['a', 'b', 'c'], 'c', 'a', false)), ['c', 'a', 'b']);

function fixture() {
  const events = {}, saves = [], messages = [];
  let focused, ready = true;
  const cards = ['a', 'b', 'c'].map((id, i) => {
    const classes = new Set(), attributes = {};
    const card = { dataset: { id }, classList: {
      add: (...names) => names.forEach(name => classes.add(name)),
      remove: (...names) => names.forEach(name => classes.delete(name)),
      contains: name => classes.has(name),
    }, closest: selector => selector === '.otp-card' ? card : null,
    getBoundingClientRect: () => ({ left: i * 300, top: 0, width: 280, height: 200 }) };
    const handle = { closest: selector => selector === '.otp-card' ? card : selector === '.otp-drag' ? handle : null,
      setAttribute: (name, value) => { attributes[name] = value; }, focus: () => { focused = id; } };
    card.handle = handle;
    card.querySelector = () => handle;
    return card;
  });
  const grid = { querySelectorAll: () => cards, closest: () => null,
    append(card) { cards.splice(cards.indexOf(card), 1); cards.push(card); },
    addEventListener: (name, handler) => { events[name] = handler; } };
  const controller = order.attach(grid, { ready: () => ready, visible: () => ['a', 'b', 'c'],
    all: () => ['a', 'hidden', 'b', 'c'], start() {}, save: async (...args) => saves.push(plain(args)),
    announce: message => messages.push(message) });
  function emit(name, target, extra = {}) {
    let prevented = false;
    events[name]({ target, preventDefault() { prevented = true; }, dataTransfer: {
      setData() {}, setDragImage() {},
    }, ...extra });
    return prevented;
  }
  return { cards, grid, controller, saves, messages, emit,
    setReady(value) { ready = value; }, focused: () => focused, ids: () => cards.map(card => card.dataset.id) };
}
let f = fixture();
assert.equal(f.emit('dragstart', f.cards[0]), true, 'only a handle may initiate dragging');
f.setReady(false);
assert.equal(f.emit('dragstart', f.cards[0].handle), true, 'loading/saving blocks new drag');
f.setReady(true);
f.emit('dragstart', f.cards[0].handle);
f.emit('dragover', f.cards[2], { clientX: 850, clientY: 170 });
assert.equal(f.cards[2].classList.contains('otp-drop-after'), true);
f.emit('drop', f.cards[2]);
assert.deepEqual(f.saves[0], [['b', 'hidden', 'c', 'a'], ['a', 'hidden', 'b', 'c'], 'a']);
assert.deepEqual(f.ids(), ['b', 'c', 'a']);
assert.equal(f.controller.active(), false);

for (const end of ['dragend', 'drop']) {
  f = fixture();
  f.emit('dragstart', f.cards[0].handle);
  f.emit('dragover', f.cards[2], { clientX: 850, clientY: 170 });
  f.emit(end, f.grid);
  assert.equal(f.saves.length, 0, 'dropping outside a card cancels stale target');
  assert.deepEqual(f.ids(), ['a', 'b', 'c']);
}
f = fixture();
const first = f.cards[0].handle;
f.emit('keydown', first, { key: ' ' });
f.emit('keydown', first, { key: 'End' });
assert.deepEqual(f.ids(), ['b', 'c', 'a']);
assert.equal(f.focused(), 'a');
f.emit('keydown', first, { key: 'Escape' });
assert.deepEqual(f.ids(), ['a', 'b', 'c']);
assert.equal(f.focused(), 'a');
assert.equal(f.saves.length, 0);
f.emit('keydown', first, { key: 'Enter' });
f.emit('keydown', first, { key: 'ArrowRight' });
f.emit('keydown', first, { key: 'Enter' });
assert.deepEqual(f.saves[0][0], ['b', 'hidden', 'a', 'c']);

for (const [issuer, file] of [['GitHub', 'github.svg'], ['GOOGLE', 'google.png'],
  ['https://gitlab.com/', 'gitlab.svg'], ['OpenAI', 'openai.svg'], ['ChatGPT', 'openai.svg'], ['谷歌', 'google.png']]) {
  const asset = window.TwoFactorBrands.image(issuer);
  assert.equal(asset, `icons/brands/${file}`);
  assert.ok(fs.existsSync(path.join(root, 'ui', asset)));
}
for (const issuer of ['github.example.com', '<img src=x>', '本地平台 é 🇯🇵', '']) {
  assert.equal(window.TwoFactorBrands.image(issuer), null, 'unknown platforms use initial fallback');
}
console.log('2FA drag, keyboard, filter order and brand contracts passed');

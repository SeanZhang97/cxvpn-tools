const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');

// 只模拟组件使用的 DOM，验证选择、刷新和焦点；不连接真实订阅或修改用户配置。
let document;
class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.dataset = {}; this.style = {};
    this.attributes = {}; this.listeners = {}; this.className = ''; this.value = '';
    this.textContent = ''; this.disabled = false; this.hidden = false;
    this.classList = {
      contains: value => this.className.split(' ').includes(value),
      add: value => { if (!this.classList.contains(value)) this.className += ` ${value}`; },
      remove: value => { this.className = this.className.split(' ').filter(item => item !== value).join(' '); },
      toggle: (value, enabled) => { (enabled ?? !this.classList.contains(value)) ? this.classList.add(value) : this.classList.remove(value); },
    };
  }
  get options() { return this.tagName === 'select' ? this.children : []; }
  get selectedIndex() { return this.options.findIndex(option => option.value === this.value); }
  get scrollHeight() { return this.children.length * 40; }
  append(...items) { items.forEach(item => { item.remove(); item.parentNode = this; this.children.push(item); }); }
  replaceChildren(...items) { this.children.forEach(item => { item.parentNode = null; }); this.children = []; this.append(...items); }
  insertBefore(item, before) { item.remove(); item.parentNode = this; this.children.splice(this.children.indexOf(before), 0, item); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(item => item !== this); this.parentNode = null; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, listener) { (this.listeners[name] ||= []).push(listener); }
  dispatchEvent(event) { this[`on${event.type}`]?.(event); (this.listeners[event.type] || []).forEach(fn => fn(event)); }
  focus() { document.activeElement = this; }
  contains(item) { return item === this || this.children.some(child => child.contains(item)); }
  matches(selector) {
    if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
    const attribute = selector.match(/^\[(role|data-region)="([^"]*)"\]$/);
    if (attribute) return (attribute[1] === 'data-region' ? this.dataset.region : this.getAttribute(attribute[1])) === attribute[2];
    return selector === this.tagName;
  }
  querySelectorAll(selector) { return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { return this.matches(selector) ? this : this.parentNode?.closest(selector); }
  scrollIntoView() {}
  getBoundingClientRect() { return this.rect || { left: 25, top: 100, bottom: 136, width: 102 }; }
}
document = {
  body: new Element('body'), activeElement: null,
  createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag),
  getElementById: () => null, querySelector: selector => document.body.querySelector(selector),
  addEventListener() {},
};
const context = { console, document, Event, setTimeout, clearTimeout,
  window: { addEventListener() {}, innerWidth: 1000, innerHeight: 700 } };
vm.createContext(context);
for (const file of ['routing_regions.js', 'routing_nodes.js', 'routing_region_picker.js']) {
  vm.runInContext(fs.readFileSync(path.join(root, 'ui', file), 'utf8'), context);
}
const source = fs.readFileSync(path.join(root, 'ui', 'routing.js'), 'utf8');
vm.runInContext(source.replace('  window.RoutingWorkspace = {', `
  window.selectTest = { enhanceRoutingSelect, closeRoutingSelect, placeRoutingMenu };
  window.RoutingWorkspace = {`), context);
const picker = context.window.RoutingRegionPicker;
const bridge = context.window.selectTest;
const tools = context.window.RoutingNodeTools;
const regions = tools.regionOptions([
  ...['美国', '香港', '日本', '新加坡', '韩国', '台湾', '老挝', '荷兰', '波兰', '埃及', '越南'],
  '美国', '美国', '香港',
].map(name => ({ name })));
const data = value => JSON.parse(JSON.stringify(value));
assert.equal(picker.shortcuts(regions, 'all').length, 6);
assert.equal(picker.shortcuts(regions, 'PL').length, 6);
assert.ok(picker.shortcuts(regions, 'PL').some(item => item.code === 'PL'));
assert.equal(new Set(picker.shortcuts(regions, 'PL').map(item => item.code)).size, 6);

const host = new Element();
const quick = new Element(); quick.className = 'routing-region-shortcuts';
const more = new Element(); more.className = 'routing-region-more';
const select = new Element('select'); select.setAttribute('aria-label', '更多地区');
more.append(select); host.append(quick, more); document.body.append(host);
const changes = [];
let selected = 'all';
const onChange = code => { changes.push(code); selected = code; picker.render(host, regions, code, onChange, bridge.enhanceRoutingSelect); };
picker.render(host, regions, selected, onChange, bridge.enhanceRoutingSelect);
const widget = select._routingWidget;
assert.equal(quick.children.length, 6);
assert.ok(select.classList.contains('routing-select-native'));
assert.equal(widget.current.children[0].textContent, '更多地区');
assert.equal(widget.trigger.getAttribute('aria-haspopup'), 'dialog');
assert.equal(widget.trigger.getAttribute('aria-activedescendant'), null);

const click = () => widget.trigger.onclick({ stopPropagation() {} });
const search = widget.menu.querySelector('input');
const options = widget.menu.querySelector('.routing-region-options');
const empty = widget.menu.querySelector('p');
const visible = () => options.children.filter(item => !item.hidden);
const key = value => search.onkeydown({ key: value, preventDefault() {} });
click();
assert.equal(widget.trigger.getAttribute('aria-expanded'), 'true');
assert.equal(search.getAttribute('aria-expanded'), 'true');
assert.equal(document.activeElement, search);
assert.equal(options.children.length, regions.length, 'every populated country remains independently accessible');
search.value = 'Netherlands'; search.oninput();
assert.deepEqual(visible().map(item => item.dataset.value), ['NL']);
const activeBefore = widget.activeIndex;
picker.render(host, regions, selected, onChange, bridge.enhanceRoutingSelect);
assert.equal(search.value, 'Netherlands', 'normal background refresh must preserve search');
assert.equal(widget.activeIndex, activeBefore);
assert.equal(document.activeElement, search);
key('Enter');
assert.deepEqual(changes, ['NL']);
assert.equal(select.value, 'NL');
assert.equal(widget.trigger.getAttribute('aria-expanded'), 'false');
assert.equal(document.activeElement, widget.trigger);
assert.ok(quick.children.some(item => item.dataset.region === 'NL' && item.classList.contains('active')));
assert.equal(widget.current.children[0].textContent, '更多地区');

click(); search.value = '不存在的国家 e\u0301 🇱🇦'; search.oninput();
assert.equal(visible().length, 0); assert.equal(empty.hidden, false);
assert.equal(search.getAttribute('aria-activedescendant'), null);
key('Enter'); assert.equal(changes.length, 1);
search.value = 'LAO'; search.oninput();
assert.deepEqual(visible().map(item => item.dataset.value), ['LA']);
search.value = ''; search.oninput();
key('Home'); assert.equal(widget.activeIndex, 0);
key('ArrowDown'); assert.equal(widget.activeIndex, 1);
key('ArrowUp'); assert.equal(widget.activeIndex, 0);
key('End'); assert.equal(widget.activeIndex, regions.length - 1);
key('Escape'); assert.equal(document.activeElement, widget.trigger);
assert.equal(search.getAttribute('aria-expanded'), 'false');
widget.trigger.onkeydown({ key: ' ', preventDefault() {} });
assert.equal(document.activeElement, search);
key('Tab'); assert.equal(widget.trigger.getAttribute('aria-expanded'), 'false');

click(); search.value = '荷蘭'; search.oninput();
assert.deepEqual(visible().map(item => item.dataset.value), ['NL']);
const updated = data(regions); updated[1].count++;
picker.render(host, updated, selected, onChange, bridge.enhanceRoutingSelect);
assert.equal(document.activeElement, search);
assert.equal(search.value, '荷蘭');
assert.deepEqual(visible().map(item => item.dataset.value), ['NL']);
widget.trigger.rect = { left: 950, top: 590, bottom: 626, width: 102 };
bridge.placeRoutingMenu(widget);
assert.equal(widget.menu.style.width, '460px');
assert.equal(widget.menu.style.left, '530px');
assert.ok(parseFloat(widget.menu.style.top) >= 10 && parseFloat(widget.menu.style.top) < 590);
context.window.innerWidth = 340; bridge.placeRoutingMenu(widget);
assert.equal(widget.menu.style.width, '320px');
assert.equal(widget.menu.style.left, '10px');
context.window.innerWidth = 1000;
const other = new Element('select');
const only = new Element('option'); only.value = 'one'; only.textContent = '普通下拉框';
const second = new Element('option'); second.value = 'two'; second.textContent = '普通下拉框 e\u0301';
other.append(only, second); other.value = 'one'; document.body.append(other);
const otherWidget = bridge.enhanceRoutingSelect(other, { compact: true });
otherWidget.open();
assert.equal(widget.trigger.getAttribute('aria-expanded'), 'false', 'shared dropdown mutual exclusion must close the region popup');
assert.equal(search.getAttribute('aria-expanded'), 'false');
otherWidget.trigger.onkeydown({ key: 'Home', preventDefault() {} });
otherWidget.trigger.onkeydown({ key: 'ArrowDown', preventDefault() {} });
assert.equal(otherWidget.activeIndex, 1);
otherWidget.trigger.onkeydown({ key: 'Enter', preventDefault() {} });
assert.equal(other.value, 'two', 'ordinary dropdowns must retain their existing keyboard selection');
assert.equal(otherWidget.trigger.getAttribute('aria-expanded'), 'false');
click(); select.disabled = true; widget.refresh();
assert.equal(widget.trigger.disabled, true);
assert.equal(widget.trigger.getAttribute('aria-expanded'), 'false');
picker.render(host, [{ code: 'all', label: '全部', count: 0 }], 'all', onChange, bridge.enhanceRoutingSelect);
assert.equal(widget.trigger.disabled, true);
assert.equal(quick.children.length, 1);

const css = fs.readFileSync(path.join(root, 'ui', 'style.css'), 'utf8');
assert.match(css, /\.routing-region-options \{[^}]*repeat\(3,minmax\(0,1fr\)\)[^}]*overflow: auto; scrollbar-gutter: stable;/);
assert.match(css, /@container node-regions \(max-width: 280px\)/);
assert.match(css, /nth-child\(n\+2\):not\(\.active\)/);
assert.match(css, /\.routing-node-filterbar \{[^}]*grid-template-columns: minmax\(0,1fr\) auto; align-items: start;/);
assert.match(source, /if \(searchable\) window\.RoutingRegionPicker\.enhanceMenu\(widget\)/);
console.log('region shortcuts, searchable shared popup, focus and viewport bounds: ok');

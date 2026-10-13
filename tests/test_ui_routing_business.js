const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'ui/routing_business.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'ui/index.html'), 'utf8');
const css = fs.readFileSync(path.join(root, 'ui/routing_rules.css'), 'utf8');
const routing = fs.readFileSync(path.join(root, 'ui/routing.js'), 'utf8');
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.value = ''; this.textContent = ''; this.attributes = {}; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(name, value) { this.attributes[name] = value; }
  get options() { return this.children; }
  querySelectorAll(tag) { return this.children.flatMap(item => [...(item.tagName === tag ? [item] : []), ...item.querySelectorAll(tag)]); }
}
const elements = new Map();
const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
let sequence = 0, changed = 0, warning = '';
const context = { window: {}, crypto: { randomUUID: () => `${++sequence}0000000-0000-0000-0000-000000000000` },
  document: { getElementById: get, createElement: tag => new Element(tag) } };
vm.createContext(context); vm.runInContext(source, context);
const config = { proxy_providers: [{ id: 'alpha', name: '订阅甲', enabled: true }], rules: [] };
const nodeA = '中文 e\u0301 🇯🇵 [a].+';
const ctx = { config, busy: false,
  groups: [{ id: 'alpha', preview: true, nodes: [{ name: nodeA, alive: true, tested: true, delay: 18 }, { name: '节点 B' }] }],
  templates: [{ id: 'chatgpt', name: 'ChatGPT', domains: ['chatgpt.com'] }, { id: 'github', name: 'GitHub', domains: ['github.com'] }],
  nodes: group => group?.nodes || [], rawName: node => node.name,
  enhance: select => { select.enhanced = true; }, destroy() {},
  changed: () => changed++, feedback: text => { warning = text; }, openNodes() {},
};
ctx.refresh = () => context.window.RoutingBusiness.render(ctx);
ctx.refresh();
ctx.groups[0].nodes.push(...Array.from({ length: 150 }, (_, index) => ({ name: `日本节点 ${index}`, delay: index, alive: true, tested: true })));
get('btn-business-outbound-add').onclick();
assert.equal(cardsForCount().querySelectorAll('select')[1].options.length, 153);
function cardsForCount() { return get('routing-business-outbounds').children[0]; }
let cards = () => get('routing-business-outbounds').children;
cards()[0].querySelectorAll('select')[1].value = nodeA;
cards()[0].querySelectorAll('select')[1].onchange();
get('btn-business-outbound-add').onclick();
cards()[1].querySelectorAll('select')[1].value = '节点 B';
cards()[1].querySelectorAll('select')[1].onchange();
assert.equal(config.proxy_outbounds[0].node_name, nodeA);
assert.equal(config.proxy_outbounds[1].node_name, '节点 B');
assert.equal(config.proxy_providers[0].selected_node, undefined);
const findButton = (root, label) => root.querySelectorAll('button').find(b => b.textContent === label);
const exitDetails = () => cards()[0].children[2];
assert.equal(exitDetails().hidden, true, '节点选择常驻，管理区域默认折叠');
const beforeDisclosure = changed;
findButton(cards()[0], '搜索 / 管理').onclick();
assert.equal(exitDetails().hidden, false);
assert.equal(findButton(cards()[0], '收起').attributes['aria-expanded'], 'true');
assert.equal(findButton(cards()[0], '收起').attributes['aria-controls'], exitDetails().id);
ctx.refresh();
assert.equal(exitDetails().hidden, false, '模型刷新不收起正在使用的编辑区');
assert.equal(changed, beforeDisclosure, '展开与收起不是配置修改');
const search = cards()[0].querySelectorAll('input')[1];
search.value = '日本节点 149'; search.oninput();
assert.ok(cards()[0].querySelectorAll('select')[1].options.some(o => o.value === '日本节点 149'));
assert.equal(cards()[0].querySelectorAll('select')[1].options.length, 3);
findButton(cards()[0], '收起').onclick();
assert.equal(cards()[0].querySelectorAll('select')[1].options.length, 153, '收起搜索后清除隐藏筛选条件');
get('business-template-tools').children[0].onclick();
get('business-template-tools').children[1].onclick();
let sets = () => get('routing-business-sets').children;
assert.equal(sets()[0].children[2].hidden, true, '模板默认展示出口和域名数量');
findButton(sets()[0], '编辑域名').onclick();
sets()[1].querySelectorAll('select')[0].value = `outbound:${config.proxy_outbounds[1].id}`;
sets()[1].querySelectorAll('select')[0].onchange();
assert.equal(sets()[0].children[2].hidden, false, '修改另一规则不重置已展开项');
sets()[0].querySelectorAll('textarea')[1].value = 'api.chatgpt.com';
sets()[0].querySelectorAll('textarea')[1].oninput();
assert.equal(config.business_rules[0].exclude_domains[0], 'api.chatgpt.com');
assert.equal(sets()[0].children[1].children[0].children[1].textContent, '1 个域名 · 1 个排除');
findButton(sets()[0], '下移').onclick();
assert.equal(config.business_rules[1].name, 'ChatGPT');
assert.equal(sets()[1].children[2].hidden, false, '展开状态跟随规则 ID 而非位置');
get('business-template-tools').children[2].onclick();
assert.equal(sets()[2].children[2].hidden, false, '自定义空规则自动展开域名编辑');
assert.equal(get('business-sets-empty').hidden, true);
const customName = sets()[2].querySelectorAll('input')[0];
customName.value = nodeA; customName.oninput(); ctx.refresh();
assert.equal(sets()[2].querySelectorAll('input')[0].value, nodeA, '中文、组合字符和非 BMP 名称保持原样');
for (const parent of [...cards(), ...sets()]) {
  for (const label of parent.querySelectorAll('label').filter(l => l.attributes.for)) {
    assert.ok(['input', 'select', 'textarea'].flatMap(tag => parent.querySelectorAll(tag)).some(c => c.id === label.attributes.for));
  }
}
cards()[0].querySelectorAll('button').find(b => b.textContent === '删除出口').onclick();
assert.match(warning, /引用/); assert.equal(config.proxy_outbounds.length, 2);
for (const parent of [...cards(), ...sets()]) assert.ok(parent.querySelectorAll('select').every(s => s.enhanced));
const dynamicSelects = [...cards(), ...sets()].flatMap(parent => parent.querySelectorAll('select'));
assert.deepEqual([...new Set(dynamicSelects.map(s => s.attributes['aria-label']))].sort(),
  ['业务规则集出口', '出口固定节点', '出口所属订阅'].sort(), '新增动态下拉必须登记并接入组件');
ctx.busy = true; ctx.refresh();
for (const parent of [...cards(), ...sets()]) for (const tag of ['input', 'select', 'textarea', 'button']) {
  assert.ok(parent.querySelectorAll(tag).every(s => s.disabled));
}
config.traffic_mode = 'global'; ctx.refresh(); assert.match(get('business-mode-warning').textContent, /不生效/);
assert.ok(changed >= 6);
for (const [, id] of source.matchAll(/byId\('([^']+)'\)/g)) assert.ok(html.includes(`id="${id}"`));
assert.match(css, /business-editor textarea[^}]*scrollbar-gutter: stable/);
assert.match(css, /business-editor \.field[^}]*width: auto/);
assert.match(css, /business-editor \[hidden\][^{]*\{ display: none/);
assert.match(css, /@media \(max-width: 600px\)/);
assert.doesNotMatch(css.slice(0, css.indexOf('@media')), /scrollbar-color|scrollbar-track/);
assert.match(routing, /代理出口引用|该订阅被代理出口引用/);
assert.match(routing, /select\.id !== 'routing-default'/);
console.log('business routing UI checks passed');

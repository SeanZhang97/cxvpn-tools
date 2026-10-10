const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

global.window = global;
require(path.join(__dirname, '..', 'ui', 'routing_regions.js'));
require(path.join(__dirname, '..', 'ui', 'routing_nodes.js'));

const tools = global.RoutingNodeTools;
assert.ok(tools);
assert.equal(tools.REGION_LABELS.JP, '日本');
assert.equal(tools.REGION_LABELS.US, '美国');
const catalog = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'ui', 'routing_regions.json'), 'utf8'));
assert.deepEqual(global.RoutingRegionCatalog, catalog, 'generated frontend catalog must match the shared JSON');
assert.equal(Object.keys(catalog).length, 258);
for (const [code, [label]] of Object.entries(catalog)) {
  if (code === 'OTHER') continue;
  assert.equal(tools.regionCode({ name: `pro-${label}-01` }), code, label);
  assert.equal(tools.regionCode({ name: `pro-${code}-01` }), code, code);
  const flag = [...code].map(char => String.fromCodePoint(0x1F1E6 + char.charCodeAt(0) - 65)).join('');
  assert.equal(tools.regionCode({ name: `中继 e\u0301 ${flag} 01` }), code, `${code} flag`);
}

assert.deepEqual(tools.splitNodeLabel({ name: '[订阅] 🇯🇵 日本东京' }), {
  text: '[订阅] 日本东京', country: 'JP',
});
assert.equal(tools.regionCode({ name: '香港 HKG 01' }), 'HK');
assert.equal(tools.regionCode({ name: 'Sa\u0303o Paulo 0.5倍' }), 'BR');
assert.equal(tools.regionCode({ name: '🇸🇬 Singapore 01' }), 'SG');
assert.equal(tools.regionCode({ name: '无地区中继节点' }), 'OTHER');
for (const [name, code] of [
  ['pro-老挝-2x', 'LA'], ['pro-荷兰', 'NL'], ['pro-波兰', 'PL'],
  ['pro-瑞士-5x', 'CH'], ['pro-埃及-10x', 'EG'], ['pro-泰国-2x', 'TH'],
  ['pro-越南', 'VN'], ['荷蘭', 'NL'], ['Netherlands 01', 'NL'], ['POL 01', 'PL'],
  ['[日本订阅] Laos 01', 'LA'], ['印度尼西亚', 'ID'], ['Indonesia 01', 'ID'],
  ['巴布亚新几内亚', 'PG'], ['几内亚比绍', 'GW'], ['南苏丹', 'SS'],
  ['North Korea 01', 'KP'], ['🇳🇱 美国中转', 'NL'],
  ['ordinary relay in transit', 'OTHER'], ['pro-online-01', 'OTHER'],
  ['🇿🇿 未命名中继', 'OTHER'],
]) assert.equal(tools.regionCode({ name }), code, name);

const worldwideNodes = ['香港', '日本', '美国', '新加坡', '韩国', '台湾', '老挝', '荷兰', '波兰', '瑞士', '泰国', '埃及', '越南']
  .map(name => ({ name: `pro-${name}` }));
const worldwideRegions = tools.regionOptions(worldwideNodes);
assert.equal(worldwideRegions.length, worldwideNodes.length + 1, 'all populated regions must remain visible beyond seven');
assert.equal(worldwideRegions.slice(1).reduce((sum, region) => sum + region.count, 0), worldwideNodes.length);
assert.ok(worldwideRegions.some(region => region.code === 'LA' && region.label === '老挝'));
assert.ok(!worldwideRegions.some(region => region.code === 'OTHER'));
assert.deepEqual(tools.regionOptions([]), [{ code: 'all', label: '全部', count: 0 }]);

const regions = tools.regionOptions([
  { name: '🇯🇵 东京 01' }, { name: '日本大阪 02' }, { name: '香港 01' }, { name: '普通中继' },
]);
assert.deepEqual(regions[0], { code: 'all', label: '全部', count: 4 });
assert.deepEqual(regions[1], { code: 'JP', label: '日本', count: 2 });
assert.ok(regions.some(item => item.code === 'HK' && item.count === 1));

const nodes = [
  { name: '🇯🇵 东京 2倍', delay: 180, alive: true, tested: true },
  { name: '🇭🇰 香港 0.5倍', delay: 80, alive: true, tested: true },
  { name: '🇸🇬 新加坡', alive: false, tested: true },
  { name: '台湾 01', tested: false },
];
assert.equal(tools.sortNodes(nodes, 'delay')[0].name, '🇭🇰 香港 0.5倍');
assert.equal(tools.sortNodes(nodes, 'multiplier')[0].name, '🇭🇰 香港 0.5倍');
assert.equal(tools.sortNodes(nodes, 'current', ['🇸🇬 新加坡'])[0].name, '🇸🇬 新加坡');
assert.deepEqual(tools.sortNodes(nodes, 'default').map(node => node.name), nodes.map(node => node.name));

const subscription = tools.subscriptionMetadata([
  { name: '剩余流量：88.61 GB' },
  { name: '距离下次重置剩余：17 天' },
  { name: '套餐到期：2027-03-18' },
  { name: '公告：维护通知，请查看官网' },
  { name: 'Sa\u0303o Paulo 🇧🇷 0.5倍' },
], Date.UTC(2026, 8, 1));
assert.deepEqual(subscription.items, [
  { label: '剩余流量', value: '88.61 GB' },
  { label: '距离重置', value: '17 天' },
  { label: '套餐到期', value: '2027-03-18' },
]);
assert.equal(subscription.expiryDays, 198);
assert.equal(subscription.tone, '');
assert.equal(tools.subscriptionMetadata([{ name: '剩余流量 900 MB' }]).tone, 'danger');
assert.equal(tools.subscriptionMetadata([{ name: '总流量 100 GB' }, { name: '剩余流量 12 GB' }]).tone, 'warning');
assert.equal(tools.subscriptionMetadata([{ name: '公告：剩余席位 3 个' }]).items.length, 0);

console.log('routing node filters and sorting: ok');

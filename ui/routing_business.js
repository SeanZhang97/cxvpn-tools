/* 命名出口与业务规则集编辑器；共享分流草稿及保存事务。 */
(() => {
  const byId = id => document.getElementById(id);
  const uid = prefix => `${prefix}-${crypto.randomUUID().slice(0, 12)}`;
  const lines = text => [...new Set(text.split(/\r?\n/).map(s => s.trim()).filter(Boolean))];
  const refs = (config, id) => [...(config.rules || []), ...(config.business_rules || [])]
    .filter(rule => rule.outbound === `outbound:${id}`);
  const expanded = new Set();
  let fieldSequence = 0;
  function box(className, ...children) {
    const root = document.createElement('div'); root.className = className;
    root.append(...children); return root;
  }
  function field(label, control) {
    const root = box('field');
    const title = document.createElement('label'); title.textContent = label;
    control.id = `business-field-${++fieldSequence}`;
    title.setAttribute('for', control.id);
    root.append(title, control); return root;
  }
  function disclosure(key, panel, label, busy, onClose = () => {}) {
    panel.id = `business-detail-${key}`;
    const toggle = button('', () => {
      if (expanded.has(key)) { expanded.delete(key); onClose(); } else expanded.add(key);
      sync();
    }, busy);
    const sync = () => {
      panel.hidden = !expanded.has(key);
      toggle.textContent = panel.hidden ? label : '收起';
      toggle.setAttribute('aria-expanded', String(!panel.hidden));
    };
    toggle.className += ' business-disclosure';
    toggle.setAttribute('aria-controls', panel.id); sync();
    return toggle;
  }
  function input(value, change, busy, multiline = false) {
    const control = document.createElement(multiline ? 'textarea' : 'input');
    control.value = value || ''; control.disabled = busy;
    if (multiline) control.rows = 5;
    control.oninput = () => change(control.value);
    return control;
  }
  function button(label, action, disabled) {
    const control = document.createElement('button'); control.type = 'button';
    control.className = 'btn ghost'; control.textContent = label;
    control.disabled = !!disabled; control.onclick = action; return control;
  }
  function select(items, selected, change, busy, label) {
    const control = document.createElement('select');
    control.setAttribute('aria-label', label);
    for (const [value, text] of items) {
      const option = document.createElement('option'); option.value = value; option.textContent = text;
      control.append(option);
    }
    if (selected && !items.some(([value]) => value === selected)) {
      const option = document.createElement('option'); option.value = selected;
      option.textContent = `${selected}（当前不可用）`; control.append(option);
    }
    control.value = selected || ''; control.disabled = busy;
    control.onchange = () => change(control.value);
    return control;
  }
  function mount(root, children, ctx) {
    ctx.destroy(root); root.replaceChildren(...children);
    root.querySelectorAll('select').forEach(control => ctx.enhance(control, { compact: true }));
  }
  function render(ctx) {
    const root = byId('routing-business-outbounds');
    if (!root || !ctx.config) return;
    const config = ctx.config;
    config.proxy_outbounds ||= []; config.business_rules ||= [];
    const change = () => ctx.changed();
    const redraw = () => { change(); ctx.refresh(); };
    const providers = (config.proxy_providers || []).filter(p => p.enabled);
    const outbounds = config.proxy_outbounds;
    byId('btn-business-outbound-add').disabled = ctx.busy || !providers.length || outbounds.length >= 32;
    byId('btn-business-outbound-add').onclick = () => {
      outbounds.push({ id: uid('exit'), name: `代理出口 ${outbounds.length + 1}`,
        provider_id: providers[0]?.id || '', node_name: '' }); redraw();
    };
    const cards = outbounds.map(item => {
      const card = box('business-editor business-outbound');
      const group = ctx.groups.find(g => g.id === item.provider_id);
      const nodes = ctx.nodes(group).map(n => ({ ...n, raw: ctx.rawName(n, !!group?.preview) })).filter(n => n.raw);
      const name = input(item.name, value => { item.name = value; change(); }, ctx.busy);
      name.onchange = () => ctx.refresh();
      const owner = select(providers.map(p => [p.id, p.name]), item.provider_id, value => {
        item.provider_id = value; item.node_name = ''; redraw();
      }, ctx.busy, '出口所属订阅');
      const nodeSelect = select([['', '请选择节点'], ...nodes.map(n => [n.raw,
        `${n.raw}${n.tested ? n.alive ? ` · ${n.delay} ms` : ' · 不可达' : ''}`])], item.node_name,
      value => { item.node_name = value; redraw(); }, ctx.busy, '出口固定节点');
      const search = input('', query => {
        const needle = query.toLocaleLowerCase();
        nodeSelect.replaceChildren();
        const choices = [['', '请选择节点'], ...nodes.filter(n => n.raw === item.node_name ||
          `${n.raw} ${window.RoutingNodeTools?.REGION_LABELS?.[window.RoutingNodeTools?.regionCode(n)] || ''}`.toLocaleLowerCase().includes(needle))
          .map(n => [n.raw, `${n.raw}${n.tested ? n.alive ? ` · ${n.delay} ms` : ' · 不可达' : ''}`])];
        if (item.node_name && !choices.some(([value]) => value === item.node_name)) choices.push([item.node_name, `${item.node_name}（当前不可用）`]);
        choices.forEach(([value, label]) => {
          const option = document.createElement('option'); option.value = value; option.textContent = label;
          nodeSelect.append(option);
        });
        nodeSelect.value = item.node_name;
        nodeSelect._routingWidget?.refresh();
      }, ctx.busy);
      search.placeholder = '搜索节点名称或地区';
      const note = document.createElement('p'); note.className = 'business-status';
      note.textContent = !item.node_name ? '尚未选择节点'
        : !nodes.some(n => n.raw === item.node_name) ? '节点不可用，请刷新订阅核对；缺失时拒绝连接。'
          : `已固定节点 · ${refs(config, item.id).length} 项规则引用`;
      if (!item.node_name || !nodes.some(n => n.raw === item.node_name)) note.className += ' business-status-warning';
      const actions = document.createElement('div'); actions.className = 'business-actions';
      actions.append(button('查看节点 / 测速', () => ctx.openNodes(item.provider_id), ctx.busy),
        button('删除出口', () => {
          const used = refs(config, item.id);
          if (used.length) { ctx.feedback(`该出口被 ${used.length} 条规则或规则集引用（包含停用项），请先改绑或删除引用。`, 'warning'); return; }
          expanded.delete(`exit-${item.id}`);
          config.proxy_outbounds = outbounds.filter(row => row.id !== item.id); redraw();
        }, ctx.busy));
      const detail = box('business-outbound-detail', field('筛选上方节点列表', search), actions);
      card.append(box('business-outbound-fields', field('出口名称', name), field('订阅', owner),
        field('固定节点', nodeSelect)),
      box('business-row-footer', note, disclosure(`exit-${item.id}`, detail, '搜索 / 管理', ctx.busy,
        () => { search.value = ''; search.oninput(); })), detail);
      return card;
    });
    mount(root, cards, ctx);
    byId('business-outbounds-empty').hidden = !!cards.length;
    const tools = byId('business-template-tools');
    const addSet = template => {
      const id = uid('set');
      if (!template.domains.length) expanded.add(`set-${id}`);
      config.business_rules.push({ id, name: template.name,
        enabled: true, outbound: `outbound:${outbounds[0].id}`,
        domains: [...template.domains], exclude_domains: [] }); redraw();
    };
    const templates = ctx.templates.map(template => button(`添加 ${template.name}`, () => addSet(template),
      ctx.busy || !outbounds.length || config.business_rules.length >= 32));
    templates.push(button('添加自定义规则集', () => addSet({ name: `业务规则集 ${config.business_rules.length + 1}`, domains: [] }),
      ctx.busy || !outbounds.length || config.business_rules.length >= 32));
    tools.replaceChildren(...templates);
    const sets = config.business_rules.map((item, index) => {
      const card = box('business-editor business-rule');
      const enabled = document.createElement('input'); enabled.type = 'checkbox';
      enabled.checked = item.enabled !== false; enabled.disabled = ctx.busy;
      enabled.onchange = () => { item.enabled = enabled.checked; redraw(); };
      const target = select(outbounds.map(o => [`outbound:${o.id}`, o.name]), item.outbound,
        value => { item.outbound = value; redraw(); }, ctx.busy, '业务规则集出口');
      const actions = document.createElement('div'); actions.className = 'business-actions';
      const move = delta => {
        const rows = config.business_rules;
        [rows[index], rows[index + delta]] = [rows[index + delta], rows[index]]; redraw();
      };
      actions.append(button('上移', () => move(-1), ctx.busy || index === 0),
        button('下移', () => move(1), ctx.busy || index === config.business_rules.length - 1),
        button('删除规则集', () => { expanded.delete(`set-${item.id}`); config.business_rules.splice(index, 1); redraw(); }, ctx.busy));
      const count = document.createElement('span'); count.className = 'business-domain-count';
      const updateCount = () => { count.textContent = `${item.domains.length} 个域名 · ${(item.exclude_domains || []).length} 个排除`; };
      updateCount();
      const detail = box('business-rule-detail', box('business-domain-fields',
        field('域名后缀 · 每行一个，含子域名', input(item.domains.join('\n'), v => { item.domains = lines(v); updateCount(); change(); }, ctx.busy, true)),
        field('排除域名 · 继续匹配后续规则', input((item.exclude_domains || []).join('\n'), v => { item.exclude_domains = lines(v); updateCount(); change(); }, ctx.busy, true))), actions);
      const switchLabel = document.createElement('label'); switchLabel.className = 'routing-option-switch business-switch';
      const switchText = document.createElement('span'); switchText.textContent = item.enabled !== false ? '已启用' : '已停用';
      enabled.setAttribute('aria-label', `启用规则集 ${item.name}`);
      const indicator = document.createElement('i'); indicator.setAttribute('aria-hidden', 'true');
      switchLabel.append(switchText, enabled, indicator);
      card.append(box('business-rule-fields', field('规则集名称', input(item.name, v => { item.name = v; change(); }, ctx.busy)),
        field('代理出口', target)),
      box('business-row-footer', box('business-rule-meta', switchLabel, count),
        disclosure(`set-${item.id}`, detail, '编辑域名', ctx.busy)), detail);
      return card;
    });
    mount(byId('routing-business-sets'), sets, ctx);
    byId('business-sets-empty').hidden = !!sets.length;
    byId('business-mode-warning').textContent = config.traffic_mode === 'global'
      ? '当前为全局模式：业务规则集不生效，切回规则模式后生效。' : '';
  }
  window.RoutingBusiness = { render, refs, lines };
})();

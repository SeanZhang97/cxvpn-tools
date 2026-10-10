/* 地区快捷筛选与可搜索浮层，复用分流工作区的下拉组件。 */
(() => {
  const normalize = value => String(value || '').normalize('NFKD').replace(/\p{M}/gu, '').toLocaleLowerCase();

  function shortcuts(regions, selectedCode) {
    const all = regions.find(region => region.code === 'all');
    const visible = regions.filter(region => region.code !== 'all').slice(0, 5);
    const selected = regions.find(region => region.code === selectedCode && region.code !== 'all');
    if (selected && !visible.includes(selected)) visible.splice(visible.length - 1, 1, selected);
    return all ? [all, ...visible] : visible;
  }

  function render(root, regions, selectedCode, onChange, enhanceSelect) {
    if (!root) return;
    const signature = JSON.stringify([regions, selectedCode]);
    if (root._regionSignature === signature) return;
    root._regionSignature = signature;
    const quick = root.querySelector('.routing-region-shortcuts');
    const focusedCode = quick.contains(document.activeElement) ? document.activeElement.dataset.region : '';
    quick.replaceChildren(...shortcuts(regions, selectedCode).map(region => {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'routing-region-chip';
      button.dataset.region = region.code;
      button.title = `${region.label}，${region.count} 个节点`;
      button.setAttribute('aria-label', button.title);
      button.setAttribute('aria-pressed', String(region.code === selectedCode));
      button.classList.toggle('active', region.code === selectedCode);
      const label = document.createElement('span'); label.textContent = region.label;
      const count = document.createElement('small'); count.textContent = String(region.count);
      button.append(label, count);
      button.onclick = () => onChange(region.code);
      return button;
    }));
    quick.querySelector(`[data-region="${focusedCode}"]`)?.focus();
    const select = root.querySelector('select');
    select.replaceChildren(...regions.map(region => {
      const choice = document.createElement('option');
      choice.value = region.code; choice.textContent = region.label;
      choice.dataset.detail = String(region.count);
      choice.dataset.search = [region.code, region.label,
        ...(window.RoutingRegionCatalog[region.code]?.[1] || [])].join(' ');
      return choice;
    }));
    select.value = selectedCode;
    select.disabled = regions.length <= 1;
    select.onchange = () => onChange(select.value);
    enhanceSelect(select, { compact: true, searchable: true, triggerLabel: '更多地区' });
  }

  function enhanceMenu(widget) {
    const { menu, trigger, select } = widget;
    menu.classList.add('routing-region-menu');
    menu.setAttribute('role', 'dialog');
    menu.setAttribute('aria-label', '选择地区');
    trigger.setAttribute('aria-haspopup', 'dialog');
    const search = document.createElement('input');
    search.type = 'search'; search.placeholder = '搜索国家、地区或代码';
    search.className = 'routing-region-search';
    search.setAttribute('aria-label', '搜索国家、地区或代码');
    search.setAttribute('role', 'combobox');
    search.setAttribute('aria-autocomplete', 'list');
    search.setAttribute('aria-expanded', 'false');
    const choices = document.createElement('div');
    choices.className = 'routing-region-options'; choices.id = `${menu.id}-regions`;
    choices.setAttribute('role', 'listbox'); choices.setAttribute('aria-label', '地区');
    search.setAttribute('aria-controls', choices.id);
    const empty = document.createElement('p');
    empty.className = 'routing-region-empty'; empty.textContent = '没有匹配的地区';
    empty.setAttribute('role', 'status');
    const visible = () => [...choices.children].filter(button => !button.hidden && !button.disabled);
    const filter = () => {
      const term = normalize(search.value).trim();
      [...choices.children].forEach(button => {
        button.hidden = !normalize(button.dataset.search).includes(term);
      });
      const items = visible();
      empty.hidden = items.length > 0;
      if (items.length && trigger.getAttribute('aria-expanded') === 'true') widget.setActive(Number((items.find(button => Number(button.dataset.index) === widget.activeIndex) || items[0]).dataset.index));
      else {
        widget.activeIndex = -1;
        search.removeAttribute('aria-activedescendant');
        trigger.removeAttribute('aria-activedescendant');
      }
    };
    search.oninput = filter;
    search.onkeydown = event => {
      const items = visible();
      if (['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        if (!items.length) return;
        const active = items.findIndex(button => Number(button.dataset.index) === widget.activeIndex);
        const index = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
          : Math.max(0, Math.min(items.length - 1, active + (event.key === 'ArrowDown' ? 1 : -1)));
        widget.setActive(Number(items[index].dataset.index));
      } else if (event.key === 'Enter') {
        event.preventDefault();
        if (widget.activeIndex >= 0 && items.length) widget.choose(widget.activeIndex);
      } else if (event.key === 'Escape') {
        event.preventDefault(); widget.close(true);
      } else if (event.key === 'Tab') widget.close(true);
    };
    widget.searchControl = {
      hasFocus: () => document.activeElement === search,
      focus: () => search.focus(),
      setActive: id => search.setAttribute('aria-activedescendant', id),
      close() {
        search.setAttribute('aria-expanded', 'false'); search.removeAttribute('aria-activedescendant');
      },
      reset() { search.value = ''; search.setAttribute('aria-expanded', 'true'); filter(); },
      update() {
        const buttons = [...menu.querySelectorAll('[role="option"]')];
        buttons.forEach(button => {
          button.tabIndex = -1; button.title = select.options[Number(button.dataset.index)].textContent;
        });
        choices.replaceChildren(...buttons);
        menu.replaceChildren(search, choices, empty);
        filter();
      },
    };
  }

  window.RoutingRegionPicker = Object.freeze({ render, shortcuts, enhanceMenu });
})();

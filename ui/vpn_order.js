/* VPN 展示顺序独立于连接状态；拖拽期间保留行，提交后重放最新快照。 */
(() => {
  'use strict';
  let owner = null;
  function sort(profiles, order) {
    const ranks = new Map((Array.isArray(order) ? order : []).map((name, index) => [name, index]));
    return [...profiles].sort((a, b) => {
      const rank = (ranks.get(a.name) ?? Infinity) - (ranks.get(b.name) ?? Infinity);
      return rank || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
    });
  }
  function move(names, source, target, after) {
    if (source === target || !names.includes(source) || !names.includes(target)) return [...names];
    const next = names.filter(name => name !== source);
    next.splice(next.indexOf(target) + Number(after), 0, source);
    return next;
  }
  function handle(name) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'vpn-drag';
    button.draggable = true;
    button.textContent = '⠿';
    button.title = '拖拽排序；Enter 选中，方向键移动，Enter 保存，Escape 取消';
    button.setAttribute('aria-label', `调整 ${name} 的顺序`);
    button.setAttribute('aria-pressed', 'false');
    return button;
  }
  function attach(root, options) {
    let session = null;
    let saving = false;
    const rows = () => [...root.querySelectorAll('[data-vpn-name]')];
    const find = name => rows().find(row => row.dataset.vpnName === name);
    function clear() {
      rows().forEach(row => {
        row.classList.remove('vpn-dragging', 'vpn-drop-before', 'vpn-drop-after');
        row.querySelector('.vpn-drag').setAttribute('aria-pressed', 'false');
      });
    }
    function arrange(names) {
      const slot = root.querySelector('.connection-slot-empty');
      names.forEach(name => { const row = find(name); if (row) root.insertBefore(row, slot); });
    }
    function focus(name) { find(name)?.querySelector('.vpn-drag').focus(); }
    function start(name, keyboard) {
      if (owner || session || saving || !options.ready()) return false;
      owner = root;
      const original = rows().map(row => row.dataset.vpnName);
      session = { name, keyboard, original, next: [...original] };
      find(name).classList.add('vpn-dragging');
      find(name).querySelector('.vpn-drag').setAttribute('aria-pressed', 'true');
      return true;
    }
    function cancel() {
      if (!session) return;
      const { name, original } = session;
      session = null;
      owner = null;
      clear();
      arrange(original);
      options.refresh();
      focus(name);
    }
    async function commit() {
      const { name, original, next } = session;
      session = null;
      saving = true;
      clear();
      arrange(next);
      try {
        if (next.some((value, index) => value !== original[index])) {
          console.info('[vpn-order] 提交排序保存请求');
          await options.save(next);
        }
      } catch (error) {
        arrange(original);
        options.error(error);
      } finally {
        saving = false;
        owner = null;
        options.refresh();
        focus(name);
      }
    }
    root.addEventListener('dragstart', event => {
      const button = event.target.closest('.vpn-drag');
      if (!button || !start(button.closest('[data-vpn-name]').dataset.vpnName, false)) {
        event.preventDefault(); return;
      }
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', session.name);
      event.dataTransfer.setDragImage(find(session.name), 20, 20);
    });
    function targetOrder(event) {
      const target = event.target.closest('[data-vpn-name]');
      if (!target) return null;
      const rect = target.getBoundingClientRect();
      const after = event.clientY > rect.top + rect.height / 2;
      return { target, after, next: move(session.original, session.name, target.dataset.vpnName, after) };
    }
    root.addEventListener('dragover', event => {
      if (!session || session.keyboard) return;
      event.preventDefault();
      const target = targetOrder(event);
      clear();
      find(session.name)?.classList.add('vpn-dragging');
      if (target) target.target.classList.add(target.after ? 'vpn-drop-after' : 'vpn-drop-before');
    });
    root.addEventListener('drop', event => {
      if (!session || session.keyboard) return;
      event.preventDefault();
      const target = targetOrder(event);
      if (!target) { cancel(); return; }
      session.next = target.next;
      void commit();
    });
    root.addEventListener('dragend', cancel);
    root.addEventListener('focusout', () => {
      queueMicrotask(() => {
        if (session?.keyboard && !root.contains(document.activeElement)) cancel();
      });
    });
    root.addEventListener('keydown', event => {
      const button = event.target.closest('.vpn-drag');
      if (!button) return;
      const name = button.closest('[data-vpn-name]').dataset.vpnName;
      if (!session && ['Enter', ' '].includes(event.key)) {
        event.preventDefault(); start(name, true); return;
      }
      if (!session?.keyboard || session.name !== name) return;
      if (['Escape', 'Tab'].includes(event.key)) {
        if (event.key === 'Escape') event.preventDefault();
        cancel(); return;
      }
      if (['Enter', ' '].includes(event.key)) { event.preventDefault(); void commit(); return; }
      if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const index = session.next.indexOf(name);
      const target = event.key === 'Home' ? 0 : event.key === 'End' ? session.next.length - 1
        : Math.max(0, Math.min(session.next.length - 1, index + (event.key === 'ArrowUp' ? -1 : 1)));
      session.next.splice(index, 1);
      session.next.splice(target, 0, name);
      arrange(session.next);
      focus(name);
    });
    return { active: () => session !== null || saving };
  }
  window.VpnOrder = { sort, move, handle, attach };
})();

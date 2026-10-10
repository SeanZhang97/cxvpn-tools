/* 手柄拖拽与键盘移动共用一份可见顺序；提交时保留筛选外的条目位置。 */
(() => {
  'use strict';
  function mergeVisible(all, visible) {
    const selected = new Set(visible);
    let index = 0;
    return all.map(id => selected.has(id) ? visible[index++] : id);
  }
  function move(ids, source, target, after) {
    if (source === target || !ids.includes(source) || !ids.includes(target)) return [...ids];
    const next = ids.filter(id => id !== source);
    next.splice(next.indexOf(target) + Number(after), 0, source);
    return next;
  }
  function attach(grid, options) {
    let session = null;
    const cards = () => [...grid.querySelectorAll('.otp-card')];
    const find = id => cards().find(card => card.dataset.id === id);
    function clearMarks() {
      cards().forEach(card => card.classList.remove('otp-dragging', 'otp-drop-before', 'otp-drop-after'));
    }
    function arrange(ids) {
      const map = new Map(cards().map(card => [card.dataset.id, card]));
      ids.forEach(id => { if (map.has(id)) grid.append(map.get(id)); });
    }
    function focus(id) { find(id)?.querySelector('.otp-drag')?.focus(); }
    function announce(message) { options.announce(message); }
    function start(id, keyboard) {
      if (session || !options.ready() || !options.visible().includes(id)) return false;
      const original = options.visible();
      session = { id, keyboard, original, next: [...original], all: options.all() };
      options.start();
      find(id)?.classList.add('otp-dragging');
      find(id)?.querySelector('.otp-drag')?.setAttribute('aria-pressed', 'true');
      if (keyboard) announce('已选中账号，方向键移动，Enter 保存，Escape 取消');
      return true;
    }
    function cancel(restore = true) {
      if (!session) return;
      const { id, original } = session;
      session = null;
      clearMarks();
      find(id)?.querySelector('.otp-drag')?.setAttribute('aria-pressed', 'false');
      if (restore) arrange(original);
    }
    async function commit() {
      if (!session) return;
      const { id, next, original, all } = session;
      const changed = next.some((value, index) => value !== original[index]);
      cancel(false);
      if (!changed) { arrange(original); focus(id); return; }
      arrange(next);
      await options.save(mergeVisible(all, next), all, id);
    }
    grid.addEventListener('dragstart', event => {
      const handle = event.target.closest('.otp-drag');
      if (!handle || !start(handle.closest('.otp-card').dataset.id, false)) { event.preventDefault(); return; }
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', session.id);
      event.dataTransfer.setDragImage(find(session.id), 25, 28);
    });
    grid.addEventListener('dragover', event => {
      if (!session || session.keyboard) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      const target = event.target.closest('.otp-card');
      if (!target || target.dataset.id === session.id) {
        clearMarks();
        find(session.id)?.classList.add('otp-dragging');
        session.next = [...session.original];
        return;
      }
      const rect = target.getBoundingClientRect();
      const after = event.clientY > rect.top + rect.height * .75 ||
        event.clientY >= rect.top + rect.height * .25 && event.clientX >= rect.left + rect.width / 2;
      clearMarks();
      find(session.id)?.classList.add('otp-dragging');
      target.classList.add(after ? 'otp-drop-after' : 'otp-drop-before');
      session.next = move(session.original, session.id, target.dataset.id, after);
    });
    grid.addEventListener('drop', event => {
      if (!session || session.keyboard) return;
      event.preventDefault();
      const target = event.target.closest('.otp-card');
      if (!target || target.dataset.id === session.id) { cancel(); return; }
      void commit();
    });
    grid.addEventListener('dragend', () => cancel());
    grid.addEventListener('keydown', event => {
      const handle = event.target.closest('.otp-drag');
      if (!handle) return;
      const id = handle.closest('.otp-card').dataset.id;
      if (!session && (event.key === ' ' || event.key === 'Enter')) {
        event.preventDefault(); start(id, true); return;
      }
      if (!session?.keyboard || session.id !== id) return;
      if (event.key === 'Escape' || event.key === 'Tab') {
        if (event.key === 'Escape') event.preventDefault();
        cancel(); focus(id); announce('已取消调整'); return;
      }
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); void commit(); return; }
      if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const index = session.next.indexOf(id);
      const target = event.key === 'Home' ? 0 : event.key === 'End' ? session.next.length - 1
        : Math.max(0, Math.min(session.next.length - 1,
          index + (['ArrowLeft', 'ArrowUp'].includes(event.key) ? -1 : 1)));
      session.next.splice(index, 1);
      session.next.splice(target, 0, id);
      arrange(session.next);
      focus(id);
      announce(`已移至第 ${target + 1} 位，Enter 保存，Escape 取消`);
    });
    return { cancel, active: () => session !== null };
  }
  window.TwoFactorOrder = { attach, mergeVisible, move };
})();

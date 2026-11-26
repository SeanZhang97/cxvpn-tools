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
    // 选择器与状态类名可由工作区覆盖；2FA 沿用默认值。
    const cardSelector = options.card || '.otp-card', handleSelector = options.handle || '.otp-drag';
    const states = { dragging: `${options.prefix || 'otp'}-dragging`,
      before: `${options.prefix || 'otp'}-drop-before`, after: `${options.prefix || 'otp'}-drop-after`,
      into: `${options.prefix || 'otp'}-drop-into` };
    const selectedMessage = options.selectedMessage || '已选中账号，方向键移动，Enter 保存，Escape 取消';
    let session = null;
    const cards = () => [...grid.querySelectorAll(cardSelector)];
    const find = id => cards().find(card => card.dataset.id === id);
    const handleOf = id => find(id)?.querySelector(handleSelector);
    function clearMarks() {
      cards().forEach(card => card.classList.remove(states.dragging, states.before, states.after, states.into));
    }
    function arrange(ids) {
      const map = new Map(cards().map(card => [card.dataset.id, card]));
      ids.forEach(id => { if (map.has(id)) grid.append(map.get(id)); });
    }
    function focus(id) { handleOf(id)?.focus(); }
    function announce(message) { options.announce(message); }
    function start(id, keyboard) {
      if (session || !options.ready() || !options.visible().includes(id)) return false;
      const original = options.visible();
      session = { id, keyboard, original, next: [...original], all: options.all(),
        into: null, target: null, after: false };
      options.start();
      find(id)?.classList.add(states.dragging);
      handleOf(id)?.setAttribute('aria-pressed', 'true');
      if (keyboard) announce(selectedMessage);
      return true;
    }
    function cancel(restore = true) {
      if (!session) return;
      const { id, original } = session;
      session = null;
      clearMarks();
      handleOf(id)?.setAttribute('aria-pressed', 'false');
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
    function previewDrop(event) {
      const target = event.target.closest(cardSelector);
      const into = target && target.dataset.id !== session.id && options.into?.target(target, event)
        ? target.dataset.id : null;
      let after = false, next = [...session.original];
      if (target && target.dataset.id !== session.id && !into) {
        const sameDrop = session.target === target.dataset.id && session.into === into;
        if (sameDrop && !Number.isFinite(event.clientX) && !Number.isFinite(event.clientY)) {
          after = session.after;
          next = session.next;
        } else {
          const rect = target.getBoundingClientRect();
          after = event.clientY > rect.top + rect.height * .75 ||
            event.clientY >= rect.top + rect.height * .25 && event.clientX >= rect.left + rect.width / 2;
          next = move(session.original, session.id, target.dataset.id, after);
        }
      }
      clearMarks();
      find(session.id)?.classList.add(states.dragging);
      if (into) target.classList.add(states.into);
      else if (target && target.dataset.id !== session.id) target.classList.add(after ? states.after : states.before);
      session.target = target?.dataset.id || null;
      session.after = after;
      session.into = into;
      session.next = next;
      return { target, into };
    }
    async function commitInto(targetId) {
      const save = options.into?.save;
      if (!session || !save) return;
      const sourceId = session.id;
      cancel(false);
      await save(sourceId, targetId);
    }
    grid.addEventListener('dragstart', event => {
      const handle = event.target.closest(handleSelector);
      if (!handle || !start(handle.closest(cardSelector).dataset.id, false)) { event.preventDefault(); return; }
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', session.id);
      event.dataTransfer.setDragImage(find(session.id), 25, 28);
    });
    grid.addEventListener('dragover', event => {
      if (!session || session.keyboard) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      previewDrop(event);
    });
    grid.addEventListener('drop', event => {
      if (!session || session.keyboard) return;
      event.preventDefault();
      const drop = previewDrop(event);
      if (!drop.target || drop.target.dataset.id === session.id) { cancel(); return; }
      if (drop.into) { void commitInto(drop.into); return; }
      void commit();
    });
    grid.addEventListener('dragend', () => cancel());
    grid.addEventListener('keydown', event => {
      const handle = event.target.closest(handleSelector);
      if (!handle) return;
      const id = handle.closest(cardSelector).dataset.id;
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
    return { cancel, active: () => session !== null,
      draggingKey: () => session && !session.keyboard ? session.id : null };
  }
  window.TwoFactorOrder = { attach, mergeVisible, move };
})();

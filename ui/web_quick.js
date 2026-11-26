/* 网页快开工作区：文件夹与网址的浏览、整理和打开；密码仅在编辑时按条目读取。 */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const backend = () => window.pywebview?.api;
  const active = () => byId('page-web-quick').classList.contains('active') && !document.hidden;
  const CALL_MS = 10000;
  const RUN_TEXT = { pending: '等待 Edge 扩展领取', claimed: '正在自动登录', success: '自动登录成功',
    failed: '自动登录失败', expired: '扩展未领取，任务已失效', timeout: '未收到登录结果', cancelled: '任务已取消' };
  const RUN_TONE = { pending: 'active', claimed: 'active', success: 'ok', failed: 'bad',
    expired: 'bad', timeout: 'bad', cancelled: 'muted' };
  let data = { folders: [], items: [], runs: {}, sites: [], auto_login_count: 0, extension_seen: 0, extension: {} };
  let current = null, loaded = false, fetching = false, busy = false, epoch = 0, revision = -1;
  let refreshPending = false;
  let orderController = null, menuEntry = null, menuTrigger = null, returnFocus = null;
  let editing = null, editorSession = 0, editorBusy = false, mode = 'open';
  let folderEditing = null, folderBusy = false;
  let faviconTimer = 0, faviconSequence = 0;
  let faviconAttempts = new Set(), editorFavicon = null;
  const faviconCache = new Map(), faviconPending = new Map();
  const opening = new Set();
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const icon = name => {
    const node = element('span', `wq-icon wq-icon-${name}`);
    node.setAttribute('aria-hidden', 'true');
    return node;
  };
  function firstGlyph(value) {
    const text = String(value || '').trim();
    if (!text) return '';
    let glyph;
    try {
      const segments = new Intl.Segmenter('zh', { granularity: 'grapheme' }).segment(text);
      const first = segments[Symbol.iterator]().next();
      glyph = first.value && typeof first.value === 'object' ? first.value.segment || '' : first.value || '';
    } catch (error) { glyph = Array.from(text)[0] || ''; }
    return glyph.toLocaleUpperCase();
  }
  function fallbackFor(row) {
    const text = firstGlyph(row?.name);
    return text ? { text } : { icon: 'quick' };
  }
  function cacheFavicon(key, dataUrl) {
    if (faviconCache.size >= 256) faviconCache.delete(faviconCache.keys().next().value);
    faviconCache.set(key, dataUrl);
    byId('wq-grid').querySelectorAll('.wq-avatar').forEach(avatar => {
      if (avatar.dataset.faviconKey !== key) return;
      const card = avatar.closest('.wq-card');
      const row = data.items.find(item => keyOf('item', item.id) === card?.dataset.id);
      showFavicon(avatar, dataUrl, row ? fallbackFor(row) : { icon: 'quick' });
    });
  }
  function showFavicon(avatar, dataUrl, fallback) {
    const showDefault = () => {
      avatar.classList.toggle('wq-fallback', Boolean(fallback?.text));
      if (fallback?.text) avatar.replaceChildren(element('span', 'wq-fallback-glyph', fallback.text));
      else avatar.replaceChildren(icon(fallback?.icon || 'quick'));
    };
    showDefault();
    const valid = typeof dataUrl === 'string' && dataUrl.startsWith('data:image/png;base64,');
    avatar.classList.toggle('wq-has-favicon', valid);
    if (!valid) return;
    const image = element('img', 'wq-favicon-image');
    image.alt = '';
    image.draggable = false;
    image.addEventListener('error', () => { if (image.isConnected) showDefault(); });
    avatar.replaceChildren(image);
    image.src = dataUrl;
  }
  function loadCachedFavicon(avatar, row) {
    const key = row.favicon_key || '', fallback = fallbackFor(row);
    showFavicon(avatar, '', fallback);
    if (!key) return;
    avatar.dataset.faviconKey = key;
    const apply = result => {
      if (result?.key === key) cacheFavicon(key, result.found ? result.data_url : '');
      const dataUrl = faviconCache.get(key) || '';
      if (avatar.isConnected && avatar.dataset.faviconKey === key) showFavicon(avatar, dataUrl, fallback);
    };
    if (faviconCache.has(key)) { void Promise.resolve().then(apply); return; }
    if (!faviconPending.has(key)) {
      const pending = call(backend().get_cached_web_quick_favicon(key), '读取图标缓存')
        .catch(() => null).finally(() => { faviconPending.delete(key); });
      faviconPending.set(key, pending);
    }
    void faviconPending.get(key).then(apply);
  }
  function checked(result) {
    if (!result || result.ok === false) throw new Error(result?.msg || '操作未完成');
    return result;
  }
  function call(promise, label) {
    let timer;
    const timeout = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`${label}超时，请重试`)), CALL_MS); });
    return Promise.race([promise, timeout]).then(checked).finally(() => clearTimeout(timer));
  }
  const feedback = (id, message = '', tone = '') => setFeedback(byId(id), message, tone);
  const enhanceSelect = select => window.RoutingWorkspace?.enhanceSelect(select);
  const refreshSelects = () => {
    ['wq-in-folder', 'wq-otp', 'wq-parent'].forEach(id => enhanceSelect(byId(id))?.refresh());
  };

  // ---------- 目录结构 ----------
  const keyOf = (kind, id) => `${kind}:${id}`;
  function siblings(parentId) {
    const rows = [
      ...data.folders.filter(row => (row.parent_id || null) === parentId)
        .map(row => ({ key: keyOf('folder', row.id), kind: 'folder', row })),
      ...data.items.filter(row => (row.folder_id || null) === parentId)
        .map(row => ({ key: keyOf('item', row.id), kind: 'item', row })),
    ];
    // 与后端一致：position、created_at、文件夹在前、ID。
    rows.sort((a, b) => a.row.position - b.row.position || a.row.created_at - b.row.created_at
      || (a.kind === b.kind ? 0 : a.kind === 'folder' ? -1 : 1) || (a.row.id < b.row.id ? -1 : 1));
    return rows;
  }
  function ancestors(folderId) {
    const folders = new Map(data.folders.map(row => [row.id, row])), path = [];
    for (let id = folderId, guard = 0; id && guard <= 64; guard++) {
      const row = folders.get(id);
      if (!row) break;
      path.unshift(row);
      id = row.parent_id || null;
    }
    return path;
  }
  const pathNames = folderId => ['全部', ...ancestors(folderId).map(row => row.name)];
  function platformFor(url) {
    try {
      const { origin } = new URL(String(url).trim());
      return data.sites.find(site => site.origin === origin) || null;
    } catch (error) { return null; }
  }
  const searching = () => byId('wq-search').value.trim().length > 0;
  function searchResults(needle = byId('wq-search').value.trim().toLocaleLowerCase()) {
    if (!needle) return [];
    const match = row => `${row.name}\n${row.url || ''}`.toLocaleLowerCase().includes(needle);
    const place = entry => ancestors(entry.kind === 'folder' ? entry.row.parent_id || null : entry.row.folder_id || null)
      .map(row => row.name).join('/');
    return [
      ...data.folders.filter(match).map(row => ({ key: keyOf('folder', row.id), kind: 'folder', row })),
      ...data.items.filter(match).map(row => ({ key: keyOf('item', row.id), kind: 'item', row })),
    ].sort((a, b) => place(a).localeCompare(place(b), 'zh') || a.row.position - b.row.position || a.key.localeCompare(b.key));
  }
  function runText(run) {
    const text = RUN_TEXT[run.state] || '';
    if (run.state === 'failed') return run.detail ? `${text}：${run.detail}` : text;
    return run.detail || text;
  }

  // ---------- 渲染 ----------
  function render() {
    orderController?.cancel();
    clearPathDrag();
    closeMenu(false);
    const grid = byId('wq-grid'), query = searching();
    const entries = query ? searchResults() : siblings(current);
    byId('wq-path').classList.toggle('hidden', query);
    if (!query) renderPath();
    const focused = focusToken(document.activeElement, grid);
    const fragment = document.createDocumentFragment();
    entries.forEach(entry => fragment.append(entry.kind === 'folder' ? folderCard(entry.row) : itemCard(entry.row)));
    grid.replaceChildren(fragment);
    byId('wq-empty').classList.toggle('hidden', entries.length > 0);
    byId('wq-empty-text').textContent = emptyText();
    byId('wq-empty-add').classList.toggle('hidden', !(loaded && !query));
    byId('wq-count').textContent = countText(entries);
    restoreFocus(focused, grid);
  }
  function emptyText() {
    if (!loaded) return '正在读取…';
    if (searching()) return '没有匹配的名称或网址';
    return data.folders.length || data.items.length ? '这个文件夹是空的' : '还没有网址，点击“添加网址”开始使用';
  }
  function countText(entries) {
    if (!loaded) return '';
    if (searching()) return `找到 ${entries.length} 个结果`;
    const folders = entries.filter(entry => entry.kind === 'folder').length;
    const items = entries.length - folders;
    const parts = [folders ? `${folders} 个文件夹` : '', items ? `${items} 个网址` : ''].filter(Boolean);
    return parts.length ? `${current ? '本层' : '共'} ${parts.join('、')}` : '';
  }
  function focusToken(node, scope) {
    const card = node?.closest?.('.wq-card');
    if (!card || !scope.contains(card)) return null;
    const role = ['wq-drag', 'wq-more', 'wq-open', 'wq-card-body'].find(name => node.classList.contains(name));
    return { key: card.dataset.id, role: `.${role}` };
  }
  function restoreFocus(token, grid) {
    if (!token) return;
    [...grid.querySelectorAll('.wq-card')].find(node => node.dataset.id === token.key)
      ?.querySelector(token.role)?.focus();
  }
  function renderPath() {
    const parts = ancestors(current);
    const nodes = [pathButton('全部', null)];
    parts.forEach((row, index) => {
      nodes.push(icon('chevron'));
      if (index < parts.length - 1) nodes.push(pathButton(row.name, row.id));
      else {
        const current_node = element('span', 'wq-path-current', row.name);
        current_node.title = row.name;
        current_node.setAttribute('aria-current', 'page');
        nodes.push(current_node);
      }
    });
    byId('wq-path').replaceChildren(...nodes);
  }
  function pathButton(name, folderId) {
    const button = element('button', '', name);
    button.type = 'button';
    button.dataset.folder = folderId || '';
    button.title = name;
    return button;
  }
  function clearPathTarget() {
    byId('wq-path').querySelectorAll('.wq-path-drop').forEach(node => node.classList.remove('wq-path-drop'));
  }
  function clearPathDrag() {
    clearPathTarget();
    byId('wq-path').classList.remove('wq-path-dragging');
  }
  function pathDropTarget(event) {
    const button = event.target.closest('button'), sourceKey = orderController?.draggingKey();
    if (!button || !byId('wq-path').contains(button) || button.dataset.folder === undefined || !sourceKey) return null;
    const targetId = button.dataset.folder || null, [kind, id] = sourceKey.split(':');
    const source = (kind === 'folder' ? data.folders : data.items).find(row => row.id === id);
    if (!source) return null;
    const parentId = (kind === 'folder' ? source.parent_id : source.folder_id) || null;
    if (parentId === targetId) return null;
    if (kind === 'folder' && targetId && ancestors(targetId).some(row => row.id === id)) return null;
    return { button, sourceKey, targetKey: `folder:${targetId || ''}` };
  }

  function dragHandle(label) {
    const drag = element('button', 'wq-drag');
    drag.type = 'button';
    drag.draggable = true;
    drag.disabled = busy;
    drag.title = '拖动调整顺序';
    drag.setAttribute('aria-label', label);
    drag.setAttribute('aria-description', '拖动手柄调整顺序，或按空格选中，再用方向键移动，Enter 保存，Escape 取消');
    drag.setAttribute('aria-pressed', 'false');
    drag.append(icon('grip'));
    return drag;
  }
  function moreButton(entry) {
    const more = element('button', 'wq-more');
    more.type = 'button';
    more.setAttribute('aria-label', `${entry.row.name} 的操作`);
    more.setAttribute('aria-haspopup', 'menu');
    more.setAttribute('aria-expanded', 'false');
    more.append(icon('more'));
    more.addEventListener('click', () => openMenu(entry, more));
    return more;
  }
  function folderCard(row) {
    const card = element('article', 'wq-card wq-folder');
    card.dataset.id = keyOf('folder', row.id);
    card.setAttribute('role', 'listitem');
    const body = element('button', 'wq-card-body');
    body.type = 'button';
    body.setAttribute('aria-label', `打开文件夹 ${row.name}`);
    const head = element('div', 'wq-card-head');
    const avatar = element('span', 'wq-avatar');
    avatar.append(icon('folder'));
    const identity = element('div', 'wq-identity');
    const name = element('h3', '', row.name), count = element('p', '', folderSummary(row.id));
    name.title = row.name;
    identity.append(name, count);
    head.append(avatar, identity);
    const meta = element('div', 'wq-card-meta');
    if (searching()) meta.append(element('span', 'wq-location', `位置：${pathNames(row.parent_id || null).join(' › ')}`));
    body.append(head, meta);
    body.addEventListener('click', () => { byId('wq-search').value = ''; navigate(row.id); });
    card.append(dragHandle(`调整文件夹 ${row.name} 的顺序`), body, moreButton({ kind: 'folder', row }));
    return card;
  }
  function folderSummary(folderId) {
    const rows = siblings(folderId);
    return rows.length ? `${rows.length} 项` : '空文件夹';
  }
  function itemCard(row) {
    const card = element('article', `wq-card wq-item${row.platform ? ' wq-item-login' : ''}`);
    card.dataset.id = keyOf('item', row.id);
    card.setAttribute('role', 'listitem');
    const body = element('div', 'wq-card-body');
    const head = element('div', 'wq-card-head');
    const avatar = element('span', 'wq-avatar');
    loadCachedFavicon(avatar, row);
    const identity = element('div', 'wq-identity');
    const name = element('h3', '', row.name), url = element('p', '', row.url);
    name.title = row.name;
    url.title = row.url;
    identity.append(name, url);
    head.append(avatar, identity);
    const meta = element('div', 'wq-card-meta');
    if (row.platform) {
      const site = data.sites.find(item => item.id === row.platform);
      meta.append(element('span', 'wq-tag', site ? site.name : row.platform));
      if (row.username) {
        const account = element('span', 'wq-account', row.username);
        account.title = row.username;
        meta.append(account);
      }
    }
    if (searching()) meta.append(element('span', 'wq-location', `位置：${pathNames(row.folder_id || null).join(' › ')}`));
    const run = data.runs[row.id];
    if (run) {
      const status = element('span', `wq-run wq-run-${RUN_TONE[run.state] || 'muted'}`, runText(run));
      status.setAttribute('role', 'status');
      meta.append(status);
    }
    body.append(head, meta);
    const foot = element('div', 'wq-card-foot');
    const open = element('button', 'btn wq-open');
    open.type = 'button';
    open.append(icon(row.platform ? 'login' : 'open'), document.createTextNode('打开'));
    open.setAttribute('aria-label', `打开 ${row.name}`);
    open.addEventListener('click', () => void openItem(row, open));
    foot.append(open);
    card.append(dragHandle(`调整 ${row.name} 的顺序`), body, foot, moreButton({ kind: 'item', row }));
    return card;
  }
  function navigate(folderId) {
    if (folderId === current) return;
    current = folderId;
    render();
  }

  // ---------- 读取与状态同步 ----------
  async function refresh() {
    if (!active() || !backend()) return;
    if (fetching || busy || orderController?.active()) {
      refreshPending = true;
      return;
    }
    refreshPending = false;
    const requestEpoch = epoch;
    fetching = true;
    try {
      const result = await call(backend().get_web_quick(), '读取');
      if (requestEpoch !== epoch || !active()) return;
      const structure = value => JSON.stringify([value.folders, value.items, value.runs, value.auto_login_count]);
      const changed = !loaded || structure(data) !== structure(result);
      data = { ...data, ...result };
      loaded = true;
      if (current && !ancestors(current).length) current = null;
      feedback('wq-feedback');
      byId('wq-retry').classList.add('hidden');
      renderExtension();
      if (changed) render();
    } catch (error) {
      if (requestEpoch !== epoch) return;
      feedback('wq-feedback', friendlyError(error), 'error');
      byId('wq-empty-text').textContent = '读取失败，现有内容未修改';
      byId('wq-retry').classList.remove('hidden');
    } finally {
      fetching = false;
      flushRefresh();
    }
  }
  function flushRefresh() {
    if (refreshPending && active() && !fetching && !busy && !orderController?.active()) void refresh();
  }
  async function changed() {
    epoch++;
    await refresh();
  }
  function renderExtension() {
    const shown = data.auto_login_count > 0;
    byId('wq-extension').classList.toggle('hidden', !shown);
    if (!shown) return;
    const info = data.extension || {};
    let tone = 'warn', text = 'Edge 扩展尚未连接';
    if (!info.available) { tone = 'bad'; text = '当前运行方式不支持自动登录，请使用安装版'; }
    else if (!info.registered) { tone = 'bad'; text = '自动登录组件登记失败，请查看运行日志'; }
    else if (data.extension_seen > 0) { tone = 'ok'; text = 'Edge 扩展已连接'; }
    const status = byId('wq-extension-status');
    status.textContent = text;
    status.className = `wq-extension-status wq-run-${tone}`;
    byId('wq-extension-path').textContent = info.path || '';
    byId('wq-extension-copy').classList.toggle('hidden', !info.path);
  }
  function pageChanged() {
    orderController?.cancel();
    clearPathDrag();
    epoch++;
    closeMenu(false);
    if (active()) void refresh();
  }

  // ---------- 打开 ----------
  async function openItem(row, button) {
    if (opening.has(row.id)) return;
    opening.add(row.id);
    button.disabled = true;
    try {
      await call(backend().open_web_quick_item(row.id), '打开');
      toast({ ok: true, tone: row.platform ? 'info' : 'success',
        msg: row.platform ? '已在 Edge 中打开，正在自动登录' : '已交给默认浏览器打开' });
    } catch (error) { toast({ ok: false, msg: friendlyError(error) }); }
    finally {
      opening.delete(row.id);
      button.disabled = false;
      await refresh();
    }
  }

  // ---------- 排序 ----------
  function applyOrder(keys) {
    keys.forEach((key, index) => {
      const [kind, id] = key.split(':');
      const row = (kind === 'folder' ? data.folders : data.items).find(item => item.id === id);
      if (row) row.position = index;
    });
  }
  async function saveOrder(keys) {
    if (busy) return;
    busy = true;
    const requestEpoch = epoch, grid = byId('wq-grid'), parentId = current;
    grid.setAttribute('aria-busy', 'true');
    grid.querySelectorAll('.wq-drag').forEach(handle => { handle.disabled = true; });
    try {
      await call(backend().reorder_web_quick(parentId, keys), '保存顺序');
      if (requestEpoch !== epoch || !active()) return;
      applyOrder(keys);
      render();
      feedback('wq-feedback');
      byId('wq-order-status').textContent = '顺序已保存';
    } catch (error) {
      if (requestEpoch !== epoch || !active()) return;
      busy = false;
      await refresh();
      feedback('wq-feedback', friendlyError(error), 'error');
      byId('wq-order-status').textContent = '顺序未保存，已重新读取';
    } finally {
      busy = false;
      grid.setAttribute('aria-busy', 'false');
      grid.querySelectorAll('.wq-drag').forEach(handle => { handle.disabled = false; });
      flushRefresh();
    }
  }

  async function moveIntoFolder(sourceKey, targetKey) {
    if (busy) return;
    busy = true;
    const grid = byId('wq-grid');
    grid.setAttribute('aria-busy', 'true');
    grid.querySelectorAll('.wq-drag').forEach(handle => { handle.disabled = true; });
    let error = null;
    try {
      const targetId = targetKey.slice('folder:'.length) || null;
      await call(backend().move_web_quick_entry(sourceKey, targetId), '移动到文件夹');
    } catch (caught) { error = caught; }
    busy = false;
    grid.setAttribute('aria-busy', 'false');
    grid.querySelectorAll('.wq-drag').forEach(handle => { handle.disabled = false; });
    if (active()) await changed();
    const focusKey = error ? sourceKey : targetKey;
    const focusControl = [...grid.querySelectorAll('.wq-card')].find(card => card.dataset.id === focusKey)
      ?.querySelector(error ? '.wq-drag' : '.wq-card-body');
    if (focusControl) focusControl.focus();
    else if (!error) {
      const targetId = targetKey.slice('folder:'.length) || null;
      [...byId('wq-path').querySelectorAll('button')].find(button => (button.dataset.folder || null) === targetId)?.focus();
    }
    if (error) {
      feedback('wq-feedback', friendlyError(error), 'error');
      byId('wq-order-status').textContent = '移动失败，已重新读取';
    } else {
      feedback('wq-feedback');
      byId('wq-order-status').textContent = '已移动到文件夹';
    }
    flushRefresh();
  }

  // ---------- 操作菜单与删除 ----------
  function closeMenu(focus = true) {
    byId('wq-menu').classList.add('hidden');
    menuTrigger?.setAttribute('aria-expanded', 'false');
    if (focus && menuTrigger?.isConnected) menuTrigger.focus();
    menuTrigger = null;
    menuEntry = null;
  }
  function openMenu(entry, trigger) {
    const wasOpen = menuTrigger === trigger;
    closeMenu(false);
    if (wasOpen) return;
    menuEntry = entry;
    menuTrigger = trigger;
    trigger.setAttribute('aria-expanded', 'true');
    const menu = byId('wq-menu'), label = entry.kind === 'folder' ? '文件夹' : '网址';
    menu.querySelector('[data-wq-action="edit"]').textContent = `编辑${label}`;
    menu.querySelector('[data-wq-action="delete"]').textContent = `删除${label}`;
    menu.classList.remove('hidden');
    const rect = trigger.getBoundingClientRect();
    menu.style.left = `${Math.max(8, Math.min(innerWidth - menu.offsetWidth - 8, rect.right - menu.offsetWidth))}px`;
    menu.style.top = `${Math.max(8, Math.min(innerHeight - menu.offsetHeight - 8, rect.bottom + 5))}px`;
    menu.querySelector('button').focus();
  }
  async function remove(entry) {
    const { kind, row } = entry;
    let accepted;
    if (kind === 'item') {
      accepted = await confirmAction({ title: '删除网址', confirmText: '删除',
        message: `删除「${row.name}」？${row.platform ? '保存的账号和密码会一并删除。' : ''}` });
    } else {
      let summary;
      try { summary = await call(backend().web_quick_folder_summary(row.id), '统计'); }
      catch (error) { toast({ ok: false, msg: friendlyError(error) }); return; }
      const parts = [summary.folders > 1 ? `${summary.folders - 1} 个子文件夹` : '',
        summary.items ? `${summary.items} 个网址` : ''].filter(Boolean);
      accepted = await confirmAction({ title: '删除文件夹', confirmText: '删除文件夹',
        message: `删除「${row.name}」${parts.length ? `及其中的 ${parts.join('、')}` : ''}？` +
          (summary.logins ? '其中自动登录条目保存的账号和密码会一并删除。' : '') });
    }
    if (!accepted) return;
    const inside = kind === 'folder' && ancestors(current).some(item => item.id === row.id);
    try {
      const result = kind === 'item' ? await call(backend().delete_web_quick_item(row.id), '删除')
        : await call(backend().delete_web_quick_folder(row.id), '删除');
      if (inside) current = row.parent_id || null;
      toast({ ok: true, msg: result.msg || '已删除' });
      await changed();
    } catch (error) {
      toast({ ok: false, msg: friendlyError(error) });
      await changed();
    }
  }

  // ---------- 弹窗 ----------
  function modalOpen(id) {
    returnFocus = document.activeElement;
    byId(id).classList.remove('hidden');
  }
  async function modalClose(modal) {
    if (modal.id === 'wq-editor') {
      if (editorBusy) return;
      editorSession++;
      faviconSequence++;
      clearTimeout(faviconTimer);
      faviconAttempts = new Set();
      editorFavicon = null;
      byId('wq-form').reset();
      showFavicon(byId('wq-favicon-preview'), '', { icon: 'quick' });
      concealSecret(byId('wq-password'), byId('wq-password-eye'));
      byId('wq-password').value = '';
      editing = null;
    } else {
      if (folderBusy) return;
      byId('wq-folder-form').reset();
      folderEditing = null;
    }
    modal.classList.add('hidden');
    if (returnFocus?.isConnected) returnFocus.focus();
  }
  function option(label, value) {
    const node = document.createElement('option');
    node.value = value;
    node.textContent = label;
    return node;
  }
  // 下拉选项按目录树顺序缩进；exclude 为不允许出现的文件夹及其子树。
  function folderOptions(select, { exclude = null, selected = null, rootLabel }) {
    const list = [];
    const walk = (parentId, depth) => siblings(parentId).forEach(entry => {
      if (entry.kind !== 'folder' || ancestors(entry.row.id).some(item => item.id === exclude)) return;
      list.push(option('　'.repeat(depth) + entry.row.name, entry.row.id));
      walk(entry.row.id, depth + 1);
    });
    walk(null, 0);
    select.replaceChildren(option(rootLabel, ''), ...list);
    select.value = list.some(node => node.value === selected) ? selected : '';
  }

  // ---------- 条目编辑 ----------
  function openEditor(row = null) {
    editorSession++;
    editing = row;
    faviconAttempts = new Set();
    editorFavicon = null;
    byId('wq-form').reset();
    byId('wq-editor-title').textContent = row ? '编辑网址' : '添加网址';
    byId('wq-name').value = row?.name || '';
    byId('wq-url').value = row?.url || '';
    folderOptions(byId('wq-in-folder'), { selected: row ? row.folder_id || null : current, rootLabel: '不放入文件夹' });
    concealSecret(byId('wq-password'), byId('wq-password-eye'));
    byId('wq-password').disabled = false;
    byId('wq-password-eye').disabled = false;
    byId('wq-password').value = '';
    byId('wq-password').placeholder = row?.has_password ? '正在读取已保存密码…' : '';
    byId('wq-username').value = row?.username || '';
    mode = row?.platform ? 'login' : 'open';
    byId('wq-mode-hint').textContent = '';
    urlChanged();
    feedback('wq-editor-feedback');
    refreshSelects();
    modalOpen('wq-editor');
    byId('wq-name').focus();
    if (row?.has_password) void loadEditorPassword(row.id, editorSession);
    void loadOtpAccounts(row?.otp_account_id || '');
  }
  async function loadEditorPassword(itemId, session) {
    const input = byId('wq-password'), eye = byId('wq-password-eye');
    input.disabled = eye.disabled = true;
    try {
      const result = await call(backend().get_web_quick_item_password(itemId), '读取密码');
      if (session !== editorSession) return;
      input.value = typeof result.password === 'string' ? result.password : '';
      input.placeholder = '';
    } catch (error) {
      if (session !== editorSession) return;
      input.value = '';
      input.placeholder = '留空保留现有密码';
      feedback('wq-editor-feedback', `已保存密码读取失败：${friendlyError(error)}；留空保存可保留原密码`, 'warning');
    } finally {
      if (session === editorSession) input.disabled = eye.disabled = editorBusy;
    }
  }
  async function loadOtpAccounts(selected) {
    const session = editorSession;
    let accounts = [], note = '';
    try {
      // 只保留选项需要的标识；验证码不进入本工作区。
      accounts = (await call(backend().get_otp_accounts(), '读取 2FA 账号')).accounts
        .map(row => ({ id: row.id, label: `${row.issuer} · ${row.account}` }));
    } catch (error) { note = `未能读取 2FA 账号：${friendlyError(error)}`; }
    if (session !== editorSession) return;
    if (selected && !accounts.some(row => row.id === selected)) accounts.push({ id: selected, label: '原先选择的 2FA 账号' });
    const select = byId('wq-otp');
    select.replaceChildren(option('不使用动态口令', ''), ...accounts.map(row => option(row.label, row.id)));
    select.value = selected;
    refreshSelects();
    if (note) feedback('wq-editor-feedback', note, 'warning');
  }
  function applyEditorFavicon(result, url, preview) {
    if (result.found) {
      editorFavicon = { url, key: result.key, dataUrl: result.data_url };
      preview.dataset.faviconKey = result.key;
      cacheFavicon(result.key, result.data_url);
      showFavicon(preview, result.data_url, { icon: 'quick' });
      const row = data.items.find(item => item.id === editing?.id && item.favicon_key === result.key);
      if (row) row.has_favicon = true;
      if (editing?.favicon_key === result.key) editing.has_favicon = true;
    }
    preview.title = result.found ? '重新抓取网站图标' : '未能抓取网站图标，点击重试';
  }
  function scheduleFavicon(value) {
    const sequence = ++faviconSequence, session = editorSession;
    clearTimeout(faviconTimer);
    const preview = byId('wq-favicon-preview');
    preview.dataset.faviconKey = '';
    preview.title = '重新抓取网站图标';
    showFavicon(preview, '', { icon: 'quick' });
    let url;
    try {
      const parsed = new URL(String(value).trim());
      if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) return;
      url = parsed.href;
    } catch (error) { return; }
    if (editing?.has_favicon && String(value).trim() === String(editing.url).trim()) {
      loadCachedFavicon(preview, { ...editing, name: '' });
      return;
    }
    if (editorFavicon?.url === url) {
      preview.dataset.faviconKey = editorFavicon.key;
      showFavicon(preview, editorFavicon.dataUrl, { icon: 'quick' });
      return;
    }
    if (faviconAttempts.has(url)) {
      preview.title = '未能抓取网站图标，点击重试';
      return;
    }
    faviconTimer = setTimeout(() => {
      if (sequence !== faviconSequence || session !== editorSession) return;
      faviconAttempts.add(url);
      call(backend().fetch_web_quick_favicon(url, editing?.id || null), '抓取站点图标').then(result => {
        if (sequence !== faviconSequence || session !== editorSession ||
            String(byId('wq-url').value).trim() !== String(value).trim()) return;
        applyEditorFavicon(result, url, preview);
      }).catch(() => {
        if (sequence === faviconSequence && session === editorSession)
          preview.title = '未能抓取网站图标，点击重试';
      });
    }, 500);
  }
  async function refreshFavicon() {
    if (editorBusy) return;
    const sequence = ++faviconSequence, session = editorSession, value = byId('wq-url').value;
    clearTimeout(faviconTimer);
    const preview = byId('wq-favicon-preview');
    let url;
    try {
      const parsed = new URL(String(value).trim());
      if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) return;
      url = parsed.href;
    } catch (error) { return; }
    preview.disabled = true;
    try {
      const result = await call(backend().refresh_web_quick_favicon(url, editing?.id || null), '重新抓取图标');
      if (sequence !== faviconSequence || session !== editorSession ||
          String(byId('wq-url').value).trim() !== String(value).trim()) return;
      applyEditorFavicon(result, url, preview);
    } catch (error) {
      if (sequence === faviconSequence && session === editorSession)
        preview.title = '未能抓取网站图标，点击重试';
    } finally { if (session === editorSession) preview.disabled = false; }
  }
  function syncMode() {
    const site = platformFor(byId('wq-url').value);
    ['open', 'login'].forEach(name => {
      const button = byId(`wq-mode-${name}`);
      button.classList.toggle('selected', name === mode);
      button.setAttribute('aria-pressed', String(name === mode));
    });
    byId('wq-mode-login').disabled = !site || editorBusy;
    byId('wq-login-fields').classList.toggle('hidden', mode !== 'login');
    byId('wq-username').required = mode === 'login';
    byId('wq-password').required = mode === 'login' && !editing?.has_password;
  }
  function urlChanged(updateFavicon = true) {
    const site = platformFor(byId('wq-url').value), hint = byId('wq-mode-hint');
    if (mode === 'login' && !site) {
      mode = 'open';
      hint.textContent = `这个网址暂不支持自动登录，目前仅支持 ${
        data.sites.map(item => `${item.name}（${item.origin}）`).join('、')}`;
    } else if (mode === 'login') {
      hint.textContent = `将用 Edge 打开 ${site.origin} 并自动填写登录信息；出现滑块验证时手动完成即可。`;
    } else hint.textContent = '';
    syncMode();
    if (updateFavicon) scheduleFavicon(byId('wq-url').value);
  }
  function setEditorBusy(value) {
    editorBusy = value;
    byId('wq-editor').querySelectorAll('button,input,select').forEach(control => { control.disabled = value; });
    syncMode();
    refreshSelects();
  }
  async function save(event) {
    event.preventDefault();
    if (editorBusy) return;
    setEditorBusy(true);
    const login = mode === 'login';
    try {
      const result = await call(backend().save_web_quick_item({
        id: editing?.id, name: byId('wq-name').value, url: byId('wq-url').value,
        folder_id: byId('wq-in-folder').value || null, auto_login: login,
        username: login ? byId('wq-username').value : '', password: login ? byId('wq-password').value : '',
        otp_account_id: login ? byId('wq-otp').value || null : null,
      }), '保存');
      editorBusy = false;
      await modalClose(byId('wq-editor'));
      toast({ ok: true, msg: result.msg || '已保存' });
      await changed();
    } catch (error) { feedback('wq-editor-feedback', friendlyError(error), 'error'); }
    finally { setEditorBusy(false); }
  }

  // ---------- 文件夹编辑 ----------
  function openFolderDialog(row = null) {
    folderEditing = row;
    byId('wq-folder-form').reset();
    byId('wq-folder-title').textContent = row ? '编辑文件夹' : '新建文件夹';
    byId('wq-folder-name').value = row?.name || '';
    folderOptions(byId('wq-parent'), { exclude: row?.id || null,
      selected: row ? row.parent_id || null : current, rootLabel: '放在顶层' });
    feedback('wq-folder-feedback');
    refreshSelects();
    modalOpen('wq-folder-dialog');
    byId('wq-folder-name').focus();
  }
  function setFolderBusy(value) {
    folderBusy = value;
    byId('wq-folder-dialog').querySelectorAll('button,input,select').forEach(control => { control.disabled = value; });
    refreshSelects();
  }
  async function saveFolder(event) {
    event.preventDefault();
    if (folderBusy) return;
    setFolderBusy(true);
    try {
      const result = await call(backend().save_web_quick_folder({
        id: folderEditing?.id, name: byId('wq-folder-name').value, parent_id: byId('wq-parent').value || null }), '保存');
      folderBusy = false;
      await modalClose(byId('wq-folder-dialog'));
      toast({ ok: true, msg: result.msg || '已保存' });
      await changed();
    } catch (error) { feedback('wq-folder-feedback', friendlyError(error), 'error'); }
    finally { setFolderBusy(false); }
  }
  async function copyExtensionPath() {
    try {
      await writeClipboardText(byId('wq-extension-path').textContent);
      toast({ ok: true, msg: '已复制扩展文件夹路径' });
    } catch (error) { toast({ ok: false, msg: friendlyError(error) }); }
  }
  async function openExtensions() {
    const button = byId('wq-extension-open');
    if (button.disabled) return;
    button.disabled = true;
    try {
      const result = await call(backend().open_web_quick_extensions(), '打开扩展管理页');
      toast({ ok: true, msg: result.msg || '已交给 Edge 打开扩展管理页' });
    } catch (error) { toast({ ok: false, msg: friendlyError(error) }); }
    finally { button.disabled = false; }
  }

  function bind() {
    orderController = window.TwoFactorOrder?.attach(byId('wq-grid'), {
      card: '.wq-card', handle: '.wq-drag', prefix: 'wq',
      selectedMessage: '已选中，方向键移动，Enter 保存，Escape 取消',
      visible: () => siblings(current).map(entry => entry.key),
      all: () => siblings(current).map(entry => entry.key),
      ready: () => active() && loaded && !fetching && !busy && !searching(),
      start: () => { closeMenu(false); byId('wq-order-status').textContent = ''; },
      into: {
        target: (card, event) => card.classList.contains('wq-folder') && Boolean(event.target.closest('.wq-card-body')),
        save: moveIntoFolder,
      },
      save: saveOrder, announce: message => { byId('wq-order-status').textContent = message; },
    });
    byId('wq-grid').addEventListener('dragstart', () => {
      if (current && orderController?.draggingKey()) byId('wq-path').classList.add('wq-path-dragging');
    });
    ['dragend', 'drop'].forEach(name => byId('wq-grid').addEventListener(
      name, () => { clearPathDrag(); setTimeout(flushRefresh, 0); }));
    byId('wq-grid').addEventListener('keydown', event => {
      if (['Escape', 'Tab', 'Enter', ' '].includes(event.key)) setTimeout(flushRefresh, 0);
    });
    byId('wq-search').addEventListener('input', render);
    byId('wq-add').addEventListener('click', () => openEditor());
    byId('wq-empty-add').addEventListener('click', () => openEditor());
    byId('wq-retry').addEventListener('click', () => void refresh());
    byId('wq-new-folder').addEventListener('click', () => openFolderDialog());
    byId('wq-path').addEventListener('dragover', event => {
      clearPathTarget();
      const target = pathDropTarget(event);
      if (!target) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      target.button.classList.add('wq-path-drop');
    });
    byId('wq-path').addEventListener('drop', event => {
      const target = pathDropTarget(event);
      if (!target) { clearPathTarget(); return; }
      event.preventDefault();
      orderController?.cancel();
      clearPathDrag();
      void moveIntoFolder(target.sourceKey, target.targetKey);
    });
    byId('wq-path').addEventListener('dragleave', clearPathTarget);
    byId('wq-path').addEventListener('click', event => {
      const button = event.target.closest('button[data-folder]');
      if (!button) return;
      byId('wq-search').value = '';
      navigate(button.dataset.folder || null);
    });
    byId('wq-extension-copy').addEventListener('click', () => void copyExtensionPath());
    byId('wq-extension-open').addEventListener('click', () => void openExtensions());
    byId('wq-form').addEventListener('submit', save);
    byId('wq-editor-cancel').addEventListener('click', () => void modalClose(byId('wq-editor')));
    byId('wq-url').addEventListener('input', urlChanged);
    byId('wq-favicon-preview').addEventListener('click', () => void refreshFavicon());
    ['open', 'login'].forEach(name => byId(`wq-mode-${name}`).addEventListener('click', () => { mode = name; urlChanged(false); }));
    byId('wq-password-eye').addEventListener('click', () => toggleSecret(byId('wq-password'), byId('wq-password-eye')));
    byId('wq-folder-form').addEventListener('submit', saveFolder);
    byId('wq-folder-cancel').addEventListener('click', () => void modalClose(byId('wq-folder-dialog')));
    byId('wq-menu').addEventListener('click', async event => {
      const action = event.target.closest('[data-wq-action]')?.dataset.wqAction, entry = menuEntry;
      if (!entry || !action) return;
      closeMenu();
      if (action === 'edit') {
        if (entry.kind === 'folder') openFolderDialog(entry.row); else openEditor(entry.row);
        return;
      }
      await remove(entry);
    });
    document.addEventListener('click', event => {
      if (!event.target.closest('#wq-menu,.wq-more')) closeMenu(false);
    });
    document.addEventListener('keydown', event => {
      if (!byId('wq-menu').classList.contains('hidden')) {
        const buttons = [...byId('wq-menu').querySelectorAll('button')];
        const index = buttons.indexOf(document.activeElement);
        if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
          event.preventDefault(); event.stopImmediatePropagation();
          buttons[event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
            : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length].focus();
        }
        if (event.key === 'Escape' || event.key === 'Tab') { closeMenu(event.key === 'Escape'); if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); } }
      }
      const modal = ['wq-editor', 'wq-folder-dialog'].map(byId).find(node => !node.classList.contains('hidden'));
      if (!modal || !byId('confirmmodal').classList.contains('hidden')) return;
      if (event.key === 'Escape' && !document.querySelector('.routing-select-menu:not(.hidden)')) {
        event.preventDefault(); event.stopImmediatePropagation(); void modalClose(modal);
      }
      if (event.key === 'Tab') {
        const focusable = [...modal.querySelectorAll('button,input,summary,[tabindex="0"]')].filter(node => !node.disabled && node.getClientRects().length && node.tabIndex >= 0);
        const target = event.shiftKey ? focusable.at(-1) : focusable[0];
        if (document.activeElement === (event.shiftKey ? focusable[0] : focusable.at(-1))) { event.preventDefault(); target?.focus(); }
      }
    }, true);
    window.addEventListener('resize', () => closeMenu(false));
    document.addEventListener('scroll', () => closeMenu(false), true);
    window.addEventListener('cxvpn:uistate', event => {
      const value = Number(event.detail?.web_quick_revision ?? -1);
      if (value === revision) return;
      revision = value;
      if (loaded) void refresh();
    });
    window.addEventListener('cxvpn:pagechange', pageChanged);
    document.addEventListener('visibilitychange', pageChanged);
    window.addEventListener('focus', () => { if (active()) void refresh(); });
    window.addEventListener('pywebviewready', () => { refreshSelects(); if (active()) pageChanged(); });
  }
  window.WebQuickWorkspace = { closeModal: modalClose };
  bind();
})();

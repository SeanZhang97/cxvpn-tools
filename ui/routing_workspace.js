/* 将同一份分流配置工作区投放到一级页面，避免复制草稿和保存状态。 */
(() => {
  const byId = id => document.getElementById(id);
  const savebar = document.querySelector('.routing-savebar');
  const routingPage = byId('page-routing');

  function mountPanel(panelId, mountId) {
    const panel = byId(panelId);
    const mount = byId(mountId);
    if (!panel || !mount) return;
    panel.classList.remove('routing-tab-panel');
    panel.classList.add('routing-workspace-panel');
    panel.removeAttribute('aria-hidden');
    panel.removeAttribute('aria-labelledby');
    panel.removeAttribute('role');
    panel.removeAttribute('data-routing-panel');
    mount.appendChild(panel);
  }

  function mountNodes() {
    const nodes = document.querySelector('[data-proxy-view="nodes"]');
    const mount = byId('routing-nodes-mount');
    if (!nodes || !mount) return;
    nodes.classList.remove('proxy-view');
    nodes.classList.add('routing-node-workspace');
    nodes.removeAttribute('data-proxy-view');
    mount.appendChild(nodes);
  }

  function placeSavebar(page) {
    if (!savebar) return;
    const target = page === 'subscriptions' ? byId('page-subscriptions')
      : page === 'rules' ? byId('page-rules')
        : page === 'routing' ? routingPage : null;
    if (target) target.appendChild(savebar);
  }

  mountPanel('routing-panel-providers', 'routing-subscriptions-mount');
  mountPanel('routing-panel-rules', 'routing-rules-mount');
  mountNodes();
  placeSavebar('routing');
  window.addEventListener('cxvpn:pagechange', event => {
    placeSavebar(event.detail?.page || '');
  });

  window.RoutingPageMounts = { placeSavebar };
})();

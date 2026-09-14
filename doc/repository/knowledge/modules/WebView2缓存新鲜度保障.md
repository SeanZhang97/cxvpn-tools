# WebView2 缓存新鲜度保障

模块：UI 静态资源缓存新鲜度保障  
关键词：`pywebview`、`bottle`、`static_file`、`no-cache`、`WebView2`、启发式缓存、固定端口 42001、版本标记、升级旧页面

代码路径：`core/ui_cache.py`、`main.py`、`tests/test_ui_cache.py`

最后验证：2026-09-14 | 分支：main

## 当前事实

- pywebview 在 `private_mode=False`（本项目固定如此）时用内置 bottle 服务器提供 UI 静态资源，端口取 `settings['DEFAULT_HTTP_PORT']`（42001，固定不变）；窗口 URL 形如 `http://127.0.0.1:42001/index.html`，升级前后 URL 不变。
- pywebview 路由里设置的 no-cache 响应头实际无效：bottle 的 `static_file` 返回独立 `HTTPResponse`，其 `apply()` 以 `other._headers = self._headers` 整体替换线程本地响应头，pywebview 先设置的头被丢弃（实测响应头为空）。WebView2 对无缓存控制头的资源按启发式策略缓存，升级后窗口会命中旧版 HTML/CSS/JS，表现为新菜单不出现、页面布局退化。
- `core/ui_cache.apply_no_cache_patch()` 包装 `bottle.static_file`，把 `Cache-Control: no-cache, no-store, must-revalidate` 与 `Pragma: no-cache` 补到返回的 `HTTPResponse` 上。pywebview 资源路由在请求时以模块属性形式调用 `bottle.static_file`，进程内持续替换该属性即可生效，不修改 pywebview 内部实现；幂等，响应头设置失败不影响资源返回。
- `core/ui_cache.purge_stale_webview_cache(storage_path, version)` 处理存量缓存：在 `webview_data` 下维护 `ui-cache-version.txt` 标记，版本变化或首次运行（无标记）时删除 `EBWebView\Default\Cache` 与 `EBWebView\Default\Code Cache`；同版本重复启动不动缓存。必须在 `webview.start()` 之前调用（WebView2 启动后锁定缓存目录）。
- 清理只删 HTTP 缓存目录，不触碰 Cookie、本地存储、IndexedDB 等登录态数据；清理结果（purged/reason/removed/errors）经 log 回调写入 startup.log。
- `main.py` 的 `_run_app()` 在创建 API 之前依次调用两层防护；版本号取自 `core/version.APP_VERSION`。

## 边界

- 补丁只作用于 bottle 的 `static_file`；js_api 路由等非静态资源响应不受影响。pywebview 升级后若改为不经 `static_file` 提供资源，补丁退化为无害空操作，版本标记清理仍兜底。
- 版本标记文件损坏或缺失时按 first-run 处理，多清一次缓存，无功能影响。
- 内嵌浏览器面板与主窗口共用 `webview_data` 存储；清缓存后其页面需重新下载，登录态不受影响。

## 离线验证

`tests/test_ui_cache.py` 覆盖：补丁加头（Cache-Control/Pragma）、幂等、响应内容保持、404 边界；清理的同版本跳过、版本变化只删两个 HTTP 缓存目录且保留 Cookies、首启清理并写标记、空 storage 路径、log 回调摘要、缓存目录缺失仍写标记。

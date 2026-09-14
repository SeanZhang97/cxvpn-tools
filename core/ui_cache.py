# -*- coding: utf-8 -*-
"""WebView2 UI 资源缓存新鲜度保障。

pywebview 在非 private 模式下用固定端口（默认 42001）的内置 bottle
服务器提供 UI 静态资源。pywebview 虽然在路由里设置了 no-cache 响应
头，但 bottle 的 static_file 返回独立 HTTPResponse，其 apply() 会
整体替换线程本地响应头，导致 no-cache 实际丢失（实测响应头为空）。
WebView2 对无缓存控制头的资源按启发式策略缓存，软件升级后端口与
URL 不变，窗口会继续显示旧版 HTML/CSS/JS。

本模块两层防护：
1. apply_no_cache_patch: 包装 bottle.static_file，把 no-cache 头补到
   返回的 HTTPResponse 上（对修复后的新响应生效）；
2. purge_stale_webview_cache: 检测 UI 版本变化时删除 WebView2 的
   HTTP 缓存目录（对升级前已写入的存量缓存生效），不触碰
   Cookie/本地存储等登录态数据。
"""
from __future__ import annotations

import os
import shutil

import bottle

CACHE_MARKER_NAME = 'ui-cache-version.txt'
WEBVIEW_HTTP_CACHE_DIRS = (
    os.path.join('EBWebView', 'Default', 'Cache'),
    os.path.join('EBWebView', 'Default', 'Code Cache'),
)
NO_CACHE_HEADERS = (
    ('Cache-Control', 'no-cache, no-store, must-revalidate'),
    ('Pragma', 'no-cache'),
)

_no_cache_patched = False


def apply_no_cache_patch():
    """给内置 HTTP 服务的静态资源响应补回 no-cache 头。

    pywebview 的资源路由在请求时以模块属性形式调用
    bottle.static_file，因此进程内持续替换该属性即可，无需修改
    pywebview 内部实现。幂等，重复调用无副作用。
    """
    global _no_cache_patched
    if _no_cache_patched:
        return True
    original = bottle.static_file

    def _static_file_no_cache(*args, **kwargs):
        response = original(*args, **kwargs)
        try:
            for header, value in NO_CACHE_HEADERS:
                response.set_header(header, value)
        except Exception:
            # 响应头设置失败不影响资源返回，避免拖垮页面加载。
            pass
        return response

    bottle.static_file = _static_file_no_cache
    _no_cache_patched = True
    return True


def is_no_cache_patch_applied():
    return _no_cache_patched


def purge_stale_webview_cache(storage_path, version, log=None):
    """UI 版本变化时删除 WebView2 的 HTTP 缓存目录。

    必须在 webview.start() 之前调用（WebView2 启动后会锁定缓存
    目录）。同版本重复启动不动缓存；首次运行（无标记）也会清理
    一次以清除历史版本遗留。返回结果字典供日志输出。
    """
    result = {
        'purged': False,
        'reason': '',
        'previous': '',
        'removed': [],
        'errors': [],
    }
    if not storage_path:
        result['reason'] = 'no-storage-path'
        return result
    storage_path = os.path.abspath(storage_path)
    current = str(version or '').strip()
    marker = os.path.join(storage_path, CACHE_MARKER_NAME)
    previous = ''
    try:
        with open(marker, encoding='utf-8') as stream:
            previous = stream.read().strip()
    except OSError:
        previous = ''
    result['previous'] = previous
    if current and previous == current:
        result['reason'] = 'same-version'
        return result

    for relative in WEBVIEW_HTTP_CACHE_DIRS:
        target = os.path.join(storage_path, relative)
        if not os.path.isdir(target):
            continue
        try:
            shutil.rmtree(target)
            result['removed'].append(relative)
        except OSError as exc:
            result['errors'].append(
                f'{target}: {type(exc).__name__}: {exc}')
    try:
        os.makedirs(storage_path, exist_ok=True)
        with open(marker, 'w', encoding='utf-8') as stream:
            stream.write(current)
    except OSError as exc:
        result['errors'].append(
            f'{marker}: {type(exc).__name__}: {exc}')

    result['reason'] = 'version-changed' if previous else 'first-run'
    result['purged'] = not result['errors']
    if log:
        try:
            log(f'webview cache purge: {result}')
        except Exception:
            pass
    return result

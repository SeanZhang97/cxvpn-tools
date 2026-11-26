# -*- coding: utf-8 -*-
"""网页快开桥接：文件夹与条目维护、自动登录任务；列表和状态不返回密码或动态口令。"""
from __future__ import annotations

import os
import sys
import time
import traceback

from core.app_paths import resource_root
from core.two_factor import TwoFactorError
from core.web_quick import AUTO_LOGIN_SITES, WebQuickError, platform_for_url
from core import web_quick, web_quick_favicon, web_quick_host, web_quick_login, web_quick_pipe


class WebQuickApi:
    def _init_web_quick(self, root):
        self._wq_store = web_quick.Store(root)
        self._web_quick_revision = 0
        self._wq_registered = False
        self._wq_manager = web_quick_login.LoginManager(self._wq_store, self.log)
        self._wq_favicons = web_quick_favicon.FaviconService(root, self.log)
        self._wq_pipe = web_quick_pipe.PipeServer(self._web_quick_pipe_request, self.log)

    def _start_web_quick(self):
        # 原生消息宿主必须是可执行文件，源码运行时不登记，仅打包版可用自动登录。
        if getattr(sys, 'frozen', False):
            manifest = os.path.join(resource_root(), 'extension', 'manifest.json')
            try:
                result = web_quick_host.register(
                    self._wq_store.root, sys.executable,
                    web_quick_host.extension_id(manifest))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                result = {'ok': False, 'error': type(exc).__name__}
            self._wq_registered = result['ok']
            outcome = '成功' if result['ok'] else f'失败（{result.get("error")}）'
            self.log(f'[web-quick] Edge 原生消息宿主登记{outcome}')
        self._wq_pipe.start()

    def _stop_web_quick(self):
        self._wq_pipe.stop()

    def _web_quick_version(self):
        return self._web_quick_revision + self._wq_manager.changes

    def _web_quick_changed(self, result):
        self._web_quick_revision += 1
        return result

    def _wq_call(self, label, operation, quiet=False):
        started = time.monotonic()
        if not quiet:
            self.log(f'[web-quick] {label}请求已提交，桥接后台已领取，开始执行')
        try:
            result = operation()
            if not quiet:
                self.log(f'[web-quick] {label}{"成功" if result.get("ok", True) else "未完成"}，'
                         f'耗时 {time.monotonic() - started:.2f} 秒')
            return result
        except (WebQuickError, TwoFactorError) as exc:
            self.log(f'[web-quick] {label}失败：{exc}，耗时 {time.monotonic() - started:.2f} 秒')
            return {'ok': False, 'msg': str(exc)}
        except Exception as exc:
            self.log(f'[web-quick] {label}失败：{type(exc).__name__}，'
                     f'耗时 {time.monotonic() - started:.2f} 秒\n'
                     f'{"".join(traceback.format_tb(exc.__traceback__))}')
            return {'ok': False, 'msg': f'{label}失败，请重试或查看运行日志'}

    # ---------- Edge 扩展管道 ----------
    def _web_quick_pipe_request(self, request):
        if not isinstance(request, dict):
            return {'ok': False, 'error': 'bad_request'}
        kind = request.get('type')
        if kind == 'hello':
            return self._wq_manager.hello()
        if kind == 'claim':
            return self._web_quick_claim(request)
        if kind == 'report':
            return self._web_quick_report(request)
        return {'ok': False, 'error': 'bad_request'}

    def _web_quick_claim(self, request):
        platform, href = request.get('platform'), request.get('href')
        raw_run_id = request.get('run_id')
        run_id = raw_run_id or None
        try:
            # 领取与整页续接只面向登记站点自身的页面，其余来源一律不发放登录信息。
            matched = platform in AUTO_LOGIN_SITES and isinstance(href, str) and \
                len(href) <= 2048 and platform_for_url(href) == platform and \
                (raw_run_id in (None, '') or isinstance(raw_run_id, str) and len(raw_run_id) <= 64)
        except (ValueError, TypeError):
            matched = False
        if not matched:
            self.log('[web-quick] 自动登录管道拒绝领取请求：站点或页面地址不在允许范围')
            return {'ok': False, 'error': 'forbidden'}
        self._wq_manager.hello()
        return self._wq_manager.claim(platform, request.get('login') is True, run_id=run_id)

    def _web_quick_report(self, request):
        run_id, state, detail = request.get('run_id'), request.get('state'), request.get('detail')
        platform = request.get('platform')
        if (not isinstance(run_id, str) or len(run_id) > 64 or
                state not in web_quick_login.REPORT_STATES or
                not isinstance(detail, str) or len(detail) > 600 or
                platform is not None and platform not in AUTO_LOGIN_SITES):
            return {'ok': False, 'error': 'bad_request'}
        self._wq_manager.hello()
        return self._wq_manager.report(run_id, state, detail, platform=platform)

    def _web_quick_extension(self):
        available = bool(getattr(sys, 'frozen', False)) and os.path.isfile(
            os.path.join(resource_root(), 'extension', 'manifest.json'))
        return {'available': available, 'registered': self._wq_registered,
                'path': os.path.join(resource_root(), 'extension') if available else ''}

    # ---------- 界面桥接 ----------
    def get_web_quick(self):
        def operation():
            data = self._wq_store.listing()
            data.update(self._wq_manager.status())
            for item in data['items']:
                try:
                    item['favicon_key'] = web_quick_favicon.favicon_key(item['url'])
                except WebQuickError:
                    item['favicon_key'] = ''
            data['extension'] = self._web_quick_extension()
            data['sites'] = [{'id': key, 'name': label, 'origin': origin}
                             for key, (label, origin) in AUTO_LOGIN_SITES.items()]
            return data
        return self._wq_call('读取网页快开', operation, quiet=True)

    def _ensure_web_quick_favicon(self, url, item_id, force=False):
        result = self._wq_favicons.ensure(url, force=force)
        if item_id and result.get('found'):
            marked = self._wq_store.set_favicon(item_id, result['key'], True)
            if marked['changed']:
                self._web_quick_changed(marked)
        return result

    def fetch_web_quick_favicon(self, url, item_id=None):
        return self._wq_call('抓取站点图标',
                             lambda: self._ensure_web_quick_favicon(url, item_id), quiet=True)

    def refresh_web_quick_favicon(self, url, item_id=None):
        return self._wq_call('重试站点图标',
                             lambda: self._ensure_web_quick_favicon(url, item_id, force=True), quiet=True)

    def get_cached_web_quick_favicon(self, key):
        return self._wq_call('读取站点图标缓存', lambda: self._wq_favicons.cached(key), quiet=True)

    def get_web_quick_item_password(self, item_id):
        return self._wq_call('读取编辑密码', lambda: self._wq_store.password_for_edit(item_id))

    def save_web_quick_item(self, payload):
        def operation():
            result = self._wq_store.save_item(payload)
            key = web_quick_favicon.favicon_key(payload['url'])
            if self._wq_favicons.cached(key)['found']:
                self._wq_store.set_favicon(result['id'], key, True)
            return result
        return self._web_quick_changed(self._wq_call('保存条目', operation))

    def delete_web_quick_item(self, item_id):
        return self._web_quick_changed(
            self._wq_call('删除条目', lambda: self._wq_store.delete_item(item_id)))

    def save_web_quick_folder(self, payload):
        return self._web_quick_changed(
            self._wq_call('保存文件夹', lambda: self._wq_store.save_folder(payload)))

    def web_quick_folder_summary(self, folder_id):
        return self._wq_call('统计文件夹', lambda: self._wq_store.subtree(folder_id), quiet=True)

    def delete_web_quick_folder(self, folder_id):
        return self._web_quick_changed(
            self._wq_call('删除文件夹', lambda: self._wq_store.delete_folder(folder_id)))

    def reorder_web_quick(self, parent_id, keys):
        return self._web_quick_changed(
            self._wq_call('保存顺序', lambda: self._wq_store.reorder(parent_id, keys)))

    def move_web_quick_entry(self, key, target_folder_id):
        return self._web_quick_changed(
            self._wq_call('移动到文件夹', lambda: self._wq_store.move_entry(key, target_folder_id)))

    def open_web_quick_extensions(self):
        def operation():
            self.log('[web-quick] 扩展管理页请求新窗口并最大化，窗口导航等待上限 6 秒')
            try:
                web_quick_login.open_url('edge://extensions', edge=True,
                                         new_window=True, maximized=True)
            except FileNotFoundError:
                raise WebQuickError('未找到 Microsoft Edge，无法打开扩展管理页') from None
            except OSError as exc:
                raise WebQuickError('未能启动 Microsoft Edge，请重试') from exc
            return {'ok': True, 'msg': '已交给 Edge 打开扩展管理页'}
        return self._wq_call('打开 Edge 扩展管理页', operation)

    def open_web_quick_item(self, item_id):
        def operation():
            item = next((row for row in self._wq_store.listing()['items']
                         if row['id'] == item_id), None)
            if item is None:
                raise WebQuickError('条目已删除，请刷新列表')
            return self._wq_manager.open(item)
        return self._wq_call('打开条目', operation)

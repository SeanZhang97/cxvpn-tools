# -*- coding: utf-8 -*-
"""main.py - 应用入口 (pywebview 桌面窗口 + Web UI)"""
import os
import json
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import app_paths

_DATA_MIGRATION = app_paths.migrate_legacy_user_data()

import webview

from api import Api
from core.windows_desktop import (
    DesktopController, SingleInstanceGuard, WINDOW_TITLE,
    activate_existing_window, migrate_legacy_startup_registration)

BASE = app_paths.resource_root()
DATA_ROOT = app_paths.user_data_root()
_BOOT = time.time()


def _boot_log(msg):
    """启动耗时落盘 (startup.log, 追加): 开局假死时据此定位卡点"""
    try:
        os.makedirs(DATA_ROOT, exist_ok=True)
        with open(os.path.join(DATA_ROOT, 'startup.log'), 'a',
                  encoding='utf-8') as f:
            f.write(f'{time.time() - _BOOT:6.2f}s {msg}\n')
    except OSError:
        pass


def _run_app():
    startup_mode = '--startup' in sys.argv[1:]
    api = Api()
    ui = os.path.join(BASE, 'ui', 'index.html')
    win = webview.create_window(
        WINDOW_TITLE, ui, js_api=api,
        width=1440, height=900, min_size=(1080, 680),
        hidden=True,
        background_color='#0b1020')
    # 内嵌浏览器面板宿主 = 主窗口; 共用存储目录 (cookie 持久化/共享)
    storage = os.path.join(DATA_ROOT, 'webview_data')
    api.worker.main_window = win
    api.worker.storage_path = storage

    def exit_app():
        api.shutdown()
        if api.worker.is_alive():
            api.worker.join(timeout=5)
        try:
            win.destroy()
        except Exception:
            pass

    def open_page(page):
        safe_page = json.dumps(str(page or 'overview'))
        win.evaluate_js(f'void goToPage({safe_page})')

    def notify_desktop_action():
        win.evaluate_js(
            "window.dispatchEvent(new CustomEvent('cxvpn:desktop-action'))")

    desktop = DesktopController(
        win,
        close_to_tray=lambda: api._cfg_get().get('close_to_tray', True),
        on_exit=exit_app,
        on_os_shutdown=api._os_shutdown_cleanup,
        quick_snapshot=api.desktop_quick_snapshot,
        quick_actions={
            'toggle_proxy': api.desktop_toggle_routing,
            'toggle_mode': api.desktop_toggle_traffic_mode,
            'set_mode': api.desktop_set_traffic_mode,
            'select_node': api.desktop_select_node,
        },
        hotkeys_enabled=lambda: api._cfg_get().get(
            'global_hotkeys_enabled', False),
        on_open_page=open_page,
        on_action_complete=notify_desktop_action,
        log=api.log)
    api._attach_desktop(desktop)
    initial_ui_ready = threading.Event()

    def reveal_initial_window():
        """首屏已用本地配置填充后再显示，避免启动时闪过空白窗口。"""
        if initial_ui_ready.is_set():
            return
        initial_ui_ready.set()
        _boot_log('initial UI ready')
        if not startup_mode:
            win.show()

    api._attach_ui_ready(reveal_initial_window)

    def on_shown():
        _boot_log('main window shown')

    def on_loaded():
        _boot_log('main window loaded')
        try:
            desktop.start()
            if startup_mode:
                desktop.hide_window()
        except Exception as e:
            api.log(f'[desktop] 托盘启动失败: {e}')
            if startup_mode:
                win.show()

        # 托盘就绪后投递启动自检结果（如清除了残留系统代理），不弹窗口。
        api.flush_pending_desktop_notice()

        if not startup_mode:
            def reveal_on_timeout():
                if not initial_ui_ready.wait(5):
                    api.log('[desktop] 首屏就绪通知超时，使用窗口显示兜底')
                    reveal_initial_window()

            threading.Thread(target=reveal_on_timeout, daemon=True).start()

    win.events.shown += on_shown
    win.events.loaded += on_loaded
    api.start()
    _boot_log('worker started, webview.start()')
    try:
        webview.start(private_mode=False, storage_path=storage)
    finally:
        api.shutdown()
        if api.worker.is_alive():
            api.worker.join(timeout=5)
        desktop.dispose()


def main():
    _boot_log('imports done')
    if _DATA_MIGRATION['copied']:
        _boot_log(
            f'user data migrated: {len(_DATA_MIGRATION["copied"])} files')
    for warning in _DATA_MIGRATION['warnings']:
        _boot_log(f'user data migration warning: {warning}')
    instance = SingleInstanceGuard()
    if not instance.acquire():
        restored = activate_existing_window()
        _boot_log(f'second instance blocked, restored={restored}')
        return
    try:
        if migrate_legacy_startup_registration():
            _boot_log('legacy startup registration migrated to CXVPNTools')
        _run_app()
    finally:
        instance.close()


if __name__ == '__main__':
    main()

# -*- coding: utf-8 -*-
"""2FA 专用桥接；不将密钥放入公共配置或全局 UI 状态流。"""
import base64
import os
import threading
import time
import traceback
import uuid

from core.two_factor import AccountStore, TwoFactorError, normalize
from core import two_factor_backup as backup
from core import two_factor_qr as qr
from core.two_factor_migration import MigrationBatch, MAX_PAGES


class TwoFactorApi:
    def _init_two_factor(self, root):
        self._otp_store = AccountStore(root)
        self._otp_transfer_lock = threading.Lock()
        self._otp_pending = None
        self._otp_google = None

    def _otp_call(self, label, operation, quiet=False):
        started = time.monotonic()
        if not quiet:
            self.log(f'[2fa] {label}请求已提交，桥接后台已领取')
            self.log(f'[2fa] 开始执行{label}；数据库锁等待上限 5 秒')
        try:
            result = operation()
            if not quiet:
                status = ('已取消' if result.get('cancelled') else
                          '预览完成，等待确认' if result.get('pending') else
                          '二维码已读取，等待补齐' if result.get('batch', {}).get('missing') else '成功')
                self.log(f'[2fa] {label}{status}，耗时 {time.monotonic()-started:.2f} 秒')
            return result
        except Exception as exc:
            # 不记录异常正文：第三方库可能把原始 URI、密钥或密码包含在消息中。
            frames = ''.join(traceback.format_tb(exc.__traceback__))
            self.log(f'[2fa] {label}失败：{type(exc).__name__}，耗时 {time.monotonic()-started:.2f} 秒\n{frames}')
            message = str(exc) if isinstance(exc, TwoFactorError) else f'{label}失败，请重试或查看运行日志'
            return {'ok': False, 'msg': message}

    def get_otp_accounts(self):
        return self._otp_call('读取账号', self._otp_store.listing, quiet=True)

    def get_otp_code(self, account_id):
        return self._otp_call('获取验证码', lambda: self._otp_store.code(account_id), quiet=True)

    def save_otp_account(self, value):
        return self._otp_call('保存账号', lambda: self._otp_store.save(value))

    def delete_otp_account(self, account_id, revision):
        return self._otp_call('删除账号', lambda: self._otp_store.delete(account_id, revision))

    def reorder_otp_accounts(self, ids, expected_order):
        return self._otp_call('保存账号顺序', lambda: self._otp_store.reorder(ids, expected_order))

    def parse_otp_input(self, value):
        return self._otp_call('解析密钥', lambda: {'ok': True, 'account': normalize(
            {'secret': value}, require_labels=False)})

    def _otp_choose(self, kind, filename='', multiple=False):
        from webview import FileDialog
        window = self.worker.main_window
        if window is None:
            raise TwoFactorError('主窗口尚未就绪')
        self.log('[2fa] 等待系统文件选择；用户可取消')
        selected = window.create_file_dialog(
            FileDialog.SAVE if filename else FileDialog.OPEN, allow_multiple=multiple,
            save_filename=filename, file_types=(kind,))
        return (selected if multiple else selected[0]) if selected else None

    def _otp_decode(self, content):
        if not self._otp_transfer_lock.acquire(blocking=False):
            raise TwoFactorError('已有导入或备份任务正在执行，请稍后重试')
        try:
            self.log('[2fa] 开始独立进程二维码格式识别；上限 6 MB、800 万像素，超时 10 秒')
            decoded = qr.decode_isolated(content, auto=True)
            if decoded['kind'] == 'google':
                self.log('[2fa] 已识别 Google 导出格式，开始创建导入预览')
                candidate = MigrationBatch()
                candidate.add(decoded['uris'])
                return {'kind': 'google', **self._otp_stage_google(candidate)}
            value = normalize({'secret': decoded['uri']}, require_labels=False)
            return {'ok': True, 'kind': 'totp', 'account': value}
        finally:
            self._otp_transfer_lock.release()

    def import_otp_qr(self):
        def operation():
            path = self._otp_choose('二维码图片 (*.png;*.jpg;*.jpeg;*.webp)')
            if not path:
                return {'ok': True, 'cancelled': True}
            if os.path.splitext(path)[1].lower() not in ('.png', '.jpg', '.jpeg', '.webp') or not os.path.isfile(path):
                raise TwoFactorError('请选择 PNG、JPEG 或 WebP 图片')
            with open(path, 'rb') as stream:
                return self._otp_decode(stream.read(qr.MAX_BYTES + 1))
        return self._otp_call('导入二维码', operation)

    @staticmethod
    def _otp_image_content(data_url):
        if not isinstance(data_url, str) or len(data_url) > qr.MAX_BYTES * 4 // 3 + 128:
            raise TwoFactorError('图片超过 6 MB')
        header, _, encoded = data_url.partition(',')
        if header not in ('data:image/png;base64', 'data:image/jpeg;base64', 'data:image/webp;base64'):
            raise TwoFactorError('剪贴板图片格式不支持')
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError:
            raise TwoFactorError('剪贴板图片数据无效') from None

    def decode_otp_image(self, data_url):
        def operation():
            return self._otp_decode(self._otp_image_content(data_url))
        return self._otp_call('识别剪贴板二维码', operation)

    def _otp_preview(self, prepared, kind):
        token = uuid.uuid4().hex
        self._otp_pending = (token, time.monotonic() + 300, prepared, kind)
        return {'ok': True, 'pending': True, 'token': token,
                'summary': {'additions': [{'issuer': r['issuer'], 'account': r['account']}
                                          for r in prepared['additions']],
                            'replacements': [{'issuer': r['issuer'], 'account': r['account']}
                                             for r in prepared['replacements']],
                            'duplicates': prepared['duplicates'], 'conflicts': prepared['conflicts']}}

    def _otp_collect_google(self, contents):
        if self._otp_google and self._otp_google.deadline < time.monotonic():
            self._otp_google = None
            self._otp_pending = None
            raise TwoFactorError('Google 导入预览已过期，请重新选择全部二维码')
        candidate = MigrationBatch(self._otp_google)
        deadline, total_bytes = time.monotonic() + 60, 0
        for index, content in enumerate(contents):
            total_bytes += len(content)
            remaining = deadline - time.monotonic()
            if index >= MAX_PAGES or total_bytes > 64 * 1024 * 1024 or remaining <= 0:
                raise TwoFactorError('本次图片超过数量、大小或 60 秒处理上限，请减少图片后重试')
            self.log(f'[2fa] 开始识别第 {index + 1} 张 Google 导出图片；单张超时 10 秒，总期限 60 秒')
            candidate.add(qr.decode_isolated(content, migration=True, timeout=min(10, remaining)))
        return self._otp_stage_google(candidate)

    def _otp_stage_google(self, candidate):
        status = candidate.status()
        result = {'ok': True, 'pending': False, 'batch': status}
        if not status['missing']:
            prepared = self._otp_store.prepare_import(candidate.accounts(), restore_unreadable=False)
            result.update(self._otp_preview(prepared, 'google'))
        else:
            self._otp_pending = None
        self._otp_google = candidate
        return result

    def preview_otp_google_images(self):
        def operation():
            if not self._otp_transfer_lock.acquire(blocking=False):
                raise TwoFactorError('已有导入或备份任务正在执行，请稍后重试')
            try:
                paths = self._otp_choose('二维码图片 (*.png;*.jpg;*.jpeg;*.webp)', multiple=True)
                if not paths:
                    return {'ok': True, 'cancelled': True}
                if len(paths) > MAX_PAGES:
                    raise TwoFactorError('一次最多选择 100 张二维码图片')

                def contents():
                    for path in paths:
                        if os.path.splitext(path)[1].lower() not in ('.png', '.jpg', '.jpeg', '.webp') or not os.path.isfile(path):
                            raise TwoFactorError('请选择 PNG、JPEG 或 WebP 图片')
                        with open(path, 'rb') as stream:
                            yield stream.read(qr.MAX_BYTES + 1)
                return self._otp_collect_google(contents())
            finally:
                self._otp_transfer_lock.release()
        return self._otp_call('读取 Google 导出图片', operation)

    def preview_otp_google_image(self, data_url):
        def operation():
            if not self._otp_transfer_lock.acquire(blocking=False):
                raise TwoFactorError('已有导入或备份任务正在执行，请稍后重试')
            try:
                return self._otp_collect_google([self._otp_image_content(data_url)])
            finally:
                self._otp_transfer_lock.release()
        return self._otp_call('读取 Google 剪贴板图片', operation)

    def export_otp_backup(self, password):
        def operation():
            if not self._otp_transfer_lock.acquire(blocking=False):
                raise TwoFactorError('已有备份任务正在执行，请稍后重试')
            try:
                path = self._otp_choose('2FA 加密备份 (*.cx2fa)',
                                        time.strftime('CXVPNTools-2FA-%Y%m%d-%H%M%S.cx2fa'))
                if not path:
                    return {'ok': True, 'cancelled': True}
                self.log('[2fa] 开始加密并写入备份；固定 KDF 参数，原子替换')
                content = backup.encrypt(self._otp_store.export_records(), password)
                return {'ok': True, 'path': backup.write_file(path, content), 'msg': '加密备份已保存'}
            finally:
                self._otp_transfer_lock.release()
        return self._otp_call('导出备份', operation)

    def preview_otp_restore(self, password):
        def operation():
            if not self._otp_transfer_lock.acquire(blocking=False):
                raise TwoFactorError('已有备份任务正在执行，请稍后重试')
            try:
                self._otp_pending = None
                self._otp_google = None
                path = self._otp_choose('2FA 加密备份 (*.cx2fa)')
                if not path:
                    return {'ok': True, 'cancelled': True}
                self.log('[2fa] 开始读取并验证加密备份；文件上限 4 MB')
                prepared = self._otp_store.prepare_import(backup.decrypt(backup.read_file(path), password))
                return self._otp_preview(prepared, 'backup')
            finally:
                self._otp_transfer_lock.release()
        return self._otp_call('预览备份', operation)

    def _otp_confirm(self, token, kind):
        def operation():
            if not self._otp_transfer_lock.acquire(blocking=False):
                raise TwoFactorError('已有备份任务正在执行，请稍后重试')
            try:
                pending = self._otp_pending
                self._otp_pending = None
                if not pending or pending[0] != token or pending[1] < time.monotonic() or pending[3] != kind:
                    raise TwoFactorError('导入预览已过期，请重新选择文件并预览')
                result = self._otp_store.apply_import(pending[2])
                self._otp_google = None
                return result
            finally:
                self._otp_transfer_lock.release()
        return self._otp_call('导入 Google 账号' if kind == 'google' else '恢复备份', operation)

    def restore_otp_backup(self, token):
        return self._otp_confirm(token, 'backup')

    def confirm_otp_google_import(self, token):
        return self._otp_confirm(token, 'google')

    def cancel_otp_restore(self):
        if not self._otp_transfer_lock.acquire(timeout=0.2):
            return {'ok': False, 'msg': '导入或备份任务正在执行，请在文件窗口取消或等待完成'}
        try:
            self._otp_pending = None
            self._otp_google = None
        finally:
            self._otp_transfer_lock.release()
        return {'ok': True, 'cancelled': True}

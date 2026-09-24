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


class TwoFactorApi:
    def _init_two_factor(self, root):
        self._otp_store = AccountStore(root)
        self._otp_transfer_lock = threading.Lock()
        self._otp_pending = None

    def _otp_call(self, label, operation, quiet=False):
        started = time.monotonic()
        if not quiet:
            self.log(f'[2fa] {label}请求已提交，桥接后台已领取')
            self.log(f'[2fa] 开始执行{label}；数据库锁等待上限 5 秒')
        try:
            result = operation()
            if not quiet:
                status = ('已取消' if result.get('cancelled') else
                          '预览完成，等待确认' if result.get('pending') else '成功')
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

    def parse_otp_input(self, value):
        return self._otp_call('解析密钥', lambda: {'ok': True, 'account': normalize(
            {'secret': value}, require_labels=False)})

    def _otp_choose(self, kind, filename=''):
        from webview import FileDialog
        window = self.worker.main_window
        if window is None:
            raise TwoFactorError('主窗口尚未就绪')
        self.log('[2fa] 等待系统文件选择；用户可取消')
        selected = window.create_file_dialog(
            FileDialog.SAVE if filename else FileDialog.OPEN, allow_multiple=False,
            save_filename=filename, file_types=(kind,))
        return selected[0] if selected else None

    def _otp_decode(self, content):
        self.log('[2fa] 开始独立进程二维码识别；上限 6 MB、800 万像素，超时 10 秒')
        value = normalize({'secret': qr.decode_isolated(content)}, require_labels=False)
        return {'ok': True, 'account': value}

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

    def decode_otp_image(self, data_url):
        def operation():
            if not isinstance(data_url, str) or len(data_url) > qr.MAX_BYTES * 4 // 3 + 128:
                raise TwoFactorError('图片超过 6 MB')
            header, _, encoded = data_url.partition(',')
            if header not in ('data:image/png;base64', 'data:image/jpeg;base64', 'data:image/webp;base64'):
                raise TwoFactorError('剪贴板图片格式不支持')
            try:
                content = base64.b64decode(encoded, validate=True)
            except ValueError:
                raise TwoFactorError('剪贴板图片数据无效') from None
            return self._otp_decode(content)
        return self._otp_call('识别剪贴板二维码', operation)

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
                path = self._otp_choose('2FA 加密备份 (*.cx2fa)')
                if not path:
                    return {'ok': True, 'cancelled': True}
                self.log('[2fa] 开始读取并验证加密备份；文件上限 4 MB')
                prepared = self._otp_store.prepare_import(backup.decrypt(backup.read_file(path), password))
                token = uuid.uuid4().hex
                self._otp_pending = (token, time.monotonic() + 300, prepared)
                return {'ok': True, 'pending': True, 'token': token,
                        'summary': {'additions': [{'issuer': r['issuer'], 'account': r['account']}
                                                  for r in prepared['additions']],
                                    'replacements': [{'issuer': r['issuer'], 'account': r['account']}
                                                     for r in prepared['replacements']],
                                    'duplicates': prepared['duplicates'], 'conflicts': prepared['conflicts']}}
            finally:
                self._otp_transfer_lock.release()
        return self._otp_call('预览备份', operation)

    def restore_otp_backup(self, token):
        def operation():
            if not self._otp_transfer_lock.acquire(blocking=False):
                raise TwoFactorError('已有备份任务正在执行，请稍后重试')
            try:
                pending = self._otp_pending
                self._otp_pending = None
                if not pending or pending[0] != token or pending[1] < time.monotonic():
                    raise TwoFactorError('导入预览已过期，请重新选择备份')
                return self._otp_store.apply_import(pending[2])
            finally:
                self._otp_transfer_lock.release()
        return self._otp_call('恢复备份', operation)

    def cancel_otp_restore(self):
        if not self._otp_transfer_lock.acquire(timeout=0.2):
            return {'ok': False, 'msg': '备份任务正在执行，请在文件窗口取消'}
        try:
            self._otp_pending = None
        finally:
            self._otp_transfer_lock.release()
        return {'ok': True, 'cancelled': True}

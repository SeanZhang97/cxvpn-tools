# -*- coding: utf-8 -*-
"""短信验证码接收器：Windows 手机连接通知或快捷指令转发邮箱。"""
import datetime
import email
import imaplib
import os
import queue
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from email import policy
from email.header import decode_header
from email.utils import parsedate_to_datetime

DB_PATH = os.path.join(os.environ['LOCALAPPDATA'], 'Microsoft', 'Windows',
                       'Notifications', 'wpndatabase.db')

CODE_PATTERNS = [
    re.compile(r'(?:验证码|校验码|动态码|确认码|短信码|一次性代码|verification\s*code|'
               r'security\s*code|one-time\s+code|code)[^0-9]{0,6}(\d{4,8})', re.I),
    re.compile(r'(\d{4,8})\s*(?:为您的验证码|是你?的验证码|是您?的一次性代码)'),
]


def now_filetime():
    """当前时间的 FILETIME (100ns, 1601-01-01 UTC)"""
    delta = datetime.datetime.now(datetime.timezone.utc) - \
        datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)
    return int(delta.total_seconds() * 10_000_000)


def toast_texts(payload):
    if not isinstance(payload, (bytes, bytearray)):
        return []
    try:
        root = ET.fromstring(payload.decode('utf-8', errors='replace'))
    except ET.ParseError:
        return []
    return [el.text.strip() for el in root.iter()
            if el.text and el.text.strip()
            and el.tag.rsplit('}', 1)[-1] == 'text']


def find_code(text):
    for pat in CODE_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


def _decode_header(value):
    """解码 RFC 2047 邮件头。"""
    parts = []
    for fragment, charset in decode_header(value or ''):
        if isinstance(fragment, bytes):
            try:
                fragment = fragment.decode(charset or 'utf-8', errors='replace')
            except LookupError:
                fragment = fragment.decode('utf-8', errors='replace')
        parts.append(fragment)
    return ''.join(parts)


def email_text(message):
    """优先提取 text/plain，兼容快捷指令生成的 multipart 邮件。"""
    plain = []
    html = []
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.get_content_maintype() == 'multipart':
            continue
        disposition = (part.get_content_disposition() or '').lower()
        if disposition == 'attachment':
            continue
        kind = part.get_content_type()
        if kind not in ('text/plain', 'text/html'):
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            raw = part.get_payload(decode=True) or b''
            content = raw.decode(part.get_content_charset() or 'utf-8',
                                 errors='replace')
        (plain if kind == 'text/plain' else html).append(str(content))
    text = '\n'.join(plain or html)
    if not plain and html:
        text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def normalize_email_settings(settings):
    email_cfg = (settings or {}).get('email') or {}
    try:
        port = int(email_cfg.get('port') or 993)
    except (TypeError, ValueError):
        port = 993
    try:
        poll = float(email_cfg.get('poll_interval') or 3)
    except (TypeError, ValueError):
        poll = 3
    return {
        'provider': str(email_cfg.get('provider') or 'custom').strip(),
        'host': str(email_cfg.get('host') or '').strip(),
        'port': port,
        'username': str(email_cfg.get('username') or '').strip(),
        'password': str(email_cfg.get('password') or ''),
        'mailbox': str(email_cfg.get('mailbox') or 'INBOX').strip() or 'INBOX',
        'subject': str(email_cfg.get('subject') or '').strip(),
        'sender': str(email_cfg.get('sender') or '').strip(),
        'poll_interval': min(30, max(1, poll)),
    }


def _identify_imap_client(client, cfg):
    """163/126 邮箱要求客户端在认证阶段声明 IMAP ID。"""
    if cfg.get('provider') != '163':
        return
    imaplib.Commands['ID'] = ('NONAUTH', 'AUTH', 'SELECTED')
    try:
        client.xatom('ID', '("name" "CXVPN Manager" "version" "1.0" '
                     '"vendor" "CXVPN")')
    except imaplib.IMAP4.error:
        # 部分服务器仅在认证后接受 ID；登录仍可能正常，不能在此提前失败。
        pass


def test_imap(settings, timeout=12):
    """登录并只读打开邮箱；不读取邮件正文。"""
    cfg = normalize_email_settings(settings)
    missing = [name for name in ('host', 'username', 'password')
               if not cfg[name]]
    if missing:
        return False, '请完整填写 IMAP 服务器、邮箱账号和授权码'
    client = None
    try:
        client = imaplib.IMAP4_SSL(cfg['host'], cfg['port'], timeout=timeout)
        _identify_imap_client(client, cfg)
        client.login(cfg['username'], cfg['password'])
        status, _ = client.select(cfg['mailbox'], readonly=True)
        if status != 'OK':
            return False, f'无法只读打开邮箱目录 {cfg["mailbox"]}'
        return True, f'邮箱连接正常，已只读打开 {cfg["mailbox"]}'
    except (imaplib.IMAP4.error, OSError) as exc:
        return False, f'邮箱连接失败：{exc}'
    finally:
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass


def fetch_sms_since(since_ft):
    """返回 ArrivalTime > since_ft 的短信通知 [(id, handler, body, code)]"""
    con = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True, timeout=2)
    try:
        rows = con.execute(
            "select n.Id, h.PrimaryId, n.Payload from Notification n "
            "join NotificationHandler h on h.RecordId=n.HandlerId "
            "where n.ArrivalTime > ? and h.PrimaryId like '%YourPhoneMessages%'",
            (since_ft,)).fetchall()
    finally:
        con.close()
    out = []
    for nid, handler, payload in rows:
        body = ' | '.join(toast_texts(payload))
        if body:
            out.append((nid, handler, body, find_code(body)))
    return out


def wait_new_code(timeout=120, poll=1.5, since_ft=None):
    """阻塞等待新短信验证码, 返回 (code, body) 或 None"""
    if since_ft is None:
        since_ft = now_filetime()
    deadline = time.time() + timeout
    seen = set()
    while time.time() < deadline:
        try:
            for nid, handler, body, code in fetch_sms_since(since_ft):
                if nid in seen:
                    continue
                seen.add(nid)
                if code:
                    return code, body
        except sqlite3.Error:
            pass
        time.sleep(poll)
    return None


class Catcher:
    """按配置从 Phone Link 或 IMAP 邮箱持续捕获新验证码。"""

    def __init__(self, settings=None, since_ft=None, poll=1.0, log=None):
        self._q = queue.Queue()
        self._seen = set()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._initial_timed_out = False
        self._log = log
        self._since = since_ft if since_ft is not None else now_filetime()
        self._started_at = time.time()
        self._settings = settings or {}
        self._last_email_uid = 0
        self._method = self._settings.get('method', 'phone_link')
        self.source_label = ('邮箱 IMAP' if self._method == 'email'
                             else 'Windows 手机连接')
        target = self._run_email if self._method == 'email' else self._run_phone_link
        threading.Thread(target=target, args=(poll,), daemon=True).start()

    def wait_ready(self, timeout=12):
        """等待接收源完成初始连接，避免在邮箱基线建立前触发短信。"""
        ready = self._ready.wait(timeout)
        if not ready:
            self._initial_timed_out = True
        return ready

    def _put(self, identity, body, code):
        if identity in self._seen:
            return
        self._seen.add(identity)
        if code:
            if self._log:
                self._log(f'[sms] 已通过{self.source_label}捕获验证码 {code}，'
                          '待流程取用')
            self._q.put((code, body))

    def _run_phone_link(self, poll):
        self._ready.set()
        while not self._stop.is_set():
            try:
                for nid, _h, body, code in fetch_sms_since(self._since):
                    self._put(('phone_link', nid), body, code)
            except sqlite3.Error:
                pass
            self._stop.wait(poll)

    def _mail_matches(self, message):
        cfg = normalize_email_settings(self._settings)
        subject = _decode_header(message.get('Subject', ''))
        sender = _decode_header(message.get('From', ''))
        if cfg['subject'] and cfg['subject'].casefold() not in subject.casefold():
            return False
        if cfg['sender'] and cfg['sender'].casefold() not in sender.casefold():
            return False
        return True

    def _scan_email(self, client, initial=False):
        status, data = client.uid('search', None, 'ALL')
        if status != 'OK' or not data:
            return
        uids = [int(item) for item in data[0].split() if item.isdigit()]
        if initial and not self._initial_timed_out:
            # 已在触发短信前完成连接：现有邮件全部作为基线，避免误取旧验证码。
            self._last_email_uid = max(uids, default=0)
            return
        # 初次连接若曾超时，检查最近 50 封并依 Date 排除流程启动前的旧邮件。
        candidates = uids[-50:] if initial else [uid for uid in uids
                                                  if uid > self._last_email_uid]
        for uid in candidates:
            identity = ('email', uid)
            if identity in self._seen:
                continue
            status, rows = client.uid('fetch', str(uid), '(BODY.PEEK[])')
            if status != 'OK' or not rows:
                continue
            raw = next((row[1] for row in rows
                        if isinstance(row, tuple) and isinstance(row[1], bytes)), None)
            if raw is None:
                continue
            message = email.message_from_bytes(raw, policy=policy.default)
            try:
                sent = parsedate_to_datetime(message.get('Date', '')).timestamp()
            except (TypeError, ValueError, OverflowError):
                sent = 0
            if sent and sent < self._started_at - 30:
                self._seen.add(identity)
                self._last_email_uid = max(self._last_email_uid, uid)
                continue
            body = email_text(message)
            code = find_code(body) if self._mail_matches(message) else None
            self._put(identity, body, code)
            self._last_email_uid = max(self._last_email_uid, uid)

    def _run_email(self, _poll):
        cfg = normalize_email_settings(self._settings)
        if not (cfg['host'] and cfg['username'] and cfg['password']):
            if self._log:
                self._log('[sms] 邮箱接收配置不完整，将保留手动输入方式')
            self._ready.set()
            return
        first_scan = True
        while not self._stop.is_set():
            client = None
            try:
                client = imaplib.IMAP4_SSL(cfg['host'], cfg['port'], timeout=12)
                _identify_imap_client(client, cfg)
                client.login(cfg['username'], cfg['password'])
                status, _ = client.select(cfg['mailbox'], readonly=True)
                if status != 'OK':
                    raise imaplib.IMAP4.error(
                        f'无法打开邮箱目录 {cfg["mailbox"]}')
                self._scan_email(client, initial=first_scan)
                first_scan = False
                self._ready.set()
                if self._log:
                    self._log(f'[sms] 邮箱验证码监听已就绪：{cfg["host"]}')
                while not self._stop.wait(cfg['poll_interval']):
                    self._scan_email(client)
            except (imaplib.IMAP4.error, OSError) as exc:
                if self._log:
                    self._log(f'[sms] 邮箱监听异常，稍后重连：{exc}')
                self._stop.wait(3)
            finally:
                if client is not None:
                    try:
                        client.logout()
                    except (imaplib.IMAP4.error, OSError):
                        pass

    def wait(self, timeout):
        """阻塞 timeout 秒取 (code, body), 超时返回 None; 返回后停止后台轮询"""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            self._stop.set()

    def poll(self):
        """非阻塞取一个已捕获的 (code, body), 无则 None"""
        try:
            return self._q.get_nowait()
        except queue.Empty:
            return None

    def stop(self):
        self._stop.set()


if __name__ == '__main__':
    print('等待新验证码 120 秒...')
    res = wait_new_code(120)
    print('结果:', res)

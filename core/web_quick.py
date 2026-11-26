# -*- coding: utf-8 -*-
"""网页快开的文件夹与条目：同上级内共用一套 position，密码按条目 ID 加密，不参与配置 revision。"""
from __future__ import annotations

import time
from urllib.parse import urlsplit
import uuid

from core import dpapi, state_store

MAX_ENTRIES = 2000
MAX_DEPTH = 16
PASSWORD_ENTROPY = 'CXVPNTools.WebQuick.v1:'
# 平台标识 -> (显示名, 站点 origin)；origin 完全一致才允许自动登录。
AUTO_LOGIN_SITES = {
    'ifort': ('iFort', 'https://10.0.34.53'),
    'jumpserver': ('JumpServer', 'http://192.168.1.51'),
    'gitlab': ('GitLab', 'https://10.0.26.27'),
}
ITEM_COLUMNS = ('SELECT id,name,url,folder_id,position,platform,username,'
                'password IS NOT NULL AS has_password,otp_account_id,has_favicon,created_at,updated_at '
                'FROM web_quick_items ')
FOLDER_COLUMNS = 'SELECT id,name,parent_id,position,created_at,updated_at FROM web_quick_folders '
# UNION 去重：即使数据中意外出现环，递归也会终止。
_SUBTREE = ('WITH RECURSIVE sub(id, depth) AS ('
            'SELECT id, 1 FROM web_quick_folders WHERE id=? '
            'UNION SELECT f.id, s.depth+1 FROM web_quick_folders f JOIN sub s ON f.parent_id=s.id) '
            'SELECT id, depth FROM sub')


class WebQuickError(ValueError):
    """可向用户展示的固定错误，不包含密码。"""


def _text(value, name, limit):
    if not isinstance(value, str):
        raise WebQuickError(f'{name}必须是文本')
    value = value.strip()
    if not value:
        raise WebQuickError(f'请填写{name}')
    if len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise WebQuickError(f'{name}过长或包含控制字符')
    return value


def normalize_url(value):
    if not isinstance(value, str):
        raise WebQuickError('网址必须是文本')
    value = value.strip()
    if not value:
        raise WebQuickError('请填写网址')
    if len(value) > 2048 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise WebQuickError('网址过长或包含空格、控制字符')
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise WebQuickError('网址格式无效') from None
    if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
        raise WebQuickError('网址需以 http:// 或 https:// 开头，并包含主机名')
    if parts.username is not None or parts.password is not None:
        raise WebQuickError('网址中不能包含账号或密码')
    return value


def platform_for_url(url):
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.hostname
    if not host:
        return None
    if ':' in host:
        host = f'[{host}]'
    port = parts.port
    origin = f'{scheme}://{host}' + ('' if port in (None, 443 if scheme == 'https' else 80)
                                     else f':{port}')
    return next((key for key, (_, site) in AUTO_LOGIN_SITES.items() if site == origin), None)


def _rows(conn, sql, args=()):
    cursor = conn.execute(sql, args)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _one(conn, sql, args=()):
    rows = _rows(conn, sql, args)
    return rows[0] if rows else None


def _ancestors(conn, folder_id):
    result = []
    current = folder_id
    while current is not None and len(result) <= MAX_DEPTH:
        result.append(current)
        row = _one(conn, 'SELECT parent_id FROM web_quick_folders WHERE id=?', (current,))
        if row is None:
            break
        current = row['parent_id']
    return result


def _subtree(conn, folder_id):
    return _rows(conn, _SUBTREE, (folder_id,))


def _children(conn, parent_id):
    rows = [{'key': f"folder:{row['id']}", 'position': row['position'],
             'created_at': row['created_at'], 'kind': 'folder', 'id': row['id']}
            for row in _rows(conn, 'SELECT id,position,created_at FROM web_quick_folders '
                                      'WHERE parent_id IS ?', (parent_id,))]
    rows += [{'key': f"item:{row['id']}", 'position': row['position'],
              'created_at': row['created_at'], 'kind': 'item', 'id': row['id']}
             for row in _rows(conn, 'SELECT id,position,created_at FROM web_quick_items '
                                       'WHERE folder_id IS ?', (parent_id,))]
    rows.sort(key=lambda row: (row['position'], row['created_at'], row['kind'], row['id']))
    return [row['key'] for row in rows]


def _next_position(conn, parent_id):
    highest = -1
    for table, column in (('web_quick_folders', 'parent_id'), ('web_quick_items', 'folder_id')):
        row = conn.execute(f'SELECT MAX(position) FROM {table} WHERE {column} IS ?',
                           (parent_id,)).fetchone()
        highest = max(highest, row[0] if row[0] is not None else -1)
    return highest + 1


def _entry_count(conn):
    return sum(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
               for table in ('web_quick_folders', 'web_quick_items'))


def _validate_folder_parent(conn, folder_id, parent_id):
    if parent_id is not None and not _one(
            conn, 'SELECT id FROM web_quick_folders WHERE id=?', (parent_id,)):
        raise WebQuickError('目标文件夹已不存在，请刷新后重试')
    if folder_id is None:
        return
    ancestors = _ancestors(conn, parent_id)
    if folder_id in ancestors:
        raise WebQuickError('不能把文件夹放到它自己或它的子文件夹内')
    depth = max((row['depth'] for row in _subtree(conn, folder_id)), default=1)
    if len(ancestors) + depth > MAX_DEPTH:
        raise WebQuickError(f'文件夹层级不能超过 {MAX_DEPTH} 级')


class Store:
    def __init__(self, root=None):
        self.root = root

    def listing(self):
        with state_store._reader(self.root) as conn:
            items = _rows(conn, ITEM_COLUMNS + 'ORDER BY position, created_at, id')
            for row in items:
                row['has_password'] = bool(row['has_password'])
                row['has_favicon'] = bool(row['has_favicon'])
                row['username'] = row['username'] or ''
                row['folder_id'] = row['folder_id'] or None
            result = {
                'ok': True,
                'folders': _rows(conn, FOLDER_COLUMNS + 'ORDER BY position, created_at, id'),
                'items': items,
                'auto_login_count': sum(1 for row in items if row['platform']),
            }
        return result

    def credentials(self, item_id):
        """仅供自动登录领取和专用编辑密码接口调用，返回值不得进入列表或日志。"""
        with state_store._reader(self.root) as conn:
            row = _one(conn, 'SELECT id,platform,username,password,otp_account_id '
                             'FROM web_quick_items WHERE id=?', (item_id,))
        if row is None or not row['platform']:
            return None
        password = ''
        if row['password']:
            try:
                password = dpapi.unprotect(bytes(row['password']),
                                           PASSWORD_ENTROPY + row['id']).decode('utf-8')
            except (dpapi.DpapiError, UnicodeError) as exc:
                raise WebQuickError('密码无法解密，请重新保存条目') from exc
        return {'platform': row['platform'], 'username': row['username'],
                'password': password, 'otp_account_id': row['otp_account_id']}

    def otp_account(self, item_id):
        """整页续接只读取平台与 2FA 关联，不解密或返回登录密码。"""
        with state_store._reader(self.root) as conn:
            return _one(conn, 'SELECT platform,otp_account_id FROM web_quick_items WHERE id=?',
                        (item_id,))

    def password_for_edit(self, item_id):
        credentials = self.credentials(item_id)
        if credentials is None:
            raise WebQuickError('自动登录条目已删除或已改为普通网址，请刷新列表')
        return {'ok': True, 'password': credentials['password']}

    def save_item(self, payload):
        if not isinstance(payload, dict):
            raise WebQuickError('条目数据格式无效')
        item_id = payload.get('id') or uuid.uuid4().hex
        if not isinstance(item_id, str) or len(item_id) > 128:
            raise WebQuickError('条目数据格式无效')
        name = _text(payload.get('name'), '名称', 128)
        url = normalize_url(payload.get('url'))
        auto_login = bool(payload.get('auto_login'))
        platform = platform_for_url(url) if auto_login else None
        if auto_login and not platform:
            supported = '、'.join(f'{label}（{site}）' for label, site in AUTO_LOGIN_SITES.values())
            raise WebQuickError(f'这个网址暂不支持自动登录，目前仅支持 {supported}')
        fields = {'platform': None, 'username': None, 'password': None, 'otp_account_id': None}
        if platform:
            password = payload.get('password')
            if not isinstance(password, str) or len(password) > 1024:
                raise WebQuickError('密码格式无效或过长')
            if any(ord(c) < 32 for c in password):
                raise WebQuickError('密码不能包含控制字符')
            fields = {'platform': platform, 'password': password,
                      'username': _text(payload.get('username'), '账号', 256),
                      'otp_account_id': payload.get('otp_account_id') or None}
        folder_id = payload.get('folder_id') or None
        if (folder_id is not None and not isinstance(folder_id, str)) or not isinstance(
                fields['otp_account_id'], (str, type(None))):
            raise WebQuickError('条目数据格式无效')
        with state_store.transaction(self.root) as conn:
            previous = _one(conn, 'SELECT * FROM web_quick_items WHERE id=?', (item_id,)) \
                if payload.get('id') else None
            if payload.get('id') and previous is None:
                raise WebQuickError('条目已删除，请刷新列表')
            if folder_id is not None and not _one(
                    conn, 'SELECT id FROM web_quick_folders WHERE id=?', (folder_id,)):
                raise WebQuickError('文件夹已不存在，请重新选择')
            if fields['otp_account_id'] is not None and not _one(
                    conn, 'SELECT id FROM otp_accounts WHERE id=?', (fields['otp_account_id'],)):
                raise WebQuickError('二次验证账号已不存在，请重新选择')
            sealed = previous['password'] if previous and platform else None
            if platform and fields['password']:
                try:
                    sealed = dpapi.protect(fields['password'].encode('utf-8'),
                                           PASSWORD_ENTROPY + item_id)
                except dpapi.DpapiError as exc:
                    raise WebQuickError('Windows 密钥加密失败，条目未保存') from exc
            if platform and not sealed:
                raise WebQuickError('请填写密码')
            if not previous and _entry_count(conn) >= MAX_ENTRIES:
                raise WebQuickError(f'条目数量已达上限（{MAX_ENTRIES}）')
            same_folder = bool(previous) and previous['folder_id'] == folder_id
            has_favicon = bool(previous and previous['url'] == url and previous['has_favicon'])
            conn.execute(
                'INSERT INTO web_quick_items(id,name,url,folder_id,position,platform,username,'
                'password,otp_account_id,has_favicon,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=excluded.url,'
                'folder_id=excluded.folder_id,position=excluded.position,platform=excluded.platform,'
                'username=excluded.username,password=excluded.password,'
                'otp_account_id=excluded.otp_account_id,has_favicon=excluded.has_favicon,'
                'updated_at=excluded.updated_at',
                (item_id, name, url, folder_id,
                 previous['position'] if same_folder else _next_position(conn, folder_id),
                 platform, fields['username'], sealed, fields['otp_account_id'], int(has_favicon),
                 previous['created_at'] if previous else int(time.time()), int(time.time())))
        return {'ok': True, 'id': item_id, 'msg': '已保存' if payload.get('id') else '已添加'}

    def delete_item(self, item_id):
        with state_store.transaction(self.root) as conn:
            if not conn.execute('DELETE FROM web_quick_items WHERE id=?', (item_id,)).rowcount:
                raise WebQuickError('条目已删除，请刷新列表')
        return {'ok': True, 'id': item_id}

    def set_favicon(self, item_id, key, acquired):
        """只接收与条目当前 origin 匹配的采集结果，忽略旧网址的迟到响应。"""
        if not isinstance(acquired, bool):
            raise WebQuickError('图标状态格式无效')
        from core.web_quick_favicon import favicon_key
        with state_store.transaction(self.root) as conn:
            row = _one(conn, 'SELECT url,has_favicon FROM web_quick_items WHERE id=?', (item_id,))
            if row is None:
                raise WebQuickError('条目已删除，请刷新列表')
            current = bool(row['has_favicon'])
            if favicon_key(row['url']) != key or current == acquired:
                return {'ok': True, 'id': item_id, 'has_favicon': current, 'changed': False}
            conn.execute('UPDATE web_quick_items SET has_favicon=?,updated_at=? WHERE id=?',
                         (int(acquired), int(time.time()), item_id))
        return {'ok': True, 'id': item_id, 'has_favicon': acquired, 'changed': True}

    def subtree(self, folder_id):
        with state_store._reader(self.root) as conn:
            rows = _subtree(conn, folder_id)
            if not rows:
                raise WebQuickError('文件夹已删除，请刷新列表')
            ids = [row['id'] for row in rows]
            marks = ','.join('?' * len(ids))
            items = _rows(conn, 'SELECT platform FROM web_quick_items '
                                  f'WHERE folder_id IN ({marks})', ids)
        return {'ok': True, 'folders': len(rows), 'items': len(items),
                'logins': sum(1 for row in items if row['platform'])}

    def delete_folder(self, folder_id):
        with state_store.transaction(self.root) as conn:
            ids = [row['id'] for row in _subtree(conn, folder_id)]
            if not ids:
                raise WebQuickError('文件夹已删除，请刷新列表')
            marks = ','.join('?' * len(ids))
            items = conn.execute(f'DELETE FROM web_quick_items WHERE folder_id IN ({marks})',
                                 ids).rowcount
            # 子文件夹由外键级联删除，rowcount 只含语句直接命中的行，数量以子树为准。
            conn.execute(f'DELETE FROM web_quick_folders WHERE id IN ({marks})', ids)
        return {'ok': True, 'id': folder_id, 'folders': len(ids), 'items': items}

    def save_folder(self, payload):
        if not isinstance(payload, dict):
            raise WebQuickError('文件夹数据格式无效')
        folder_id = payload.get('id') or uuid.uuid4().hex
        parent_id = payload.get('parent_id') or None
        if any(value is not None and (not isinstance(value, str) or len(value) > 128)
               for value in (folder_id, parent_id)):
            raise WebQuickError('文件夹数据格式无效')
        name = _text(payload.get('name'), '文件夹名称', 64)
        with state_store.transaction(self.root) as conn:
            previous = _one(conn, 'SELECT * FROM web_quick_folders WHERE id=?', (folder_id,)) \
                if payload.get('id') else None
            if payload.get('id') and previous is None:
                raise WebQuickError('文件夹已删除，请刷新列表')
            _validate_folder_parent(conn, folder_id, parent_id)
            if not previous and _entry_count(conn) >= MAX_ENTRIES:
                raise WebQuickError(f'条目数量已达上限（{MAX_ENTRIES}）')
            same_parent = bool(previous) and previous['parent_id'] == parent_id
            now = int(time.time())
            conn.execute(
                'INSERT INTO web_quick_folders(id,name,parent_id,position,created_at,updated_at) '
                'VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,'
                'parent_id=excluded.parent_id,position=excluded.position,updated_at=excluded.updated_at',
                (folder_id, name, parent_id,
                 previous['position'] if same_parent else _next_position(conn, parent_id),
                 previous['created_at'] if previous else now, now))
        return {'ok': True, 'id': folder_id, 'msg': '已保存' if payload.get('id') else '已新建'}

    def move_entry(self, key, target_folder_id):
        if target_folder_id in (None, ''):
            target_folder_id = None
        kind, separator, row_id = key.partition(':') if isinstance(key, str) else ('', '', '')
        if (not separator or kind not in ('folder', 'item') or not row_id or len(row_id) > 128 or
                (target_folder_id is not None and
                 (not isinstance(target_folder_id, str) or len(target_folder_id) > 128))):
            raise WebQuickError('移动数据格式无效')
        # 表名与上级列名只由固定映射决定，不接收外部输入。
        table, column = (('web_quick_folders', 'parent_id') if kind == 'folder'
                         else ('web_quick_items', 'folder_id'))
        with state_store.transaction(self.root) as conn:
            previous = _one(conn, f'SELECT id,{column} FROM {table} WHERE id=?', (row_id,))
            if previous is None:
                raise WebQuickError('移动的内容已删除，请刷新列表')
            _validate_folder_parent(conn, row_id if kind == 'folder' else None, target_folder_id)
            if previous[column] == target_folder_id:
                return {'ok': True, 'moved': False}
            position = _next_position(conn, target_folder_id)
            conn.execute(f'UPDATE {table} SET {column}=?,position=?,updated_at=? WHERE id=?',
                         (target_folder_id, position, int(time.time()), row_id))
        return {'ok': True, 'moved': True, 'position': position}

    def reorder(self, parent_id, keys):
        if (not isinstance(keys, list) or len(keys) > MAX_ENTRIES or
                any(not isinstance(key, str) or len(key) > 140 for key in keys) or
                len(set(keys)) != len(keys)):
            raise WebQuickError('顺序数据格式无效')
        with state_store.transaction(self.root) as conn:
            current = _children(conn, parent_id)
            if sorted(current) != sorted(keys):
                raise WebQuickError('内容已变化，请刷新后重新调整')
            for index, key in enumerate(keys):
                kind, _, row_id = key.partition(':')
                # 表名与上级列名只由固定映射决定，不接收外部输入。
                table, column = (('web_quick_folders', 'parent_id') if kind == 'folder'
                                 else ('web_quick_items', 'folder_id'))
                conn.execute(f'UPDATE {table} SET position=? WHERE id=? AND {column} IS ?',
                             (index, row_id, parent_id))
        return {'ok': True}

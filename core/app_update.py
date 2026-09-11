# -*- coding: utf-8 -*-
"""GitHub Releases 更新检查与安装包下载。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request
import webbrowser

from core import app_paths
from core.version import APP_NAME, APP_VERSION, GITHUB_REPOSITORY, is_newer


API_URL = f'https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest'
USER_AGENT = f'{APP_NAME}/{APP_VERSION}'


def _request(url, timeout=12):
    request = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': USER_AGENT,
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def check_latest():
    """读取最新稳定 Release；失败时返回可展示的错误而不抛到 UI。"""
    try:
        payload = json.loads(_request(API_URL).decode('utf-8'))
        version = str(payload.get('tag_name') or '').lstrip('vV')
        assets = payload.get('assets') or []
        installer = next((item for item in assets
                          if str(item.get('name', '')).lower().endswith('.exe')), None)
        result = {
            'ok': True,
            'current_version': APP_VERSION,
            'latest_version': version,
            'newer': is_newer(version),
            'release_url': payload.get('html_url') or '',
            'installer_url': (installer or {}).get('browser_download_url') or '',
            'installer_name': (installer or {}).get('name') or '',
            'notes': str(payload.get('body') or ''),
            'published_at': payload.get('published_at') or '',
        }
        return result
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {
                'ok': True,
                'current_version': APP_VERSION,
                'latest_version': '',
                'newer': False,
                'release_url': f'https://github.com/{GITHUB_REPOSITORY}/releases',
                'msg': 'GitHub 仓库尚无正式 Release，暂时没有可用的软件更新',
            }
        return {'ok': False, 'current_version': APP_VERSION,
                'msg': f'GitHub 更新接口返回 HTTP {exc.code}'}
    except (OSError, ValueError, UnicodeError, urllib.error.URLError) as exc:
        return {'ok': False, 'current_version': APP_VERSION,
                'msg': f'检查更新失败：{type(exc).__name__}'}


def open_release(url):
    url = str(url or '').strip()
    if not url.startswith(('https://github.com/', 'https://api.github.com/')):
        return {'ok': False, 'msg': '更新地址不受信任'}
    try:
        webbrowser.open(url)
        return {'ok': True, 'msg': '已打开 GitHub Release 页面'}
    except OSError as exc:
        return {'ok': False, 'msg': f'无法打开更新页面：{exc}'}


def download_installer(url, expected_sha256=''):
    """下载到用户数据目录并校验；不执行安装器。"""
    if not url.startswith('https://github.com/'):
        return {'ok': False, 'msg': '安装包地址不受信任'}
    target_root = os.path.join(app_paths.user_data_root(), 'updates')
    os.makedirs(target_root, exist_ok=True)
    target = os.path.join(target_root, os.path.basename(url.split('?', 1)[0]) or 'update.exe')
    temporary = tempfile.mktemp(prefix='update.', suffix='.tmp', dir=target_root)
    digest = hashlib.sha256()
    try:
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as source, open(temporary, 'wb') as out:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
        if expected_sha256 and digest.hexdigest().lower() != expected_sha256.lower():
            raise ValueError('安装包 SHA-256 校验失败')
        os.replace(temporary, target)
        return {'ok': True, 'path': target, 'sha256': digest.hexdigest()}
    except Exception as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return {'ok': False, 'msg': f'下载更新失败：{type(exc).__name__}'}

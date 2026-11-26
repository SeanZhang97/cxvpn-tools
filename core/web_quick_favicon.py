# -*- coding: utf-8 -*-
"""网页快开站点图标：有界抓取、图片转码和可重建的 origin 级 PNG 缓存。"""
from __future__ import annotations

import base64
import collections
import hashlib
import http.client
import io
import ipaddress
import json
import os
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
import uuid
from html.parser import HTMLParser

from core import app_paths
from core.web_quick import WebQuickError, normalize_url

TOTAL_SECONDS = 8.0
MAX_HTML_BYTES = 512 * 1024
MAX_IMAGE_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_PIXELS = 4_000_000
MAX_REDIRECTS = 3
FAIL_SECONDS = 10 * 60
CACHE_METADATA_VERSION = 1
MAX_CACHE_FILES = 256
_KEY = re.compile(r'[0-9a-f]{64}\Z')
_IMAGE_TYPES = {'PNG', 'JPEG', 'GIF', 'ICO', 'WEBP'}
_FALLBACK_PATHS = ('/favicon.ico', '/favicon.png',
                   '/static/img/favicon.ico', '/static/img/favicon.png')


def canonical_origin(url):
    parts = urllib.parse.urlsplit(normalize_url(url))
    host = parts.hostname
    try:
        host = host.encode('idna').decode('ascii').lower()
    except UnicodeError:
        raise WebQuickError('网址主机名无效') from None
    if ':' in host:
        host = f'[{host}]'
    scheme = parts.scheme.lower()
    port = parts.port
    suffix = '' if port in (None, 443 if scheme == 'https' else 80) else f':{port}'
    return f'{scheme}://{host}{suffix}'


def favicon_key(url):
    origin = canonical_origin(url).encode('ascii')
    return hashlib.sha256(b'web-quick-favicon-v4\0' + origin).hexdigest()


def _data_url(content):
    return 'data:image/png;base64,' + base64.b64encode(content).decode('ascii')


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        if self.count > MAX_REDIRECTS:
            raise WebQuickError('图标地址重定向过多')
        target = normalize_url(urllib.parse.urljoin(req.full_url, newurl))
        if urllib.parse.urlsplit(req.full_url).scheme.lower() == 'https' and \
                urllib.parse.urlsplit(target).scheme.lower() != 'https':
            raise WebQuickError('图标地址不允许 HTTPS 降级')
        return super().redirect_request(req, fp, code, msg, headers, target)


class _IconLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.base = ''

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        values = {str(key).lower(): value or '' for key, value in attrs}
        if tag == 'base' and not self.base:
            self.base = values.get('href', '').strip()
            return
        if len(self.links) >= 12:
            return
        if tag == 'meta' and values.get('name', '').lower() == 'msapplication-tileimage':
            href = values.get('content', '').strip()
            if href:
                self.links.append((2, href))
            return
        if tag != 'link':
            return
        rel = values.get('rel', '').lower().split()
        href = values.get('href', '').strip()
        if not href:
            return
        if 'icon' in rel:
            self.links.append((0, href))
        elif any(value in rel for value in
                 ('apple-touch-icon', 'apple-touch-icon-precomposed', 'fluid-icon')):
            self.links.append((1, href))


class FaviconService:
    def __init__(self, root=None, log=None, opener=None, context_opener=None,
                 clock=time.time, monotonic=time.monotonic):
        self.root = os.path.join(root or app_paths.user_data_root(), 'web-quick-icons')
        self._log = log or (lambda message: None)
        self._opener = opener or self._open
        self._context_opener = context_opener or self._open_with_context
        self._clock = clock
        self._monotonic = monotonic
        self._guard = threading.Lock()
        self._locks = {}

    @staticmethod
    def _open(request, timeout):
        # 每次重建 opener，避免系统代理变化后沿用旧 opener。
        return urllib.request.build_opener(urllib.request.ProxyHandler(), _Redirects()).open(
            request, timeout=timeout)

    @staticmethod
    def _open_with_context(request, timeout, context):
        return urllib.request.build_opener(
            urllib.request.ProxyHandler(), urllib.request.HTTPSHandler(context=context),
            _Redirects()).open(request, timeout=timeout)

    def _lock_for(self, key):
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def cached(self, key):
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise WebQuickError('图标缓存键无效')
        content = self._read_png(key)
        return {'ok': True, 'found': bool(content), 'key': key,
                'data_url': _data_url(content) if content else ''}

    def ensure(self, url, force=False):
        origin = canonical_origin(url)
        key = favicon_key(url)
        with self._lock_for(key):
            cached = self._read_png(key)
            metadata = self._read_metadata(key)
            timestamp = metadata.get('timestamp', 0)
            age = max(0.0, self._clock() - timestamp)
            if cached and metadata.get('found') is True:
                return self._result(key, True, _data_url(cached), False)
            if (not force and metadata.get('found') is False and
                    metadata.get('version') == CACHE_METADATA_VERSION and age < FAIL_SECONDS):
                return self._result(key, bool(cached), _data_url(cached) if cached else '', bool(cached))
            started = self._monotonic()
            self._log(f'[web-quick] 站点图标抓取已提交：source=editor, target={origin}, '
                      f'timeout={TOTAL_SECONDS:.0f}s')
            content = self._download(url, started + TOTAL_SECONDS)
            if content:
                self._write_success(key, content)
                self._prune()
                self._log(f'[web-quick] 站点图标抓取成功：target={origin}，'
                          f'耗时 {self._monotonic() - started:.2f} 秒')
                return self._result(key, True, _data_url(content), False)
            self._write_metadata(key, False)
            self._log(f'[web-quick] 站点图标未获取：target={origin}，'
                      f'耗时 {self._monotonic() - started:.2f} 秒')
            return self._result(key, bool(cached), _data_url(cached) if cached else '', bool(cached))

    @staticmethod
    def _result(key, found, data_url, stale):
        return {'ok': True, 'found': found, 'key': key, 'data_url': data_url, 'stale': stale}

    def _download(self, url, deadline):
        origin = canonical_origin(url)
        start_url = normalize_url(url)
        start_parts = urllib.parse.urlsplit(start_url)
        pages = [start_url]
        if start_parts.path not in ('', '/') or start_parts.query or start_parts.fragment:
            pages.append(origin + '/')
        candidates = []
        # 实际登录页常有自己的图标声明；没有声明时才补查站点根页。
        for page in pages:
            try:
                content, content_type, final_url = self._download_bytes(
                    page, MAX_HTML_BYTES, deadline, 'text/html,application/xhtml+xml')
                if 'html' not in content_type and not content.lstrip().startswith(b'<'):
                    continue
                parser = _IconLinks()
                parser.feed(content.decode('utf-8-sig', errors='replace'))
                if not parser.links:
                    continue
                try:
                    base = normalize_url(urllib.parse.urljoin(final_url, parser.base or final_url))
                except WebQuickError:
                    base = final_url
                for _, href in sorted(parser.links, key=lambda row: row[0]):
                    candidates.append(urllib.parse.urljoin(base, href))
                break
            except (WebQuickError, OSError, ValueError, http.client.HTTPException):
                continue
        candidates.extend(origin + path for path in _FALLBACK_PATHS)
        attempted = set()
        for candidate in candidates:
            if len(attempted) >= 4:
                break
            try:
                candidate = normalize_url(candidate)
                parts = urllib.parse.urlsplit(candidate)
                if parts.path.lower().endswith('.svg') or candidate in attempted:
                    continue
                if urllib.parse.urlsplit(origin).scheme == 'https' and parts.scheme.lower() != 'https':
                    continue
                attempted.add(candidate)
                content, content_type, _ = self._download_bytes(
                    candidate, MAX_IMAGE_BYTES, deadline, 'image/*,*/*;q=0.8')
                return self._convert(content, content_type)
            except (WebQuickError, OSError, ValueError, http.client.HTTPException):
                continue
        return None

    @staticmethod
    def _hostname_mismatch(url, exc):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme.lower() != 'https':
            return False
        try:
            ipaddress.ip_address(parts.hostname)
        except ValueError:
            return False
        reason = getattr(exc, 'reason', exc)
        if getattr(reason, 'verify_code', None) == 62:
            return True
        text = str(reason).lower()
        return any(marker in text for marker in
                   ('hostname mismatch', 'ip address mismatch', 'wrong principal', 'not valid for'))

    @staticmethod
    def _error_summary(exc):
        reason = getattr(exc, 'reason', exc)
        code = getattr(reason, 'verify_code', None)
        detail = f'{type(exc).__name__}/{type(reason).__name__}'
        return detail + (f' verify_code={code}' if code is not None else '')

    def _download_bytes(self, url, limit, deadline, accept):
        url = normalize_url(url)
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise WebQuickError('图标抓取超时')
        origin = canonical_origin(url)
        started = self._monotonic()
        self._log(f'[web-quick] 开始请求站点图标资源：target={origin}, source=editor, '
                  f'timeout={remaining:.1f}s')
        request = urllib.request.Request(url, headers={
            'Accept': accept, 'Accept-Encoding': 'identity',
            'User-Agent': 'CXToolkit-WebQuick-Favicon/1.0', 'Connection': 'close'})

        def fetch(opener):
            with opener(request, timeout=remaining) as response:
                headers = response.headers
                length = headers.get('Content-Length')
                if length is not None:
                    try:
                        declared = int(length)
                    except ValueError:
                        declared = None
                    if declared is not None and declared > limit:
                        raise WebQuickError('图标响应超过大小限制')
                content = response.read(limit + 1)
                if len(content) > limit:
                    raise WebQuickError('图标响应超过大小限制')
                content_type = str(headers.get('Content-Type', '')).split(';', 1)[0].strip().lower()
                final_url = response.geturl() if hasattr(response, 'geturl') else url
            return content, content_type, final_url

        try:
            result = fetch(self._opener)
        except Exception as exc:
            if not self._hostname_mismatch(url, exc):
                self._log(f'[web-quick] 站点图标资源请求失败：target={origin}，'
                          f'{self._error_summary(exc)}，耗时 {self._monotonic() - started:.2f} 秒')
                raise
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_REQUIRED
            self._log(f'[web-quick] 站点图标按用户确认策略重试：target={origin}，'
                      '仅忽略 IP 主机名不匹配，证书链仍强制校验')
            try:
                result = fetch(lambda req, timeout: self._context_opener(req, timeout, context))
            except Exception as exc:
                self._log(f'[web-quick] 站点图标受限重试失败：target={origin}，'
                          f'{self._error_summary(exc)}，耗时 {self._monotonic() - started:.2f} 秒')
                raise
        self._log(f'[web-quick] 站点图标资源请求完成：target={origin}，bytes={len(result[0])}，'
                  f'耗时 {self._monotonic() - started:.2f} 秒')
        return result

    @staticmethod
    def _flatten_background(image):
        """仅清理与四角一致的纯色底，保留主体；可能清空的图片保持原样。"""
        if image.width < 4 or image.height < 4:
            return image
        pixels = list(image.get_flattened_data() if hasattr(image, 'get_flattened_data') else image.getdata())
        border = [image.getpixel((x, y)) for x in range(image.width) for y in (0, image.height - 1)]
        border += [image.getpixel((x, y)) for y in range(image.height) for x in (0, image.width - 1)]
        colors = collections.Counter(pixel[:3] for pixel in border if pixel[3] > 0)
        if not colors:
            return image
        background, count = colors.most_common(1)[0]
        if count < max(2, len(border) // 8):
            return image
        flattened, removed = [], 0
        for pixel in pixels:
            distance = sum((pixel[index] - background[index]) ** 2 for index in range(3))
            if distance <= 55 ** 2:
                flattened.append((pixel[0], pixel[1], pixel[2], 0))
                removed += 1
            else:
                flattened.append(pixel)
        if removed < len(pixels):
            image.putdata(flattened)
        return image

    @staticmethod
    def _convert(content, content_type):
        # MIME 仅用于排除 SVG；其余由 Pillow 实际解码，兼容内网服务器错误响应头。
        if not content or content_type == 'image/svg+xml':
            raise WebQuickError('图标响应不是受支持的图片')
        from PIL import Image, UnidentifiedImageError
        try:
            with Image.open(io.BytesIO(content)) as picture:
                if picture.format not in _IMAGE_TYPES:
                    raise WebQuickError('图标图片格式不受支持')
                if picture.width < 1 or picture.height < 1 or \
                        picture.width * picture.height > MAX_PIXELS:
                    raise WebQuickError('图标图片尺寸无效或过大')
                picture.seek(0)
                picture.load()
                image = picture.convert('RGBA')
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
            raise WebQuickError('图标图片损坏或格式无效') from exc
        with image:
            image.thumbnail((64, 64), Image.Resampling.LANCZOS)
            image = FaviconService._flatten_background(image)
            output = io.BytesIO()
            image.save(output, format='PNG', optimize=True)
        result = output.getvalue()
        if len(result) > MAX_OUTPUT_BYTES:
            raise WebQuickError('图标转换结果超过大小限制')
        return result

    def _path(self, key, suffix):
        return os.path.join(self.root, key + suffix)

    def _read_png(self, key):
        path = self._path(key, '.png')
        try:
            if not os.path.isfile(path) or not 1 <= os.path.getsize(path) <= MAX_OUTPUT_BYTES:
                return b''
            with open(path, 'rb') as stream:
                content = stream.read(MAX_OUTPUT_BYTES + 1)
            return content if content.startswith(b'\x89PNG\r\n\x1a\n') and len(content) <= MAX_OUTPUT_BYTES else b''
        except OSError:
            return b''

    def _read_metadata(self, key):
        try:
            with open(self._path(key, '.json'), 'rb') as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                return {}
            value = json.loads(raw.decode('utf-8'))
            if (isinstance(value, dict) and isinstance(value.get('found'), bool) and
                    isinstance(value.get('timestamp'), (int, float))):
                return value
        except (OSError, UnicodeError, ValueError):
            pass
        return {}

    def _write_success(self, key, content):
        if self._atomic_write(self._path(key, '.png'), content):
            self._write_metadata(key, True)

    def _write_metadata(self, key, found):
        content = json.dumps({'found': found, 'timestamp': self._clock(),
                              'version': CACHE_METADATA_VERSION},
                             ensure_ascii=True, separators=(',', ':')).encode('utf-8')
        self._atomic_write(self._path(key, '.json'), content)

    def _atomic_write(self, path, content):
        temporary = path + '.' + uuid.uuid4().hex + '.tmp'
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(temporary, 'xb') as stream:
                stream.write(content)
            os.replace(temporary, path)
            return True
        except OSError as exc:
            self._log(f'[web-quick] 站点图标缓存写入失败：{type(exc).__name__}')
            try:
                os.remove(temporary)
            except OSError:
                pass
            return False

    def _prune(self):
        try:
            rows = []
            with os.scandir(self.root) as entries:
                for entry in entries:
                    if entry.is_file() and entry.name.endswith('.png') and len(entry.name) == 68:
                        rows.append((entry.stat().st_mtime, entry.name[:64]))
            rows.sort(reverse=True)
            for _, key in rows[MAX_CACHE_FILES:]:
                for suffix in ('.png', '.json'):
                    try:
                        os.remove(self._path(key, suffix))
                    except OSError:
                        pass
        except OSError:
            pass

# -*- coding: utf-8 -*-
"""应用版本信息的唯一来源。"""

APP_NAME = 'CXVPNTools'
APP_VERSION = '1.2.1'
APP_VERSION_TUPLE = (1, 2, 1, 0)
GITHUB_REPOSITORY = 'SeanZhang97/cxvpn-tools'


def normalized_version(value):
    value = str(value or '').strip().lstrip('vV')
    parts = value.split('.')
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        return ''
    return '.'.join(str(int(part)) for part in parts)


def is_newer(candidate, current=APP_VERSION):
    candidate = normalized_version(candidate)
    current = normalized_version(current)
    if not candidate or not current:
        return False
    return tuple(map(int, candidate.split('.'))) > tuple(map(int, current.split('.')))

# -*- coding: utf-8 -*-
"""隔离的打包产物恢复探针，不启动 GUI、worker、服务或真实网络。"""
import os
from pathlib import Path
import tempfile
import time

from core import state_store


def run(root, stage):
    target = Path(root).resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    if (temporary not in target.parents or not target.name.startswith('cxvpn-recovery-') or
            not (target / 'probe-allowed').is_file() or
            stage not in ('after-config', 'after-cache', 'before-commit', 'after-commit')):
        raise ValueError('恢复探针只能使用显式创建的隔离临时目录')

    def barrier(point):
        if stage != point:
            return
        with open(target / 'ready', 'w', encoding='utf-8') as stream:
            stream.write(point)
            stream.flush()
            os.fsync(stream.fileno())
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            time.sleep(.05)
        raise TimeoutError('恢复测试父进程未终止探针')

    key = 'probe:fp'
    with state_store.transaction(str(target)):
        state_store.save_config({'probe_version': 2}, str(target), links=[('probe', 'cache', key)])
        barrier('after-config')
        state_store.save_cache('cache', {'id': 'probe'}, b'new-body', {'node_count': 1}, key, str(target))
        barrier('after-cache')
        state_store.save_snapshot(key, {'nodes': [{'name': '节点 e\u0301 🇯🇵', 'tested': True,
            'tested_at': 2, 'delay': 20, 'alive': True}], 'updated_at': 2}, str(target))
        state_store.bind_config_data([('probe', 'cache', key)], str(target))
        barrier('before-commit')
    barrier('after-commit')

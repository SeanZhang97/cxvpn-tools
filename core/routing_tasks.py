# -*- coding: utf-8 -*-
"""网络任务在锁外运行，仅在提交缓存/快照时复核调用方版本。"""
from contextlib import contextmanager, nullcontext
import threading


_current = threading.local()


@contextmanager
def operation_scope(guard):
    previous = getattr(_current, 'guard', None)
    _current.guard = guard
    try:
        yield
    finally:
        _current.guard = previous


@contextmanager
def commit_scope():
    guard = getattr(_current, 'guard', None)
    if getattr(_current, 'committing', False):
        yield
        return
    with guard() if guard else nullcontext():
        _current.committing = True
        try:
            yield
        finally:
            _current.committing = False

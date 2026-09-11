# -*- coding: utf-8 -*-
"""网络任务在锁外运行，仅在提交缓存/快照时复核调用方版本。"""
from contextlib import contextmanager, nullcontext
import threading
from core import state_store


_current = threading.local()


@contextmanager
def operation_scope(guard):
    previous = getattr(_current, 'guard', None)
    previous_revision = getattr(_current, 'revision', None)
    _current.guard = guard
    _current.revision = state_store.current_revision()
    try:
        yield
    finally:
        _current.guard = previous
        _current.revision = previous_revision


@contextmanager
def commit_scope():
    guard = getattr(_current, 'guard', None)
    if getattr(_current, 'committing', False):
        yield
        return
    with guard() if guard else nullcontext():
        _current.committing = True
        try:
            with state_store.transaction(expected_revision=getattr(_current, 'revision', None)):
                yield
        finally:
            _current.committing = False

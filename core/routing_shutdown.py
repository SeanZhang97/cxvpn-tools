"""路由提交互斥与退出时的提交入口封闭。"""
import threading


class RoutingSubmissionLock:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = threading.Lock()
        self._closing = False

    def acquire(self, blocking=True, timeout=-1):
        with self._state:
            if self._closing:
                return False
        acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            with self._state:
                if self._closing:
                    self._lock.release()
                    return False
        return acquired

    def release(self):
        self._lock.release()

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError('正在退出，未提交新的代理操作，请稍后重试')
        return self

    def __exit__(self, *args):
        self.release()

    def drain(self, timeout):
        with self._state:
            self._closing = True
        if not self._lock.acquire(timeout=timeout):
            return False
        self._lock.release()
        return True

    def reopen(self):
        with self._state:
            self._closing = False

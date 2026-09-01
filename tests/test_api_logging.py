# -*- coding: utf-8 -*-
import os
import tempfile
import threading
import unittest
from unittest import mock

import api


class GbkConsole:
    encoding = 'gbk'

    def __init__(self):
        self.value = ''

    def write(self, value):
        value.encode(self.encoding)
        self.value += value

    def flush(self):
        pass


class ApiLoggingTests(unittest.TestCase):
    def test_non_bmp_node_name_never_breaks_logging(self):
        instance = api.Api.__new__(api.Api)
        instance._lock = threading.Lock()
        instance.logs = []
        flag = '\U0001f1ef\U0001f1f5'
        console = GbkConsole()

        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(api.cfgmod, 'BASE', root), \
                mock.patch.object(api.sys, 'stdout', console):
            instance.log(f'[routing] 节点 {flag} 日本东京')
            with open(os.path.join(root, 'run.log'), encoding='utf-8') as stream:
                persisted = stream.read()

        self.assertIn(flag, persisted)
        self.assertIn(r'\U0001f1ef\U0001f1f5', console.value)


if __name__ == '__main__':
    unittest.main()

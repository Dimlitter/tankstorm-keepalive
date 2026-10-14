# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""测试用的替身：假录制器、假 socket、假服务端、假时钟。

每个测试文件都要**先** import 本模块，再 import tankstorm：这里先给 "tankstorm"
logger 挂上 NullHandler，log.get_logger() 看到已有 handler 就不会再往
logs/ 里开日志文件，测试不会污染真实日志。

不连网络、不碰账号。测试数据全是编的，不含任何真实 uid / 昵称。
"""

import contextlib
import logging
import os
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_log = logging.getLogger("tankstorm")
if not _log.handlers:
    _log.addHandler(logging.NullHandler())
    _log.propagate = False

from tankstorm import schema, sender  # noqa: E402


class FakeRec:
    """够请求原语用的最小录制器：latest / recent / seq_mark / rc4_c2s / uid。"""

    def __init__(self, uid="1000"):
        self.latest, self.recent, self._seq = {}, {}, 0
        self.rc4_c2s = None
        self.uid = uid

    def seq_mark(self):
        return self._seq

    def push(self, name, data):
        self._seq += 1
        self.latest[name] = (self._seq, data)
        hist = self.recent.setdefault(name, [])
        hist.append((self._seq, data))
        del hist[:-8]


class FakeSock:
    """recv 永远立刻返回一点数据（连接活着），sendall 只计数。"""

    def __init__(self):
        self.sent = []

    def settimeout(self, t):
        pass

    def recv(self, n):
        return b"x"

    def sendall(self, b):
        self.sent.append(bytes(b))


class Clock:
    """假时钟：每次读 time.time() 往前走 10ms，sleep 直接把时间拨过去。

    等回包的循环靠截止时刻退出，用真时钟的话"没回包"的分支要真等 6 秒。
    从真实的当前时刻起步：date.today() 内部也调 time.time()，起点离得太远，
    每日状态的"今天"就对不上了。
    """

    def __init__(self, start=None):
        self.now = time.time() if start is None else start

    def time(self):
        self.now += 0.01
        return self.now

    def sleep(self, s):
        self.now += max(0.0, s)


@contextlib.contextmanager
def fake_server(handler, clock=None):
    """把 sender.send_frame 换成"解码请求 → 交给 handler(op, 字段字典)"。

    handler 负责往录制器里 push 回包。产出 (sent, clock)，sent 是
    [(opcode, 字段字典), ...]。
    """
    clock = clock or Clock()
    sent = []

    def send_frame(sock, op, body, rc4=None):
        d = schema.decode(body, op) or {}
        sent.append((op, d))
        handler(op, d)

    with mock.patch.object(sender, "send_frame", send_frame), \
            mock.patch("time.time", clock.time), \
            mock.patch("time.sleep", clock.sleep):
        yield sent, clock


@contextlib.contextmanager
def temp_daily_state():
    """把每日任务的状态文件指到临时目录，测试绝不能写真实的 logs/daily-state.json。"""
    from tankstorm.daily import state
    with tempfile.TemporaryDirectory() as d:
        with mock.patch.object(state, "STATE_FILE",
                               os.path.join(d, "daily-state.json")):
            yield state

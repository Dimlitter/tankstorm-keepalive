# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""连接层：对着本机临时起的 TCP 服务端走一遍连接、登录、心跳、断开。

录制关掉（不落盘），游戏页参数用编的 ctx 顶替，不连外网。
"""
import socket
import threading
import unittest
from unittest import mock

import fakes  # noqa: F401

from tankstorm import jobs, protocol, session, socket_keepalive, wiremap

NO_RECORDING = {"录制": {"启用": False, "实时解密": False, "原始流": {"启用": False}}}


class LocalServer(threading.Thread):
    """收下登录字节，等到第一次心跳就主动断开。"""

    def __init__(self, hb="0006040e00000000"):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.received = b""
        self.hb = hb

    def run(self):
        conn, _ = self.srv.accept()
        conn.settimeout(5)
        hb = bytes.fromhex(self.hb)
        try:
            while hb not in self.received:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                self.received += chunk
        finally:
            conn.close()
            self.srv.close()


class FakeQQ:
    uin = "10001"

    def is_valid(self):
        return True


def ctx_for(port, version="2026101403"):
    return {"openkey": "k", "uid": "1000", "secret": "s", "sid": "1",
            "level": "35", "firstLogin": "false", "version": version,
            "server": "127.0.0.1", "port": str(port)}


class SessionTest(unittest.TestCase):
    def setUp(self):
        # open_link 会按 ctx 里的游戏版本切换全局映射，测完换回来
        self.addCleanup(setattr, wiremap, "ACTIVE", wiremap.ACTIVE)
        # 登录推送这里没有，收 0.2 秒就够；假服务端只等 5 秒心跳
        real_drain = session.drain
        patcher = mock.patch.object(session, "drain", lambda s, seconds=6.0: real_drain(s, 0.2))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unverifiable_guess_sends_nothing_after_login(self):
        # 没见过的版本借用对照表试连；录制和解密都关着，没法核对，就不该发心跳
        self.addCleanup(wiremap._REJECTED.pop, "2099010101", None)
        srv = LocalServer(hb="0006043e00000000")
        srv.start()
        spec = dict(protocol.load_spec(), http_warmup=False)
        rec = socket_keepalive.Recorder(NO_RECORDING)
        with mock.patch.object(socket_keepalive, "get_game_context",
                               lambda qq: ctx_for(srv.port, "2099010101")):
            reason = socket_keepalive._one_session(FakeQQ(), spec, NO_RECORDING, rec,
                                                   with_daily=False)
        srv.join(5)
        rec.close()
        self.assertIn("核对没通过", reason)
        self.assertTrue(srv.received.startswith(b"tgw_"))              # 握手照发
        self.assertNotIn(bytes.fromhex("0006043e00000000"), srv.received)   # 心跳没发
        with self.assertRaises(wiremap.UnsupportedVersion):           # 之后连 TCP 都不连
            session.open_link(spec, ctx_for(srv.port, "2099010101"), rec)

    def test_new_protocol_version_is_translated_on_the_wire(self):
        srv = LocalServer(hb="0006043e00000000")
        srv.start()
        spec = dict(protocol.load_spec(), http_warmup=False)
        rec = socket_keepalive.Recorder(NO_RECORDING)
        with mock.patch.object(socket_keepalive, "get_game_context",
                               lambda qq: ctx_for(srv.port, "2026102102")):
            reason = socket_keepalive._one_session(FakeQQ(), spec, NO_RECORDING, rec,
                                                   with_daily=False)
        srv.join(5)
        rec.close()
        self.assertIn("服务器关闭连接", reason)
        self.assertIn(bytes.fromhex("0023044a00000000"), srv.received)   # RceInit
        self.assertNotIn(bytes.fromhex("0023041c"), srv.received)        # 旧编号一个都不发
        self.assertNotIn(bytes.fromhex("0028041d"), srv.received)        # 旧 041d 现在是购买
    def test_keepalive_session_logs_in_beats_and_reports_close(self):
        srv = LocalServer()
        srv.start()
        spec = dict(protocol.load_spec(), http_warmup=False)
        rec = socket_keepalive.Recorder(NO_RECORDING)
        with mock.patch.object(socket_keepalive, "get_game_context",
                               lambda qq: ctx_for(srv.port)):
            reason = socket_keepalive._one_session(FakeQQ(), spec, NO_RECORDING, rec,
                                                   with_daily=False)
        srv.join(5)
        rec.close()
        self.assertIn("服务器关闭连接", reason)
        self.assertTrue(srv.received.startswith(b"tgw_"))
        self.assertIn(b"a,1000,s", srv.received)

    def test_run_once_hands_logged_in_socket_to_work(self):
        srv = LocalServer()
        srv.start()
        seen = {}

        def work(rec, sock, spec, ctx):
            seen["uid"] = ctx["uid"]
            session.make_beater(sock, spec, ctx, {}).sock.sendall(
                protocol.build_heartbeat(spec, ctx))
            return 0

        with mock.patch.object(session, "get_game_context", lambda qq: ctx_for(srv.port)):
            code = session.run_once(FakeQQ(), NO_RECORDING, work)
        srv.join(5)
        self.assertEqual(code, 0)
        self.assertEqual(seen["uid"], "1000")

    def test_connect_failure_is_exit_code_1(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                                       # 没人监听的端口
        with mock.patch.object(session, "get_game_context", lambda qq: ctx_for(port)):
            self.assertEqual(session.run_once(FakeQQ(), NO_RECORDING,
                                              lambda *a: 0), 1)

    def test_summary_counts_only_acted(self):
        sent = []
        with mock.patch.object(jobs.notify, "send",
                               lambda cfg, title, html, template="txt": sent.append(title)):
            jobs.push_daily_summary({}, {"a": "未开启", "b": "成功：ret=0",
                                         "c": "服务器返回 ret=11"}, {})
            jobs.push_daily_summary({}, {"a": "冷却中，还需 3 分钟"}, {})
        self.assertEqual(sent, ["坦克风暴每日任务：成功 1，未成 1"])


if __name__ == "__main__":
    unittest.main()

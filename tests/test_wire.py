# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
import unittest

from fakes import Clock, FakeRec, FakeSock, fake_server

from tankstorm import wire


class ReadPathTest(unittest.TestCase):
    DATA = {
        "freeVisitCnt": [3, 1, 0],
        "sVisitData": {"field4": [0, 1]},
        "field14": [{"field1": 1, "field2": 0}, {"field1": 0, "field2": 99}],
        "field4": [{"field1": 2, "field2": 3, "field3": 0},
                   {"field1": 1, "field2": 0, "field3": 1786609115}],
        "single": {"field1": 7},
    }

    def test_plain_and_nested(self):
        self.assertEqual(wire.read_path(self.DATA, "freeVisitCnt"), [3, 1, 0])
        self.assertEqual(wire.read_path(self.DATA, "sVisitData.field4"), [0, 1])
        self.assertIsNone(wire.read_path(self.DATA, "sVisitData.nope"))
        self.assertIsNone(wire.read_path(None, "freeVisitCnt"))

    def test_pluck_returns_list(self):
        self.assertEqual(wire.read_path(self.DATA, "field14[]"), self.DATA["field14"])
        # 单个元素被解码成 dict 时也要包成列表
        self.assertEqual(wire.read_path(self.DATA, "single[]"), [{"field1": 7}])

    def test_select_by_marker_not_index(self):
        # 免费档 field1==1 排在第二位，按标记挑才挑得对
        self.assertEqual(wire.read_path(self.DATA, "field4[field1=1].field2"), 0)
        self.assertEqual(wire.read_path(self.DATA, "field4[field1=1].field3"), 1786609115)
        self.assertIsNone(wire.read_path(self.DATA, "field4[field1=9].field2"))

    def test_is_int_rejects_bool(self):
        self.assertTrue(wire.is_int(0))
        self.assertFalse(wire.is_int(True))
        self.assertFalse(wire.is_int("1"))


class AwaitResponseTest(unittest.TestCase):
    def test_only_newer_than_since(self):
        rec = FakeRec()
        rec.push("RseX", {"type": 0})
        since = rec.seq_mark()
        with fake_server(lambda op, d: None):
            self.assertIsNone(wire.await_response(FakeSock(), rec, "RseX", since, 1.0))
            rec.push("RseX", {"type": 1})
            self.assertEqual(wire.await_response(FakeSock(), rec, "RseX", since, 1.0),
                             {"type": 1})

    def test_want_then_relaxed(self):
        rec = FakeRec()
        rec.push("RseX", {"type": 0, "cnt": 3})
        rec.push("RseX", {"type": 3})            # 同名第二条把 latest 冲掉
        with fake_server(lambda op, d: None):
            got = wire.await_response(FakeSock(), rec, "RseX", 0, 1.0,
                                      want=lambda d: d.get("type") == 1,
                                      relaxed=lambda d: "cnt" in d)
        self.assertEqual(got, {"type": 0, "cnt": 3})

    def test_send_returns_mark_before_reply(self):
        rec = FakeRec()

        def server(op, d):
            rec.push("RseX", {"type": d.get("type")})

        with fake_server(server) as (sent, _):
            since = wire.send(FakeSock(), rec, "0463", {4: ("int32", 2)})
            self.assertEqual(since, 0)
            self.assertEqual(sent, [("0463", {"type": 2})])


class HeartbeatTest(unittest.TestCase):
    def test_nested_restores_outer(self):
        calls = []
        outer = lambda: calls.append("outer")      # noqa: E731
        inner = lambda: calls.append("inner")      # noqa: E731
        with wire.heartbeat(outer):
            with wire.heartbeat(None):             # None 不能把外层的摘掉
                wire.beat()
            with wire.heartbeat(inner):
                wire.beat()
            wire.beat()
        wire.beat()                                # 退出后没有心跳器
        self.assertEqual(calls, ["outer", "inner", "outer"])

    def test_beater_sends_only_when_due(self):
        clock = Clock()
        sock = FakeSock()
        with fake_server(lambda op, d: None, clock):
            b = wire.Beater(sock, b"\x00\x06", 10)
            b()
            clock.sleep(10)
            b()
            b()
        self.assertEqual(b.count, 1)
        self.assertEqual(sock.sent, [b"\x00\x06"])

    def test_nap_beats_while_waiting(self):
        clock = Clock()
        sock = FakeSock()
        with fake_server(lambda op, d: None, clock):
            with wire.heartbeat(wire.Beater(sock, b"hb", 10)):
                wire.nap(35)
        self.assertGreaterEqual(len(sock.sent), 3)


if __name__ == "__main__":
    unittest.main()

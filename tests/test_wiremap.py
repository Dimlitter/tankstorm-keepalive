# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""协议编号翻译：2026102102 起，游戏更新会打乱 opcode 和字段号。

下面写死的线上编号都是对着 RedWar_2026102102.swf 的字节码手工核过的：
常量类 C2S_/S2C_ 给出心跳、握手、广播的 opcode，RseSuperStormOpt 的字段号
是直接读它的 writeToBuffer 得到的（type=5 nResult=4 deftUid=6 deftName=2
atkUid=3 atkName=1），不依赖 tools/build_wiremap.py 的推导。
"""
import glob
import os
import re
import struct
import tempfile
import unittest
from unittest import mock

import fakes  # noqa: F401

from tankstorm import crypto, protocol, recorder, schema, sender, session, wiremap
from tankstorm.proto_encode import encode_message
from tankstorm.recorder import Recorder

NEW = "2026102102"
KEY = b"Redwarhq2018HoneyHoneyHoney"


def _op(name):
    return next(o for o, e in schema.SCHEMA.items() if e["name"] == name)


class VersionTest(unittest.TestCase):
    def test_canonical_version_needs_no_translation(self):
        m = wiremap.for_version("2026101403")
        self.assertTrue(m.identity)
        self.assertEqual(m.to_wire("040e", b""), ("040e", b""))

    def test_unknown_version_borrows_the_latest_table(self):
        # 游戏更新未必动协议：先按最近一个已适配版本的编号试，登录后再核对
        for v in ("2099010101", "", "_canon"):
            m = wiremap.for_version(v)
            self.assertEqual(m.assumed_from, NEW)
            self.assertEqual(m.op_out("040e"), "043e")
        self.assertIsNone(wiremap.for_version(NEW).assumed_from)

    def test_rejected_version_is_refused_before_connecting(self):
        self.addCleanup(wiremap._REJECTED.pop, "2099010101", None)
        wiremap.reject("2099010101", "测试")
        with mock.patch("socket.create_connection") as conn, \
                self.assertRaises(wiremap.UnsupportedVersion) as cm:
            session.open_link({}, {"version": "2099010101"}, None)
        conn.assert_not_called()
        self.assertIsInstance(cm.exception, OSError)


def _sample_bodies(n=15):
    """挑 n 条简单字段多的下行消息，按 schema 类型造 body（本项目编号）。"""
    out = []
    for op, e in sorted(schema.SCHEMA.items()):
        simple = {int(t): ty for t, (_, ty) in e["fields"].items()
                  if ty in ("int32", "string", "bool")}
        if not e["name"].startswith("Rse") or len(simple) < 8:
            continue
        values = {"int32": 7, "string": "x", "bool": True}
        out.append((op, encode_message({t: (ty, values[ty]) for t, ty in simple.items()},
                                       omit_zero=False)))
        if len(out) == n:
            break
    return out


class VerifyTest(unittest.TestCase):
    """登录后核对：对的对照表放行，错的拦下。"""

    CTX = {"uid": "1000", "sid": "42", "level": "35", "firstLogin": "false"}

    def _login_burst(self, speak, listen, decrypt=True):
        """服务端按 speak 版本的编号推一批登录消息，录制器按 listen 版本去解。"""
        auth = _op("RseAuthState")
        tag = next(int(t) for t, (n, _) in schema.SCHEMA[auth]["fields"].items()
                   if n == "nCurTime")
        bodies = _sample_bodies() + [(auth, encode_message({tag: ("int32", 1792581501)}))]
        server = crypto.from_ctx(self.CTX)[0]["s2c"]
        with tempfile.TemporaryDirectory() as d, mock.patch.object(recorder, "LOG_DIR", d), \
                wiremap.using(speak) as out, wiremap.using(listen) as m:
            rec = Recorder({"录制": {"原始流": {"启用": False}, "实时解密": decrypt}})
            rec.on_connect()
            rec.enable_crypto(self.CTX)
            for op, body in bodies:
                wop, wbody = out.to_wire(op, body)
                if m.op_in(wop) not in crypto.EXEMPT["s2c"]:   # 和录制器的加密判定保持一致
                    wbody = server.crypt(wbody)
                rec.feed(sender.build_frame(wop, wbody))
            rec.close()
            return wiremap.verify(rec, now=1792581501 + 30)

    def test_right_table_passes(self):
        ok, why = self._login_burst(NEW, NEW)
        self.assertTrue(ok, why)
        ok, why = self._login_burst(NEW, "2099010101")      # 借用的对照表恰好没变
        self.assertTrue(ok, why)

    def test_reshuffled_protocol_is_caught(self):
        ok, why = self._login_burst(NEW, "2026101403")      # 服务端已打乱，脚本还按旧编号
        self.assertFalse(ok)
        ok, why = self._login_burst("2026101403", "2099010101")
        self.assertFalse(ok)

    def _rec(self, check, auth_time, seq=5):
        rec = mock.Mock(decrypt_ok=True, wire_check=check, connect_seq=2,
                        latest={"RseAuthState": (seq, {"nCurTime": auth_time})})
        return wiremap.verify(rec, now=1792581501)

    def test_each_rule_on_its_own(self):
        with wiremap.using(NEW):
            self.assertTrue(self._rec([50, 1000, 3], 1792581501)[0])
            # 字段类型大面积对不上：真实流拿错的对照表去解是 18%~57%
            ok, why = self._rec([50, 500, 400], 1792581501)
            self.assertFalse(ok)
            self.assertIn("编号对不上", why)
            # 服务端时间差太多，或者 RseAuthState 是上一次连接留下的
            self.assertFalse(self._rec([50, 1000, 3], 1792581501 + 7200)[0])
            self.assertFalse(self._rec([50, 1000, 3], 1792581501, seq=1)[0])
            self.assertFalse(self._rec([5, 80, 0], 1792581501)[0])        # 太少，不够判断

    def test_without_decryption_only_known_versions_pass(self):
        self.assertTrue(self._login_burst(NEW, NEW, decrypt=False)[0])
        self.assertFalse(self._login_burst(NEW, "2099010101", decrypt=False)[0])

    def test_failed_check_rejects_the_version(self):
        self.addCleanup(wiremap._REJECTED.pop, "2099010101", None)
        with mock.patch.object(session.notify, "send") as push, \
                mock.patch.object(wiremap, "verify", return_value=(False, "测试")):
            self.assertEqual(session.check_protocol(None, {"version": "2099010101"}, {}), "测试")
        push.assert_called_once()
        with self.assertRaises(wiremap.UnsupportedVersion):
            wiremap.for_version("2099010101")


class NewVersionTest(unittest.TestCase):
    def setUp(self):
        ctx = wiremap.using(NEW)
        self.m = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def test_exempt_messages_land_on_the_new_exempt_lists(self):
        # Transport 静态构造里的两个豁免数组
        self.assertEqual({self.m.op_out(o) for o in crypto.EXEMPT["c2s"]},
                         {"043e", "044a", "0451", "04ee"})
        self.assertEqual({self.m.op_out(o) for o in crypto.EXEMPT["s2c"]},
                         {"0237", "032a", "0200", "0246", "0298"})

    def test_login_and_heartbeat_frames(self):
        spec = protocol.load_spec()
        steps = [d for d, _ in protocol.build_login_sequence(
            spec, {"uid": "1000", "secret": "s", "version": NEW})]
        self.assertTrue(steps[0].startswith(b"tgw_"))
        self.assertEqual(steps[1], struct.pack(">H", 8) + b"a,1000,s")
        # RceInit：versionKey 从字段 3 挪到字段 7
        self.assertEqual(steps[2], bytes.fromhex("0023044a00000000") + b"\x3a\x1b" + KEY)
        # RceLoad：按声明顺序 baseid type h hn enterType 写，字段号 5 6 2 4 3
        self.assertEqual(steps[3].hex(),
                         "00280451000000002a0130" + "3205" + b"build".hex()
                         + "1209" + b"undefined".hex() + "2209" + b"undefined".hex()
                         + "1800")
        self.assertEqual(protocol.build_heartbeat(spec, {}).hex(), "0006043e00000000")

    def test_incoming_super_storm_is_renumbered(self):
        wire = encode_message({5: ("int32", 1), 4: ("int32", 0), 6: ("string", "111"),
                               2: ("string", "me"), 3: ("string", "222"),
                               1: ("string", "atk")}, omit_zero=False)
        op, body = self.m.from_wire("028c", wire)
        self.assertEqual(op, "027c")
        self.assertEqual(schema.decode(body, op),
                         {"type": 1, "nResult": 0, "deftUid": "111", "deftName": "me",
                          "atkUid": "222", "atkName": "atk"})

    def test_reject_super_storm_goes_out_renumbered(self):
        sent = []
        sock = mock.Mock(sendall=sent.append)
        with mock.patch.object(sender.time, "sleep"):
            sender.send_reject_super_storm(
                sock, crypto.RC4(b"0123456789", False),
                {"atkUid": "222", "atkName": "atk", "deftUid": "111", "deftName": "me"})
        frame = sent[0]
        self.assertEqual(frame[2:4].hex(), "045e")        # 不再是 04ab

    def test_requests_roundtrip(self):
        op = _op("RceArenaOpt")
        body = encode_message({1: ("int32", 1), 2: ("string", "1000"), 3: ("string", "9"),
                               6: ("int32", 3)}, omit_zero=False)
        wop, wbody = self.m.to_wire(op, body)
        self.assertEqual(wop, "0425")
        self.assertNotEqual(wbody, body)                 # 字段号确实换了
        self.assertEqual(self.m.from_wire(wop, wbody), (op, body))

    def test_nested_messages_are_renumbered(self):
        # RseCountryOpt.countryData.field13 是国战行动力，嵌套消息里的字段号同样被打乱
        op = _op("RseCountryOpt")
        ctag = next(int(t) for t, (n, _) in schema.SCHEMA[op]["fields"].items()
                    if n == "countryData")
        wtag, nested, _ = self.m._out[self.m._msg[op]][ctag]
        inner = self.m._out[nested][13][0]
        self.assertNotEqual((wtag, inner), (ctag, 13))
        wire = encode_message({wtag: ("bytes", encode_message({inner: ("int32", 121)}))})
        _, body = self.m.from_wire(self.m.op_out(op), wire)
        self.assertEqual(schema.decode(body, op)["countryData"], {"field13": 121})

    def test_unmapped_request_is_refused_before_touching_rc4(self):
        unmapped = sorted(set(schema.SCHEMA) - set(self.m._op_out))
        self.assertTrue(unmapped)
        rc4 = crypto.RC4(b"0123456789", False)
        before = (rc4.i, rc4.j, list(rc4.S))
        sock = mock.Mock()
        with self.assertRaises(wiremap.Unmapped):
            sender.send_frame(sock, unmapped[0], b"\x08\x01", rc4)
        sock.sendall.assert_not_called()
        self.assertEqual((rc4.i, rc4.j, list(rc4.S)), before)

    def test_unknown_incoming_opcode_cannot_collide(self):
        unknown = next(o for o in ("0254", "030e", "040c", "0422") if o not in self.m._op_in)
        self.assertGreaterEqual(int(self.m.op_in(unknown), 16), 0x8000)

    def test_recorder_decrypts_then_renumbers(self):
        ctx = {"uid": "1000", "sid": "42", "level": "35", "firstLogin": "false"}
        server = crypto.from_ctx(ctx)[0]["s2c"]
        op = _op("RseDailySignIn")
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(recorder, "LOG_DIR", d):
                rec = Recorder({"录制": {"原始流": {"启用": False}}})
                rec.enable_crypto(ctx)
                for days in range(8, 14):
                    body = encode_message({2: ("int32", 1), 6: ("bool", True),
                                           8: ("int32", days)})
                    wop, wbody = self.m.to_wire(op, body)
                    rec.feed(sender.build_frame(wop, server.crypt(wbody)))
                rec.close()
        self.assertEqual(rec._crypto, "ok")
        self.assertEqual(rec.latest["RseDailySignIn"][1],
                         {"nType": 1, "bSignIn": True, "nSignInDays": 13})
        self.assertEqual(rec.counts, {op: 6})

    def test_every_request_the_code_sends_is_mapped_in_every_version(self):
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tankstorm")
        used = set()
        for path in glob.glob(os.path.join(root, "**", "*.py"), recursive=True):
            with open(path, encoding="utf-8") as f:
                used |= set(re.findall(r"[\"'](04[0-9a-f]{2})[\"']", f.read()))
        self.assertTrue(used)
        versions = [v for v in wiremap._load() if not v.startswith("_")]
        self.assertIn(NEW, versions)
        for v in versions:
            m = wiremap.for_version(v)
            self.assertEqual(sorted(o for o in used if o not in m._op_out), [], v)


if __name__ == "__main__":
    unittest.main()

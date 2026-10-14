# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""帧、protobuf 编解码、RC4 密钥推导、分帧器。"""
import struct
import tempfile
import unittest
from unittest import mock

import fakes  # noqa: F401

from tankstorm import crypto, protocol, recorder, schema, sender
from tankstorm.proto_encode import encode_message
from tankstorm.recorder import FrameReader, Recorder


class EncodeTest(unittest.TestCase):
    def test_zero_fields_kept_when_asked(self):
        f = {3: ("int32", 1), 4: ("int32", 0)}
        self.assertEqual(encode_message(f, omit_zero=False), b"\x18\x01\x20\x00")
        self.assertEqual(encode_message(f), b"\x18\x01")

    def test_roundtrip_through_schema(self):
        body = encode_message({1: ("int32", 1), 2: ("int32", 0)}, omit_zero=False)
        self.assertEqual(schema.decode(body, "048a"), {"OptType": 1, "ActiveType": 0})
        self.assertTrue(schema.is_protobuf(body))
        self.assertFalse(schema.is_protobuf(b"\x07\xff\xff"))

    def test_frame_layout(self):
        frame = sender.build_frame("040e", b"")
        self.assertEqual(frame.hex(), "0006040e00000000")


class CryptoTest(unittest.TestCase):
    def test_middle(self):
        self.assertEqual(crypto.middle(35, "false"), 3501)
        self.assertEqual(crypto.middle("35", "true"), 3500)

    def test_key_layout(self):
        self.assertEqual(crypto.make_key("s2c", "1000", 3501, "089"), b"10003501089")
        self.assertEqual(crypto.make_key("c2s", "1000", 3501, "089"), b"08910003501")

    def test_rc4_roundtrip_and_stream(self):
        key = crypto.make_key("c2s", "1000", 3501, "123456")
        a, b = crypto.RC4(key, False), crypto.RC4(key, False)
        msg = b"hello world"
        self.assertEqual(b.crypt(a.crypt(msg)), msg)
        # 密钥流连续累积：同一实例再加密一次，结果必然不同
        self.assertNotEqual(a.crypt(msg), crypto.RC4(key, False).crypt(msg))

    def test_from_ctx_needs_material(self):
        self.assertIsNone(crypto.from_ctx({"uid": "1000"})[0])
        rc4, _ = crypto.from_ctx({"uid": "1000", "sid": "1", "level": "35",
                                  "firstLogin": "false"})
        self.assertEqual(set(rc4), {"s2c", "c2s"})

    def test_heartbeat_is_exempt(self):
        self.assertIn("040e", crypto.EXEMPT["c2s"])
        self.assertEqual(sender.encrypt_body("040e", b"\x01", None), b"\x01")
        with self.assertRaises(RuntimeError):
            sender.encrypt_body("0463", b"\x01", None)


class FrameReaderTest(unittest.TestCase):
    def test_gateway_preamble_and_split_frames(self):
        r = FrameReader()
        tgw = b"tgw_l7_forward\r\nHost: x:8001\r\n\r\n"
        f1 = sender.build_frame("040e", b"")
        f2 = sender.build_frame("041c", b"\x1a\x01a")
        data = tgw + f1 + f2
        got = r.feed(data[:20]) + r.feed(data[20:41]) + r.feed(data[41:])
        self.assertEqual([(op, body) for op, _seq, body, _off in got],
                         [("040e", b""), ("041c", b"\x1a\x01a")])

    def test_desync_drops_buffer(self):
        seen = []
        r = FrameReader(on_desync=lambda *a: seen.append(a))
        r._preamble = False
        self.assertEqual(r.feed(struct.pack(">H", 3) + b"abc"), [])
        self.assertTrue(seen)
        self.assertEqual(r.pending(), 0)


class RecorderDecryptTest(unittest.TestCase):
    CTX = {"uid": "1000", "sid": "42", "level": "35", "firstLogin": "false"}

    def test_live_decrypt_fills_latest(self):
        op = next(o for o, e in schema.SCHEMA.items() if e["name"] == "RseDailySignIn")
        server = crypto.from_ctx(self.CTX)[0]["s2c"]       # 服务端那一侧的密钥流
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(recorder, "LOG_DIR", d):
                rec = Recorder({"录制": {"原始流": {"启用": False}}})
                rec.enable_crypto(self.CTX)
                for days in range(8, 14):
                    # 自检只数 4 字节以上的 body
                    body = encode_message({2: ("int32", 1), 6: ("bool", True),
                                           8: ("int32", days)})
                    rec.feed(sender.build_frame(op, server.crypt(body)))
                rec.close()
        self.assertEqual(rec._crypto, "ok")                  # 自检 6 条全过
        seq, data = rec.latest["RseDailySignIn"]
        self.assertEqual(seq, 6)
        self.assertEqual(data, {"nType": 1, "bSignIn": True, "nSignInDays": 13})
        self.assertEqual(len(rec.recent["RseDailySignIn"]), 6)
        self.assertEqual(rec.uid, "1000")


class ProtocolSpecTest(unittest.TestCase):
    def test_bundled_spec_builds_login(self):
        spec = protocol.load_spec()
        steps = protocol.build_login_sequence(spec, {"uid": "1000", "secret": "s"})
        self.assertEqual(len(steps), 4)
        self.assertTrue(steps[0][0].startswith(b"tgw_"))
        self.assertEqual(steps[1][0], struct.pack(">H", 8) + b"a,1000,s")
        self.assertEqual(protocol.build_heartbeat(spec, {}).hex(), "0006040e00000000")


if __name__ == "__main__":
    unittest.main()

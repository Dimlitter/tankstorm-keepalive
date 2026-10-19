# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""闸门、安全检查、回包判据、成败判定，以及任务表本身必须满足的约束。"""
import unittest

import fakes  # noqa: F401  （先挂 NullHandler）

from tankstorm import schema
from tankstorm.daily import ALLOWED_OPCODES, TASKS, ordered_tasks
from tankstorm.daily.checks import (check_gate, check_safety, echo_want,
                                    field_names, read_counts)
from tankstorm.daily.judge import judge, show
from tankstorm.daily.model import Gate, Tiers
from tankstorm.proto_encode import encode_message

TASK = {t.key: t for t in TASKS}


class GateTest(unittest.TestCase):
    def test_tiers_take_first_tier_with_count(self):
        g, tiers = Gate("RseHeroOpen", "freeVisitCnt"), Tiers(4, [0, 1, 2])
        ok, _why, exhausted, tier = check_gate(g, {"freeVisitCnt": [0, 1, 0]}, tiers)
        self.assertEqual((ok, exhausted, tier), (True, False, 1))
        ok, _why, exhausted, tier = check_gate(g, {"freeVisitCnt": [0, 0, 0]}, tiers)
        self.assertEqual((ok, exhausted, tier), (False, True, None))

    def test_index_not_max(self):
        # 用完低级后是 [0,1,0]，不分档的闸门只看约定的那一档
        g = Gate("RseHeroOpen", "freeVisitCnt", index=0)
        ok, why, exhausted, _ = check_gate(g, {"freeVisitCnt": [0, 1, 0]})
        self.assertFalse(ok)
        self.assertTrue(exhausted)
        self.assertIn("已用完", why)

    def test_unreadable_is_not_exhausted(self):
        g = Gate("RseX", "freeVisitCnt")
        for data in (None, {}, {"freeVisitCnt": True}):
            ok, _why, exhausted, _ = check_gate(g, data)
            self.assertFalse(ok)
            self.assertFalse(exhausted, data)

    def test_claimed_flag(self):
        g = Gate("RseDailySignIn", "bSignIn", claimed_flag=True)
        self.assertTrue(check_gate(g, {"bSignIn": False})[0])
        ok, _why, exhausted, _ = check_gate(g, {"bSignIn": True})
        self.assertEqual((ok, exhausted), (False, True))
        self.assertFalse(check_gate(g, {"bSignIn": 0})[0])   # 不是布尔就不认

    def test_pluck_counts(self):
        data = {"field14": [{"field1": 1}, {"field2": 5}]}
        self.assertEqual(read_counts(data, "field14[].field1"), [1, 0])


class SafetyTest(unittest.TestCase):
    def test_danger_field_must_be_zero(self):
        t = TASK["配件探索"]
        names = field_names(schema, t.opcode)
        ok, _ = check_safety(t, names, dict(t.fields), ALLOWED_OPCODES)
        self.assertTrue(ok)
        bad = dict(t.fields)
        credit = next(no for no, n in names.items() if n == "credit")
        bad[credit] = ("int32", 20)
        ok, why = check_safety(t, names, bad, ALLOWED_OPCODES)
        self.assertFalse(ok)
        self.assertIn("credit", why)

    def test_safe_fields_only_for_that_opcode(self):
        t = TASK["国家宝箱"]
        names = field_names(schema, t.opcode)
        count = next(no for no, n in names.items() if n == "count")
        fields = dict(t.fields)
        fields[count] = ("int32", 6)
        self.assertTrue(check_safety(t, names, fields, ALLOWED_OPCODES)[0])

    def test_whitelist(self):
        t = TASK["每日签到"]
        ok, why = check_safety(t, {}, dict(t.fields), set())
        self.assertFalse(ok)
        self.assertIn("白名单", why)


class EchoWantTest(unittest.TestCase):
    def test_prelude_reply_rejected(self):
        names = {1: "OptType", 2: "ActiveType"}
        want = echo_want({1: ("int32", 1), 2: ("int32", 0)}, names)
        self.assertFalse(want({"OptType": 0, "ActiveType": 0}))   # 前置的回包
        self.assertTrue(want({"OptType": 1, "ActiveType": 0}))
        self.assertFalse(want({"OptType": 1, "ActiveType": 1}))   # 参谋那条
        self.assertTrue(want({"ret": 0}))                         # 没回显不强求

    def test_no_names_no_judge(self):
        self.assertIsNone(echo_want({1: ("int32", 1)}, {}))


class JudgeTest(unittest.TestCase):
    def test_ret_codes(self):
        self.assertEqual(judge("RseHeroVisit", {"ret": 0})[0], True)
        ok, why, stop = judge("RseHeroVisit", {"ret": 1})
        self.assertEqual((ok, stop), (False, True))
        self.assertIn("花钱", why)
        ok, _why, stop = judge("RseBuildingModify", {"error": 81})
        self.assertEqual((ok, stop), (False, True))

    def test_no_response_is_retryable(self):
        self.assertEqual(judge("RseX", None)[::2], (False, False))

    def test_result_family(self):
        self.assertTrue(judge("RseArenaOpt", {"result": 1})[0])
        self.assertFalse(judge("RseArenaOpt", {"result": 32})[0])
        self.assertFalse(judge("RseArenaOpt", {"type": 3})[0])    # 没有 result

    def test_success_flag(self):
        f = "ngetdailyawd"
        self.assertTrue(judge("RseDoubleElevenOfficer", {f: True}, success_flag=f)[0])
        self.assertFalse(judge("RseDoubleElevenOfficer", {f: False}, success_flag=f)[0])
        self.assertFalse(judge("RseDoubleElevenOfficer", {"optype": 2}, success_flag=f)[0])

    def test_left_zero_stops_unless_tiered(self):
        self.assertTrue(judge("RseWPCExplore", {"ret": 0, "leftFreeCnt": 0})[2])
        self.assertFalse(judge("RseWPCExplore", {"ret": 0, "leftFreeCnt": 0},
                               ignore_left=True)[2])

    def test_show_unsigned_minus_one(self):
        self.assertEqual(show(2 ** 64 - 1), "无")
        self.assertEqual(show(633), 633)


class TaskTableTest(unittest.TestCase):
    """任务表的硬约束。加新任务时这里先挡一道。"""

    def test_keys_unique_and_order(self):
        keys = [t.key for t in TASKS]
        self.assertEqual(len(keys), len(set(keys)))
        order = [t.key for t in ordered_tasks()]
        self.assertEqual(order[-2:], ["周任务", "每日任务"])

    def test_repeatable_tasks_have_a_limit_basis(self):
        # 铁律：没有次数依据就不重复。可重复的实测任务必须有闸门或冷却。
        for t in TASKS:
            if t.runner or t.confidence != "实测" or t.max_per_day <= 1:
                continue
            with self.subTest(task=t.key):
                self.assertTrue(t.gate or t.cooldown_sec or t.cooldown_until)

    def test_same_opcode_prelude_has_discriminator(self):
        # 前置与动作同 opcode 时必须造得出回包判据，否则运行时会拒发
        # （锦鲤心愿的 optype 就是漏登记 DISCRIMINATORS 才被拒发的）
        for t in TASKS:
            if t.runner or not any(op == t.opcode for op, _ in t.prelude):
                continue
            with self.subTest(task=t.key):
                self.assertIsNotNone(echo_want(t.fields, field_names(schema, t.opcode)))

    def test_every_request_encodes(self):
        for t in TASKS:
            for op, f in t.prelude:
                encode_message(f, omit_zero=False)
                self.assertIn(op, ALLOWED_OPCODES)
            plain = {k: v for k, v in t.fields.items() if not hasattr(v[1], "resolve")}
            encode_message(plain, omit_zero=False)

    def test_static_fields_pass_safety(self):
        for t in TASKS:
            if t.runner:
                continue
            plain = {k: v for k, v in t.fields.items() if not hasattr(v[1], "resolve")}
            with self.subTest(task=t.key):
                ok, why = check_safety(t, field_names(schema, t.opcode), plain,
                                       ALLOWED_OPCODES)
                self.assertTrue(ok, why)


if __name__ == "__main__":
    unittest.main()

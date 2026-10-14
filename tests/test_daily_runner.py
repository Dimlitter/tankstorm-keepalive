# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""每日任务执行器的端到端行为：对着一个脚本化的假服务端跑 daily.run()。"""
import unittest

from fakes import FakeRec, FakeSock, fake_server, temp_daily_state

from tankstorm import daily, schema


def config(*keys):
    return {"每日任务": {"启用": True, "任务": {k: True for k in keys},
                     "间隔秒": 3, "响应等待秒": 6}}


class World:
    """一个最小的游戏服务端：英雄开采、技能书、战略训练、国家宝箱、公会捐献。"""

    def __init__(self, rec):
        self.rec = rec
        self.hero = [2, 1, 0]
        self.book = {0: 1, 1: 0}
        self.train_said, self.train_real = 2, 1       # 面板谎报一次
        self.donate_reply = True

    def __call__(self, op, d):
        rse = "Rse" + schema.name_of(op)[3:]
        t = d.get("type")
        push = self.rec.push
        if op == "0400":
            push("RseHeroOpen", {"type": 0, "freeVisitCnt": list(self.hero)})
        elif op == "0402":
            k = d.get("type", 0)
            self.hero[k] -= 1
            push(rse, {"ret": 0, "type": k, "leftFreeCnt": self.hero[k]})
        elif op == "048a":
            a = d.get("ActiveType", 0)
            if d.get("OptType", 0) == 0:
                left = self.book[a]
                push(rse, {"OptType": 0, "ActiveType": a, "ret": 0, "field4": [
                    {"field1": 1, "field2": left, "field3": 0}]})
            else:
                self.book[a] = 0
                push(rse, {"OptType": 1, "ActiveType": a, "ret": 0})
        elif op == "04a5":
            if t == 1:
                push(rse, {"type": 1, "skilltraintimes": self.train_said})
                self.train_said -= 1
            elif self.train_real > 0:
                self.train_real -= 1
                push(rse, {"type": 4, "ret": 0})
            else:
                push(rse, {"type": 4, "ret": 11})
        elif op == "0463":
            push(rse, {"type": t, "ret": 0, "boxPage": {"field4": 6}} if t == 15
                 else {"type": t, "ret": 0})
        elif op == "0479":
            if t == 16 and not self.donate_reply:
                return
            push(rse, {"type": t, "ret": 0})


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.rec = FakeRec()
        self.rec.push("RseInit", {"username": "玩家甲"})
        self.world = World(self.rec)

    def run_daily(self, *keys):
        with fake_server(self.world) as (sent, _):
            results, details = daily.run(self.rec, FakeSock(), config(*keys))
        return results, sent

    def test_tiers_drain_each_tier_then_gate_stops(self):
        with temp_daily_state() as state:
            results, sent = self.run_daily("英雄开采")
            visits = [d.get("type", 0) for op, d in sent if op == "0402"]
            self.assertEqual(visits, [0, 0, 1])          # 低级两次、中级一次、高级没有
            # 做成过之后再被"已用完"拦下，结果保留最后一次成功的说明
            self.assertTrue(results["英雄开采"].startswith("成功"))
            self.assertEqual(state.load()["done"]["英雄开采"], 5)   # 确定没次数，记满

    def test_disabled_and_done_tasks_send_nothing(self):
        with temp_daily_state() as state:
            st = state.load()
            st["done"]["英雄开采"] = 5
            state.save(st)
            results, sent = self.run_daily("英雄开采")
            self.assertEqual(sent, [])
            self.assertIn("今日已执行", results["英雄开采"])
            self.assertEqual(results["每日签到"], "未开启")

    def test_skill_books_do_not_cross_talk(self):
        with temp_daily_state():
            results, sent = self.run_daily("技能书将领", "技能书参谋")
            claims = [d.get("ActiveType", 0) for op, d in sent
                      if op == "048a" and d.get("OptType") == 1]
            self.assertEqual(claims, [0])                # 参谋那档剩 0，不发
            self.assertTrue(results["技能书将领"].startswith("成功"))
            self.assertIn("闸门拦截", results["技能书参谋"])

    def test_server_refusal_stops_for_today(self):
        with temp_daily_state() as state:
            results, sent = self.run_daily("战略训练")
            trains = [op for op, d in sent if op == "04a5" and d.get("type") == 4]
            self.assertEqual(len(trains), 2)             # 第二次被 ret=11 拒
            self.assertIn("ret=11", results["战略训练"])
            self.assertEqual(state.load()["done"]["战略训练"], 7)

    def test_followup_runs_all_steps(self):
        with temp_daily_state():
            results, sent = self.run_daily("国家宝箱")
            types = [d.get("type") for op, d in sent if op == "0463"]
            self.assertEqual(types, [15, 10, 11])
            self.assertTrue(results["国家宝箱"].startswith("成功"))

    def test_missing_reply_counts_as_miss_not_done(self):
        self.world.donate_reply = False
        with temp_daily_state() as state:
            results, sent = self.run_daily("公会捐献")
            donate = [d for op, d in sent if op == "0479" and d.get("type") == 16]
            self.assertEqual(donate[0].get("tarUserName"), "玩家甲")   # 名字现取
            st = state.load()
            self.assertEqual(st["miss"]["公会捐献"], 1)
            self.assertNotIn("公会捐献", st["done"])


if __name__ == "__main__":
    unittest.main()

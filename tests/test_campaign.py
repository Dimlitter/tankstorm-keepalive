# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""征战世界：按服务端进度打到打不过，免费次数用完就停，付费重征一律不碰。"""
import unittest

from fakes import FakeRec, FakeSock, fake_server

from tankstorm import campaign

RSE = campaign.RSE


class Campaign:
    """假的征战服务端：lose_at 那关必输，其余必胜。

    回包的形状照 8/8 抓包：打一关先回 result=0（cdtime=开打时刻+10），
    再回 result=1/2（cdtime=开打时刻）。在不该发的时候收到请求就记进 violations。
    """

    def __init__(self, rec, stage=1, free=2, lost=0, lose_at=4):
        self.rec = rec
        self.stage, self.free, self.lost = stage, free, lost
        self.lose_at = lose_at
        self.clock = None
        self.fight_times = []
        self.violations = []
        self.restart_reply = None       # 给了就用它当重新开始的回包
        self.fight_result = None        # 给了就用它当战斗结果的 result

    def fd(self):
        return {"field1": self.stage, "field2": self.free, "field3": 0,
                "field4": self.lost, "field10": 0}

    def __call__(self, op, d):
        push = self.rec.push
        if op == campaign.ARMY_OPCODE:
            push("RseStrategicArmyInfo", {"type": 1})
            return
        now = int(self.clock.now)
        t = d.get("type")
        if t == campaign.QUERY:
            push(RSE, {"fightdata": self.fd(), "type": 5, "result": 3, "cdtime": 0})
        elif t == campaign.FIGHT:
            if self.lost:
                self.violations.append(("输了还接着打", self.stage))
            self.fight_times.append(self.clock.now)
            push(RSE, {"fightdata": self.fd(), "type": 6, "result": 0,
                       "cdtime": now + 10})
            if self.stage == self.lose_at:
                self.lost, result = 1, 2
            else:
                self.stage, result = self.stage + 1, 1
            push(RSE, {"fightdata": self.fd(), "type": 6,
                       "result": self.fight_result or result, "cdtime": now})
        elif t == campaign.RESTART:
            if self.free <= 0:
                self.violations.append(("免费次数为 0 还重新开始", self.stage))
            if self.restart_reply is not None:
                push(RSE, self.restart_reply)
                return
            self.stage, self.free, self.lost = 1, self.free - 1, 0
            push(RSE, {"fightdata": self.fd(), "type": 2, "result": 0,
                       "cdtime": now})


class CampaignTest(unittest.TestCase):
    CFG = {"每日任务": {"间隔秒": 3, "响应等待秒": 6}}

    def play(self, world, handler=None):
        with fake_server(handler or world) as (sent, clock):
            world.clock = clock
            ok, why = campaign.daily_fight(world.rec, FakeSock(), self.CFG)
        return ok, why, sent

    @staticmethod
    def kinds(sent):
        return [d.get("type") for op, d in sent if op == campaign.OPCODE]

    def test_fresh_day_fights_until_free_restarts_run_out(self):
        world = Campaign(FakeRec(), stage=1, free=2, lost=0, lose_at=4)
        ok, why, sent = self.play(world)
        self.assertTrue(ok, why)
        self.assertTrue(why.startswith("成功"))
        self.assertEqual(world.violations, [])
        # 每轮 1~3 关胜、第 4 关负；输了两次重新开始，第三轮输完就停
        self.assertEqual(self.kinds(sent), [5] + [6] * 4 + [2] + [6] * 4 + [2] + [6] * 4)
        self.assertIn("胜 9 关、负 3 关，重新开始 2 次", why)
        self.assertEqual((world.free, world.lost), (0, 1))

    def test_requests_match_the_capture(self):
        world = Campaign(FakeRec(), stage=1, free=1, lost=0, lose_at=2)
        _ok, _why, sent = self.play(world)
        self.assertEqual(sent[0], (campaign.ARMY_OPCODE, {"type": 1}))
        self.assertEqual(sent[1], (campaign.OPCODE, {"bAutoTreat": False, "type": 5}))
        fights = [d for op, d in sent if op == campaign.OPCODE and d.get("type") == 6]
        restarts = [d for op, d in sent if op == campaign.OPCODE and d.get("type") == 2]
        self.assertTrue(fights)
        self.assertTrue(all(d == {"bAutoTreat": True, "type": 6} for d in fights))
        # 重新开始只带 type 一个字段，抓包里就是这样
        self.assertEqual(restarts, [{"type": 2}])

    def test_yesterday_ended_on_a_loss_restarts_first(self):
        world = Campaign(FakeRec(), stage=60, free=2, lost=1, lose_at=3)
        ok, why, sent = self.play(world)
        self.assertTrue(ok, why)
        self.assertEqual(world.violations, [])
        self.assertEqual(self.kinds(sent), [5, 2] + [6] * 3 + [2] + [6] * 3)

    def test_nothing_left_today_only_queries(self):
        world = Campaign(FakeRec(), stage=60, free=0, lost=1)
        ok, why, sent = self.play(world)
        self.assertTrue(ok, why)
        self.assertEqual(self.kinds(sent), [5])
        self.assertIn("免费重新开始用完了", why)

    def test_no_free_restart_left_still_plays_one_run(self):
        world = Campaign(FakeRec(), stage=1, free=0, lost=0, lose_at=3)
        ok, why, sent = self.play(world)
        self.assertTrue(ok, why)
        self.assertEqual(world.violations, [])
        self.assertEqual(self.kinds(sent), [5, 6, 6, 6])

    def test_stage_count_is_not_hardcoded(self):
        # 能打到第几关看账号实力：强号打到第 150 关才输，也一路打下去
        world = Campaign(FakeRec(), stage=1, free=0, lost=0, lose_at=150)
        ok, why, sent = self.play(world)
        self.assertTrue(ok, why)
        self.assertEqual(self.kinds(sent), [5] + [6] * 150)
        self.assertIn("胜 149 关、负 1 关", why)
        self.assertIn("最远打到第 150 关", why)

    def test_fights_are_spaced_by_server_cooldown(self):
        world = Campaign(FakeRec(), stage=1, free=0, lost=0, lose_at=5)
        self.play(world)
        gaps = [b - a for a, b in zip(world.fight_times, world.fight_times[1:])]
        self.assertEqual(len(gaps), 4)
        for g in gaps:                       # 冷却 10 秒 + 客户端停顿 2 秒
            self.assertGreaterEqual(g, 12)
            self.assertLess(g, 13)

    def test_unknown_result_stops(self):
        world = Campaign(FakeRec(), stage=1, free=2, lost=0)
        world.fight_result = 7
        ok, why, sent = self.play(world)
        self.assertFalse(ok)
        self.assertFalse(why.startswith("成功"))
        self.assertIn("对不上", why)
        self.assertEqual(self.kinds(sent), [5, 6])

    def test_no_reply_sends_nothing_more(self):
        world = Campaign(FakeRec())
        ok, why, sent = self.play(world, handler=lambda op, d: None)
        self.assertFalse(ok)
        self.assertEqual(self.kinds(sent), [5])
        self.assertIn("查进度", why)

    def test_odd_restart_reply_stops(self):
        world = Campaign(FakeRec(), stage=60, free=2, lost=1)
        # 回到第 1 关了，免费次数却没少：可能是扣了别的，不认
        world.restart_reply = {"fightdata": {"field1": 1, "field2": 2, "field4": 0},
                               "type": 2, "result": 0, "cdtime": 0}
        ok, why, sent = self.play(world)
        self.assertFalse(ok)
        self.assertEqual(self.kinds(sent), [5, 2])
        self.assertIn("重新开始", why)

    def test_unreadable_cooldown_stops(self):
        world = Campaign(FakeRec(), stage=1, free=0, lost=0, lose_at=5)

        def no_started_reply(op, d):
            world(op, d)
            if op == campaign.OPCODE and d.get("type") == campaign.FIGHT:
                world.rec.recent[RSE] = [e for e in world.rec.recent[RSE]
                                         if e[1].get("result") != 0]
        ok, why, sent = self.play(world, handler=no_started_reply)
        self.assertFalse(ok)
        self.assertEqual(self.kinds(sent), [5, 6])
        self.assertIn("cdtime", why)


if __name__ == "__main__":
    unittest.main()

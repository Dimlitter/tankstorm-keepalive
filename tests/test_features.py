# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
"""争霸战选目标、账号状态读取、功勋商城的防线。"""
import unittest

from fakes import FakeRec, FakeSock, fake_server

from tankstorm import account, arena, country_war, shop

P1, P2 = "2000000000000001", "2000000000000002"     # 编的真人 uid


class PickTargetTest(unittest.TestCase):
    WINDOW = [(554, "13"), (589, "28"), (602, "6"), (623, "5"), (637, "1"),
              (658, "10"), (665, P1), (672, "13"), (679, "27"), (686, "11")]

    def test_npc_first(self):
        self.assertEqual(arena.pick_target(self.WINDOW, 693), (637, "1"))
        self.assertEqual(arena.pick_target(self.WINDOW, 693, prefer="最高名次优先"),
                         (554, "13"))

    def test_player_fallback_nearest(self):
        self.assertEqual(arena.pick_target([(631, P2), (665, P1)], 693), (665, P1))
        self.assertIsNone(arena.pick_target([(631, P2)], 693, allow_player=False))
        self.assertIsNone(arena.pick_target([(700, "1")], 693))
        self.assertIsNone(arena.pick_target([], 693))

    def test_avoid(self):
        self.assertEqual(arena.pick_target(self.WINDOW, 693, avoid={(637, "1")}),
                         (623, "5"))

    def test_npc_id(self):
        self.assertEqual(arena.npc_id("11"), 11)
        self.assertEqual(arena.npc_id("126"), 126)
        for v in ("127", "0", P1, "", None, "abc"):
            self.assertEqual(arena.npc_id(v), 0, v)

    def test_npc_table_monotonic_per_ladder(self):
        fp = [arena.NPC[i][1] for i in range(1, 67)]
        self.assertEqual(fp, sorted(fp))
        fp2 = [arena.NPC[i][1] for i in range(67, 127)]
        self.assertEqual(fp2, sorted(fp2))
        self.assertEqual(len(arena.NPC), 126)


class AccountTest(unittest.TestCase):
    def test_country_sources(self):
        rec = FakeRec()
        self.assertIsNone(account.read_my_country(rec))
        rec.push("RseLoad", {"countryData": {"field5": 7}})
        self.assertEqual(account.read_my_country(rec), 7)
        rec.push("RseFightSimpInfo", {"countryid": 3})
        self.assertEqual(account.read_my_country(rec), 3)    # 打架时取带名字的

    def test_bag_item(self):
        rec = FakeRec()
        self.assertEqual(account.bag_item(rec, 40004), (None, None))
        rec.push("RseBagItemLst", {"bagItem": {"field1": 1035, "field2": 40004,
                                               "field4": 63}})
        self.assertEqual(account.bag_item(rec, 40004), (1035, 63))
        self.assertEqual(account.bag_item(rec, 10118), (None, 0))

    def test_no_country_means_no_requests(self):
        rec = FakeRec()
        with fake_server(lambda op, d: None) as (sent, _):
            out = arena.run(rec, FakeSock(), {}, rounds=10)
            self.assertIn("国家ID", out["停止原因"])
            out = country_war.run(rec, FakeSock(), {}, rounds=10)
            self.assertIn("国家ID", out["停止原因"])
        self.assertEqual(sent, [])


class ShopTest(unittest.TestCase):
    CFG = {"功勋商城": {"自动补支援兵": True, "补货线": 20, "目标库存": 50,
                    "单次最多买几个": 10}}

    def setUp(self):
        self.rec = FakeRec()
        self.rec.push("RseBagItemLst", {"bagItem": [{"field1": 1020, "field2": 10118,
                                                     "field4": 5}]})
        self.rec.push("RseUserInfo", {"credit": 250, "feats": 356943})
        self.medal_after = 250

    def server(self, op, d):
        if op == shop.OP_STORE:
            self.rec.push(shop.RSE_STORE, {"type": 0})
        elif op == shop.OP_BUY:
            n = d.get("buynum")
            self.rec.push(shop.RSE_BUY, {"error": 0, "count": n, "shopID": 1199,
                                         "itemID": 10118, "credit": str(self.medal_after),
                                         "feats": str(356943 - 100 * n)})

    def test_off_by_default(self):
        with fake_server(self.server) as (sent, _):
            ok, _why = shop.daily_restock(self.rec, FakeSock(), {})
        self.assertTrue(ok)
        self.assertEqual(sent, [])

    def test_buys_with_feats(self):
        with fake_server(self.server) as (sent, _):
            ok, why = shop.daily_restock(self.rec, FakeSock(), self.CFG)
        self.assertTrue(ok, why)
        buy = [d for op, d in sent if op == shop.OP_BUY]
        self.assertEqual(len(buy), 1)
        self.assertEqual(buy[0]["credittype"], 2)
        self.assertEqual(buy[0]["buynum"], 10)
        self.assertEqual(buy[0]["credit"], "1000")

    def test_medal_change_is_an_alarm(self):
        self.medal_after = 150
        with fake_server(self.server):
            ok, why = shop.daily_restock(self.rec, FakeSock(), self.CFG)
        self.assertFalse(ok)
        self.assertIn("勋章", why)

    def test_unreadable_wallet_never_buys(self):
        self.rec.latest.pop("RseUserInfo")
        self.rec.recent.pop("RseUserInfo")
        with fake_server(self.server) as (sent, _):
            ok, _why = shop.daily_restock(self.rec, FakeSock(), self.CFG)
        self.assertFalse(ok)
        self.assertFalse([1 for op, _ in sent if op == shop.OP_BUY])

    def test_precheck(self):
        f = {2: ("string", "1000"), 3: ("string", "SHOP"), 5: ("int32", 1199),
             6: ("int32", 2), 7: ("int32", 10)}
        self.assertTrue(shop._precheck(f, 1000, 10, 1199)[0])
        self.assertFalse(shop._precheck({**f, 6: ("int32", 1)}, 1000, 10, 1199)[0])
        self.assertFalse(shop._precheck(f, 900, 10, 1199)[0])
        self.assertFalse(shop._precheck(f, 1000, 101, 1199)[0])


if __name__ == "__main__":
    unittest.main()

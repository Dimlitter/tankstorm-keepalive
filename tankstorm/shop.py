# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""功勋商城 —— 目前只做一件事：把国战用的摩多军团支援兵补到目标库存。

⚠️ **这是全项目唯一会主动花钱的模块。** 别的任务一律把危险字段钉死为 0，
只有这里的 `RcePurchase.credit` 必须是非零的价格。所以本模块自带一整套
自检，不依赖 daily.py 的通用安全检查（runner 本来就跳过那一套）。

机制来自 2026-08-30 抓包（8.30捕捉.pcapng，上下行 RC4 全流校验通过）。
玩家手动买了三次，两次各 1 个、一次 10 个，每一步都拿到了：

    RceSpecialStore{type:0}                               开商店面板
    RcePurchase{credit:"100"×n, type:"SHOP", shopID:1199,
                credittype:2, buynum:n}                   买 n 个
        → RsePurchase{error:0, count:n, shopID:1199, itemID:10118,
                      feats:"<剩余功勋>", credit:"<勋章余额>"}

三笔账全部对得上，这是判定"花的是功勋不是勋章"的依据：

| 观测点              | 买之前  | 买 1    | 再买 1  | 买 10   |
|---------------------|---------|---------|---------|---------|
| 背包槽位 1020 的数量 | 38      | 39      | 40      | 50      |
| feats（功勋）        | 356943  | 356843  | 356743  | 355743  |
| credit（勋章）       | 250     | 250     | 250     | 250     |
| coupon（券）         | 0       | 0       | 0       | 0       |

也就是：数量正好 +buynum，功勋正好 -100×buynum，**勋章和券分文未动**。
`credittype:2` 就是"用功勋付"，请求里那个 `credit` 字段是**总价**、不是勋章。
（回包里的 `credit` 才是勋章余额，同名不同义，别搞混。）

### 价格为什么只能写在配置里

商品表 `ShopItem_2026081001.dat` 在 CDN 上是加密的（不像 ArenaNpcSet 那样是
明文 TSV），服务端也不在任何回包里报价。所以单价只能按抓包实测的 100 写进
配置，并靠"总价必须等于单价×数量"这条自检兜住算错的情况。万一游戏改价，
服务端会直接拒（error≠0），我们停手，不会闷头多花。

### 前置那条 RceSpecialStore 是照抄客户端

抓包里玩家点开商店时发过它，所以我们也发。但要如实说明：它的回包
（`field3` 六项，`field1` 是 81/75/92/36/107/68）**并不包含 shopID 1199**，
看着更像"特惠商店"的内容。所以它到底是不是这笔购买的必需前置，没有证据；
发它只是因为真客户端发过，而且它不花钱、不改状态。
"""

import time

from . import sender
from .daily import _await_response, _nap
from .log import get_logger
from .proto_encode import encode_message

log = get_logger()

OP_STORE = "04f6"       # RceSpecialStore
OP_BUY = "041f"         # RcePurchase
RSE_STORE = "RseSpecialStore"
RSE_BUY = "RsePurchase"
RSE_BAG = "RseBagItemLst"
RSE_USER = "RseUserInfo"

# 抓包实测的常量。shopID/itemID 允许配置覆盖（游戏改版时好改），
# 但 credittype **写死**：2 = 用功勋付。这个值一旦错了就是花勋章，
# 所以它不接受任何外部输入。
CREDITTYPE_FEATS = 2
DEFAULT_SHOP_ID = 1199          # 摩多军团支援兵在功勋商城里的商品号
DEFAULT_ITEM_ID = 10118         # 它进背包之后的物品号
DEFAULT_UNIT_PRICE = 100        # 单价（功勋）

# 兜底硬上限：配置写飞了也不会一次买出天价。
HARD_MAX_BUY = 100


def _bag_count(rec, item_id):
    """从背包里读某物品的 (槽位, 数量)；读不到返回 (None, None)。

    ⚠️ 返回 None 和返回 0 是两回事：None = "没读到背包"，此时**绝不能买**
    （读不到就不做，铁律）；0 = "确实一个都没有"。

    RseBagItemLst 有两个列表：field2 是临时背包（槽位 2xxx），
    bagItem 是正式背包（槽位 1xxx）。买来的东西进正式背包，只看后者。
    """
    got = rec.latest.get(RSE_BAG) if rec else None
    if not got or not isinstance(got[1], dict):
        return None, None
    items = got[1].get("bagItem")
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return None, None
    for e in items:
        if isinstance(e, dict) and e.get("field2") == item_id:
            return e.get("field1"), int(e.get("field4") or 0)
    return None, 0        # 背包读到了，但里面没有这件东西


def _wallet(rec):
    """读 (勋章, 功勋)；读不到返回 (None, None)。

    勋章是买完之后用来对账的基准 —— 只要它动了，就说明 credittype 的理解
    是错的，必须立刻停手。
    """
    got = rec.latest.get(RSE_USER) if rec else None
    if not got or not isinstance(got[1], dict):
        return None, None
    d = got[1]
    c, f = d.get("credit"), d.get("feats")
    if not isinstance(c, int) or isinstance(c, bool):
        return None, None
    return c, (f if isinstance(f, int) and not isinstance(f, bool) else None)


def _precheck(fields, want_price, want_num, shop_id):
    """发之前把请求逐字段验一遍。返回 (是否放行, 原因)。

    daily.py 的通用安全检查对本模块是失效的（runner 跳过那一套，而且
    credit 本来就必须非零），所以这里自己来 —— 而且要留下可观测的日志。
    "防护装了但没接线"是踩坑记录第 13 条，教训就是防护必须能被看见。
    """
    # 数量上限先查：它是最根本的一道界，先报它比先报"总价对不上"清楚得多。
    if not (1 <= want_num <= HARD_MAX_BUY):
        return False, f"数量 {want_num} 超出硬上限 1..{HARD_MAX_BUY}，拒发"
    if fields.get(6, (None, None))[1] != CREDITTYPE_FEATS:
        return False, f"credittype 不是 {CREDITTYPE_FEATS}（功勋），拒发"
    if fields.get(5, (None, None))[1] != shop_id:
        return False, "shopID 与配置不符，拒发"
    if fields.get(7, (None, None))[1] != want_num:
        return False, "buynum 与算出来的数量不符，拒发"
    if fields.get(2, (None, None))[1] != str(want_price):
        return False, "credit（总价）与单价×数量对不上，拒发"
    return True, (f"credittype={CREDITTYPE_FEATS}(功勋) shopID={shop_id} "
                  f"buynum={want_num} 总价={want_price}功勋")


def daily_restock(rec, sock, config):
    """每日任务用：库存低于补货线就把支援兵补到目标库存。

    返回 (是否成功, 说明)，签名符合 daily.Task 的 runner 约定。
    """
    conf = (config.get("功勋商城", {}) or {})
    if not conf.get("自动补支援兵", False):
        return True, "成功：未开启自动补支援兵（默认关闭），什么都没做"

    item_id = int(conf.get("支援兵物品ID", DEFAULT_ITEM_ID))
    shop_id = int(conf.get("支援兵商品ID", DEFAULT_SHOP_ID))
    price = int(conf.get("支援兵单价功勋", DEFAULT_UNIT_PRICE))
    low = int(conf.get("补货线", 20))
    target = int(conf.get("目标库存", 50))
    cap = int(conf.get("单次最多买几个", 10))

    if price <= 0 or cap <= 0 or target <= 0:
        return False, "配置里的单价/上限/目标库存必须是正数，什么都没做"
    if target < low:
        return False, f"目标库存({target}) 比补货线({low}) 还低，配置有误，不买"

    # 铁律：先查询、读不到依据就不做。背包读不到 = 不知道现在有几个 = 不买。
    slot, have = _bag_count(rec, item_id)
    if have is None:
        return False, f"读不到背包里物品 {item_id} 的数量，不买（读不到就不做）"
    if have >= low:
        return True, (f"成功：支援兵还有 {have} 个（补货线 {low}），不用补")

    credit0, feats0 = _wallet(rec)
    if credit0 is None:
        return False, "读不到勋章余额，没法在买完之后对账，不买"

    want = min(target - have, cap, HARD_MAX_BUY)
    if want <= 0:
        return True, f"成功：算出来要买 {want} 个，不用补"
    total = price * want
    if feats0 is not None and feats0 < total:
        return False, (f"功勋不够：有 {feats0}，买 {want} 个要 {total}，不买")

    log.info("[功勋商城] 支援兵 %d 个 < 补货线 %d，准备买 %d 个补到 %d"
             "（单价 %d 功勋，共 %d；当前勋章 %s、功勋 %s）",
             have, low, want, have + want, price, total, credit0, feats0)

    # 前置：照抄真客户端在购买前发过的开商店面板（不花钱、不改状态）
    before = rec.seq_mark() if rec else 0
    sender.send_frame(sock, OP_STORE,
                      encode_message({1: ("int32", 0)}, omit_zero=False),
                      rec.rc4_c2s)
    _await_response(sock, rec, RSE_STORE, before, 6.0,
                    want=lambda d: d.get("type") == 0)
    _nap(0.6)

    # 字段顺序与真客户端逐字段一致；r1(1)/r2(4) 客户端不发，我们也不发。
    fields = {2: ("string", str(total)), 3: ("string", "SHOP"),
              5: ("int32", shop_id), 6: ("int32", CREDITTYPE_FEATS),
              7: ("int32", want)}
    ok, why = _precheck(fields, total, want, shop_id)
    if not ok:
        log.error("[功勋商城] 自检不通过：%s", why)
        return False, f"发送前自检不通过：{why}"
    log.info("[功勋商城] 自检通过：%s", why)

    before = rec.seq_mark() if rec else 0
    sender.send_frame(sock, OP_BUY, encode_message(fields, omit_zero=False),
                      rec.rc4_c2s)
    r = _await_response(
        sock, rec, RSE_BUY, before, 8.0,
        want=lambda d: (d.get("shopID") == shop_id and d.get("itemID") == item_id))
    if not isinstance(r, dict):
        return False, "购买请求没有回包，停手（钱有没有花出去未知，请自行核对）"

    err = r.get("error")
    if err not in (0, None):
        return False, f"服务器拒绝购买 error={err}（没买成，也就没花钱）"

    # 买完对账。**这一步是本模块最重要的防线**：只要勋章动了，就说明
    # credittype 的理解是错的，必须立刻停手并让用户看见。
    credit1 = r.get("credit")
    try:
        credit1 = int(credit1)
    except (TypeError, ValueError):
        credit1 = None
    if credit1 is not None and credit1 != credit0:
        log.error("[功勋商城] ⚠️ 勋章从 %s 变成了 %s —— 这笔买卖扣的不是功勋！"
                  "已停手，请立刻把 config 里的「自动补支援兵」关掉并核对账户",
                  credit0, credit1)
        return False, (f"危险：勋章由 {credit0} 变为 {credit1}，扣的不是功勋，"
                       f"已停手（请关掉自动补支援兵并核对账户）")

    got = r.get("count")
    feats1 = r.get("feats")
    spent = None
    if feats0 is not None:
        try:
            spent = feats0 - int(feats1)
        except (TypeError, ValueError):
            spent = None
    note = ""
    if spent is not None and spent != total:
        # 不当失败处理：钱已经花了，报出来比假装没事强。
        note = f"；⚠️ 实际扣了 {spent} 功勋，与预估 {total} 不符"
        log.warning("[功勋商城] 实扣功勋 %s 与预估 %s 不符", spent, total)

    log.info("[功勋商城] 买到 %s 个（请求 %d 个），勋章仍为 %s，功勋 %s→%s",
             got, want, credit1, feats0, feats1)
    return True, (f"成功：买了 {got if got is not None else want} 个支援兵，"
                  f"库存 {have}→{have + want}；花功勋 "
                  f"{spent if spent is not None else total}；勋章未动（{credit0}）"
                  f"{note}")

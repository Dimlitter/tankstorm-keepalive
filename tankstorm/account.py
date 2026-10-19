# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""从服务端推送里读账号状态：自己的国家 ID、背包里某件物品的数量。

这些都由服务端在登录时主动推来，录制器解码后存在 rec.latest 里，
读的时候不用额外发请求。读不到一律返回 None，由调用方停手 ——
猜一个值发出去，轻则请求无效，重则打错国家、买错东西。
"""

from . import wire
from .log import get_logger

log = get_logger()

BAG_LST = "RseBagItemLst"


def read_my_country(rec):
    """读"我是哪个国家"。返回 int；读不到返回 None。

    国战和争霸战的请求都要带这个值（争霸战的名次还是**本国内部**排名），
    以前写死在 config 里是 3 —— 那只是这个号是英国而已，换个号就错。

    两个来源：

      · `RseFightSimpInfo.countryid` —— schema 解出了真名，首选
      · `RseLoad.countryData.field5` —— 登录响应里的同一份 countryData，兜底

    可信度的旁证：`RseLoad.countryData` 和国战面板 `RseCountryOpt.countryData`
    是同一个结构，country_war 早就验过的 field6(当前城市 3201)、field13(行动力)、
    field17(累计战功) 在这里全部对得上，field5 就在它们中间。
    2026-08-29 和 08-30 两份抓包里两个来源都等于 3，与玩家所述"我是英国"一致。
    """
    def _pick(msg, path):
        v = wire.read_path(wire.latest(rec, msg), path)
        return v if wire.is_int(v) and v > 0 else None

    named = _pick("RseFightSimpInfo", "countryid")
    backup = _pick("RseLoad", "countryData.field5")
    if named and backup and named != backup:
        # 两个来源打架就以带名字的那个为准，但一定要报出来 —— 说明其中一个
        # 字段的含义理解错了，不该悄悄用下去。
        log.warning("国家ID 两个来源不一致：RseFightSimpInfo.countryid=%s，"
                    "RseLoad.countryData.field5=%s，取前者", named, backup)
    return named or backup


def bag_item(rec, item_id):
    """从背包里读某物品的 (槽位, 数量)。

    返回 (None, None) 表示**没读到背包**，此时绝不能据此花钱或用道具；
    返回 (None, 0) 表示背包读到了，但里面没有这件东西。

    抓包实测背包条目形如
        {"field1": 1035, "field2": 40004, "field3": 1696834514, "field4": 63}
    field1 是**槽位 ID**（正是 RceBagItemUse 请求里的 id），field2 是物品 ID，
    field4 是数量。槽位 ID 跟着这一格走，不能写死。

    RseBagItemLst 有两个列表：field2 是临时背包（槽位 2xxx），
    bagItem 是正式背包（槽位 1xxx）。买来的东西进正式背包，只看后者。
    """
    data = wire.latest(rec, BAG_LST)
    if data is None:
        return None, None
    items = data.get("bagItem")
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return None, None
    for e in items:
        if isinstance(e, dict) and e.get("field2") == item_id:
            return e.get("field1"), int(e.get("field4") or 0)
    return None, 0

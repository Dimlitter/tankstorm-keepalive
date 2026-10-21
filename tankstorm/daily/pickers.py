# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""从前置/动作的响应里挑出下一步要发的值。

全部按 2026-08-10 抓包的真实响应结构写，字段号见各处注释。
返回 None 一律表示"没有可做的"，调用方据此跳过，不猜。
"""

from ..wire import as_list


def free_wargame_site(data):
    """军事演习：从查询响应里挑一个没人占的演习场。

    响应结构（RseWarGameOpt type=1，分页推送，bLastMsg 标记最后一页）：
        field14.field2 = [ {field1: 场地号, field2..field5: 属性,
                            field6: 占领者uid, field7: 占领者名} , ... ]
    没人占的条目就是**没有 field6**。实测最后一页 241 个场地里 121 个是空的。
    """
    sites = []
    for blk in as_list(data.get("field14")):
        if isinstance(blk, dict):
            sites += [e for e in as_list(blk.get("field2")) if isinstance(e, dict)]
    for e in sites:
        if not e.get("field6") and isinstance(e.get("field1"), int):
            return e["field1"]
    return None


def sevendays_day(data):
    """七天乐：只领**当天**那份，已领过就返回 None。

    响应 RseSevenDays{type:0}：logonDays=登录到第几天，
    hasGetGift = 7 个 bool，hasGetGift[day-1] 表示第 day 天是否已领。
    实测 logonDays=3、hasGetGift=[F,T,F,...]，客户端领的正是 day=3。
    （字段 3 早先的 schema 认不出名字，叫 field3；2026101403 的类里存储槽名可读。）
    只领当天是为了贴合实测行为，不去猜历史几天还能不能补领。
    """
    day = data.get("logonDays")
    got = data.get("hasGetGift")
    if not isinstance(day, int) or not isinstance(got, list):
        return None
    if day < 1 or day > len(got):
        return None
    return None if got[day - 1] else day


def next_mine_to_occupy(data):
    """矿区争夺：探索响应里挑一个无人占领的矿，返回占矿请求的字段。

    响应 RseResourceOpt{type:2} 的 field5 是探到的矿列表：
        {field1: 矿ID}                      ← 无人占领
        {field1: 矿ID, field2: 占领者名, field6: 占领者uid, ...}  ← 有人
    实测探到 120007(空) / 120058 / 120053，客户端占的正是 120007。
    """
    for e in as_list(data.get("field5")):
        if isinstance(e, dict) and isinstance(e.get("field1"), int) \
                and not e.get("field6"):
            return {1: ("int32", 3), 2: ("int32", e["field1"])}
    return None


def eligible_gift_tier(data):
    """每日任务：返回一个"活跃度已达标且还没领"的档位，没有就 None。

    响应 RseDailyTask：
        dailyTask.field2 = 当前活跃度
        getGift = [{field1: 档位(10/30/50/80/100), field2: 0未领/1已领}, ...]
    实测活跃度 64，领了 10/30/50 三档，80/100 因为没达标领不了。
    """
    task = data.get("dailyTask")
    if isinstance(task, list):
        task = task[-1] if task else None
    act = task.get("field2") if isinstance(task, dict) else None
    if not isinstance(act, int):
        return None
    for e in as_list(data.get("getGift")):
        if not isinstance(e, dict):
            continue
        tier, taken = e.get("field1"), e.get("field2")
        if isinstance(tier, int) and tier <= act and not taken:
            return tier
    return None


def next_daily_gift(data):
    """每日任务的后续领取：还有达标未领的档位就继续领。"""
    tier = eligible_gift_tier(data)
    return None if tier is None else {5: ("int32", tier)}


def country_fields(type_, count=0):
    """RceCountryOpt 的整包字段（客户端 11 个字段全写，只有 type/count 有值）。"""
    return {1: ("int32", 0), 2: ("int32", count), 3: ("int32", 0),
            4: ("int32", type_), 6: ("int32", 0), 7: ("int32", 0),
            9: ("int32", 0), 13: ("int32", 0), 14: ("int32", 0),
            15: ("int32", 0), 16: ("int32", 0)}


def next_country_box(data):
    """国家宝箱是三步，光发 type:15 什么也领不到。

    8/10 抓包：
        {type:15}            查询 → 响应 boxPage.field4 = 可领数量（实测 6）
        {type:10, count:6}   领取 → countryData.field1 +6
        {type:11, count:1}   开箱 → 响应带 field15（实测掉 30028/20012 两样东西）
    按上一条响应的 type 决定下一步发什么。
    """
    t = data.get("type")
    if t == 15:
        box = data.get("boxPage")
        if isinstance(box, list):
            box = box[-1] if box else None
        n = box.get("field4") if isinstance(box, dict) else None
        if isinstance(n, int) and n > 0:
            return country_fields(10, n)
        return None
    if t == 10:
        return country_fields(11, 1)
    return None

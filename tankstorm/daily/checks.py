# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""发送前后的检查：危险字段、免费次数闸门、"这条回包是不是本次动作的"。"""

import re

from ..wire import is_int, read_path

# 字段名命中这些词 = 可能花钱/耗券，值必须为 0
#
# credit 就是勋章（RceWPCExplore.credit、RceMineModify.credit、
# RceCountryOpt.costCredit 都是），useItemID/useItemCnt 是"优先使用XX券"那个
# 勾选框对应的字段。游戏里免费次数用完后，同一个按钮会转而扣券或扣勋章，
# 所以这些字段必须钉死为 0。
DANGER_FIELD = re.compile(
    r"buy|cost|price|money|gold|coin|diamond|gem|pay|charge|"
    r"num|cnt|count|times|amount|soul|medal|credit|"
    r"item|card|ticket|discount", re.I)

# (opcode, 字段名) 白名单：抓包实证这个字段在**这一条消息里**不是花钱字段。
# 只按消息逐条放行，绝不整体放宽 DANGER_FIELD —— 同名字段在别的消息里照样危险。
#
# 0463 RceCountryOpt.count：国家宝箱领取/开箱的"第几档/开几个"，跟着面板回包的
#   boxPage.field4 走。2026-08-10 真客户端抓包实证 {type:10,count:6,costCredit:0}
#   与 {type:11,count:1,costCredit:0} 均 ret=0，全程免费；这条消息里真正花钱的是
#   costCredit，我们恒发 0，安全检查照旧盯着它。
#   注意：这个误伤是修好 schema 之后才暴露的 —— 以前字段名解不出来，
#   拿 "field2" 去匹配危险词自然不命中，等于一直在裸奔。
SAFE_FIELDS = {("0463", "count")}

# 请求里用来区分"这是哪一步"的字段名。前置和动作常常是同一个 opcode
# （04a5 既是开面板又是训练），响应也就同名，光按消息名等会把**前置的回包**
# 当成动作的结果 —— 2026-08-12 实盘就这样：战略训练第 6、7 次服务器已经回
# ret=11 拒绝了，我们却匹配到前置那条 type:1 的回包，判成功继续打。
# 除了"这是哪一步"（OptType/type），还要带上"这是哪一档" —— 技能书将领和参谋
# 是同一个 opcode、都发 OptType:1，**只靠 ActiveType 区分**（0=将领 1=参谋）。
# 2026-08-13 03:13:06 两条回包同一秒到达，不认 ActiveType 就会互相串。
# subType/sceneID/nExlType 同理，是特工派遣/配件探索/军备的档位选择字段。
# ⚠️ 这是按**字段名逐条登记**的白名单，不做模糊匹配 —— 和 SAFE_FIELDS 一样，
# 宁可漏也不整体放宽。代价是新消息容易漏登记：`optype` 就是这么漏的，
# 它和已在表里的 `optType` 差一个字母（opt+Type vs op+type），小写化也对不上，
# 结果锦鲤心愿造不出判据被拒发（2026-09-01 实盘）。
# 漏登记的症状很好认：日志里报"认不出动作回包"，且会把当时的字段名列出来。
DISCRIMINATORS = ("type", "noptType", "OptType", "optType", "optype", "nType",
                  "ntype", "nOptType", "ActiveType", "activetype", "subType",
                  "sceneID", "nExlType")


def field_names(schema, op):
    """opcode -> {字段号: 字段名}。读不到 schema 时返回 {}。"""
    e = (getattr(schema, "SCHEMA", {}) or {}).get(op) if schema else None
    if not e:
        return {}
    return {int(k): v[0] for k, v in e.get("fields", {}).items()}


def echo_want(fields, names):
    """按请求里的区分字段，造一个"这条响应是不是本次动作的回包"的判据。

    响应里没有这个字段就不强求（有些回包确实不回显），只在**回显了但对不上**
    时拒绝 —— 那必然是别的步骤的回包。请求里一个区分字段都没有时返回 None。
    """
    checks = [(names[fno], val)
              for fno, (_t, val) in fields.items()
              if names.get(fno) in DISCRIMINATORS]
    if not checks:
        return None

    def want(d):
        return all(d.get(n) == v for n, v in checks if n in d)
    return want


def read_counts(data, path):
    """取"每档剩余次数"。返回 int、list[int] 或 None。

    "field14[].field1" 这种路径会对列表里每个元素取 field1，取不到的记 0。
    """
    if "[]." in path:
        head, leaf = path.split("[].", 1)
        items = read_path(data, head + "[]")
        if not isinstance(items, list):
            return None
        out = []
        for e in items:
            v = e.get(leaf) if isinstance(e, dict) else None
            out.append(v if is_int(v) else 0)
        return out or None
    return read_path(data, path)


def check_gate(gate, data, tiers=None):
    """判断闸门。返回 (是否放行, 说明, 免费次数是否已归零, 该打第几档)。

    第三项用来区分"确定没次数了"和"读不到"：前者可以把今日次数打满，
    后者只是这一轮不做，不该消耗配额。
    第四项是本次要打的档位下标（不分档的任务返回 None）。
    """
    if data is None:
        return (False, f"{gate.timeout:.0f}s 内没收到 {gate.rse_msg}，"
                       "这一轮不做（宁可少做也不误扣券/勋章）", False, None)
    raw = read_counts(data, gate.field)
    if raw is None:
        return (False, f"{gate.rse_msg} 里没有 {gate.field}，这一轮不做",
                False, None)
    whole = raw

    if isinstance(raw, (list, tuple)):
        nums = [x if is_int(x) else 0 for x in raw]
        if tiers:
            # 分档任务：从低到高找第一个还有次数的档，逐档领干净
            n = min(len(nums), len(tiers.values))
            for i in range(n):
                if nums[i] > 0:
                    return (True, f"第 {i} 档（{tiers.field}={tiers.values[i]}）"
                                  f"还有 {nums[i]} 次（{gate.field}={whole}）",
                            False, i)
            return (False, f"各档免费次数都已用完（{gate.field}={whole}）",
                    True, None)
        # 不分档但服务端给的是数组：只看约定的那一档
        if gate.index >= len(nums):
            return (False, f"{gate.field}={whole} 没有第 {gate.index} 档，"
                           "这一轮不做", False, None)
        cnt = nums[gate.index]
        if cnt <= 0:
            return (False, f"第 {gate.index} 档免费次数已用完 "
                           f"{gate.field}={whole}（再发就会扣券/勋章）", True, None)
        return (True, f"第 {gate.index} 档还有 {cnt} 次（{gate.field}={whole}）",
                False, None)

    if gate.claimed_flag:
        # 布尔型闸门："领过了吗"。False=还没领→放行，True=领过了→今天别再来。
        # 争霸战上期排名奖励就是这种：RseArenaInfo.bLastRankGet，
        # 2026-08-10 真客户端抓包里它在领奖前后由 false 翻 true。
        if not isinstance(raw, bool):
            return (False, f"{gate.field}={raw!r} 不是布尔标记，这一轮不做",
                    False, None)
        if raw:
            return (False, f"已经领过了（{gate.field}=True）", True, None)
        return True, f"还没领（{gate.field}=False）", False, None

    if not is_int(raw):
        return (False, f"{gate.field}={raw!r} 不是次数，这一轮不做", False, None)
    if raw <= 0:
        return (False, f"免费次数已用完 {gate.field}={whole}（再发就会扣券/勋章）",
                True, None)
    return True, f"剩余 {raw} 次（{gate.field}={whole}）", False, None


def check_safety(task, names, fields, allowed):
    """发送前的安全检查。返回 (是否放行, 原因)。

    fields 是**已经把占位符解析成真值**的字段表，allowed 是 opcode 白名单。
    """
    if task.opcode not in allowed:
        return False, f"opcode {task.opcode} 不在白名单"
    for fno, (_ftype, val) in fields.items():
        fname = names.get(fno, f"field{fno}")
        if (task.opcode, fname) in SAFE_FIELDS:
            continue
        if DANGER_FIELD.search(fname) and val not in (0, False, "", None):
            return False, f"危险字段 {fname}={val!r} 非零，拒发（防止消耗资产）"
    return True, ""

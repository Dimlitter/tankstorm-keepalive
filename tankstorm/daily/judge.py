# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""判断一次动作的成败。

服务器响应里 ret 的含义
------------------------
2026-08-09 从 SWF 字节码确认（不再是推断）：RseHeroVisit 的处理函数
_-5TJ:_-18n::_-3gC 开头就是

    if (msg.ret != 0) {
        if (msg.ret == 1) { BUY.open(); return; }        // 弹充值/购买面板
        POPUPS.alert(Locales.Get('heroRecruitError' + msg.ret));  return;
    }
    ... 正常流程 ...

所以：

    ret == 0  成功
    ret == 1  要花钱才能做（客户端会弹商店），对脚本而言就是失败
    ret >= 2  具体错误，文案键是 heroRecruitError{ret}

文案本身在外部语言包里，SWF 常量池只有 'heroRecruitError' 这个前缀，
所以 ret=3 的中文说明还拿不到；但"非 0 即失败"这条已经是铁的。

保守起见，一旦判为失败就当天不再重试该任务 —— 宁可少做一次，
也不要在"次数已用完"的情况下反复撞墙。

曾经这里有个 RET_IS_PAYLOAD = {"RseWPCExplore"} 的例外，理由是"它的 ret 是
12672/76032 这种大数，装的是收益不是状态码"。那其实是 schema 字段名错位
导致的误读：RseWPCExplore 真正的 ret 是 7 号字段，而当时读的 6 号字段
是 leftTime（冷却秒数，12672 秒≈3.5 小时，数量级正好对得上）。
字段名修正后它和别的响应一样按 ret 判，例外已删除。
"""

from ..wire import is_int

# 响应里这些字段若存在，用来展示"获得了什么"
REWARD_HINT = ("ret", "freeVisitCnt", "leftFreeCnt", "getTimes", "leftTime",
               "nResult", "addsoul", "addoil", "addmetal", "jungong",
               "trainExp", "nAddRes")

# 响应里表示"还剩几次"的字段（名字以修正后的 schema 为准）。
#
# 注意这里**不能**放 leftTime 和 finishVisitTime：它们是冷却/完成时刻（秒），
# 不是次数。此前把 leftTime 当次数用，读到 12672 就以为"还剩一万多次"。
# 也不能放 hasCreditVisit —— 它是 bool（"是否还能花勋章再来一次"），
# 而 Python 里 isinstance(True, int) 为真，会被当成"剩 1 次"。
LEFT_FIELDS = ("freeVisitCnt", "leftFreeCnt", "getTimes", "nfreeTimes",
               "nRemainFreeNum")

# 这些响应不带 ret，成败看 result（1=成功），语义与 ret 相反
RESULT_IS_STATUS = {"RseArenaOpt", "RseWorldArenaOpt", "RseRegionArenaOpt",
                    "RseHeroArenaOpt"}

_U64_MAX = 2 ** 64 - 1


def left_count(data):
    """从响应里取剩余次数。返回 None 表示这条响应没提供该信息。

    freeVisitCnt 这类字段是 [各档剩余次数] 的数组（实测 [2,1,1]→[1,1,1]），
    取最大值：任一档还有免费次数就算还能做。
    """
    if not isinstance(data, dict):
        return None
    for f in LEFT_FIELDS:
        v = data.get(f)
        if is_int(v):
            return v
        if isinstance(v, (list, tuple)):
            nums = [x for x in v if is_int(x)]
            if nums:
                return max(nums)
    return None


def show(v):
    """给人看的值。服务端用无符号 64 位表示 -1（"没有/未上榜"），
    直接打出 18446744073709551615 只会让人以为是天文数字。"""
    if is_int(v) and v >= _U64_MAX - 8:
        return "无"
    return v


def judge(rse_msg: str, data, ignore_left=False, success_flag=None):
    """判断一次任务的结果。返回 (是否成功, 说明, 是否应停止今日重试)。

    "没等到响应"和"服务器明确拒绝"必须区别对待
    ------------------------------------------
    以前两者都会把当天次数直接打满。结果是：网络抖一下、或者响应比 6 秒慢一点，
    这个任务当天就废了 —— 三档的任务一次超时就把三次机会全赔进去。

    现在只有**服务器明确回了非 0 的 ret** 才算"今天别做了"。
    收不到响应只当这一轮没做成，由调用方按连续失败次数决定何时放弃
    （见 runner 里的 miss 计数），免费次数本来就有闸门兜着，不会白撞墙。
    """
    if data is None:
        return False, "未收到响应（这一轮不算数，稍后再试）", False
    if not isinstance(data, dict):
        return True, str(data)[:80], False
    # success_flag：动作回包里这个布尔字段翻成 true 才算真做成了。
    # 给那些**没有 ret 也没有 result** 的消息用 —— 找不到状态码就直接判成功，
    # 那是踩坑记录第十六条（"一个请求成功不等于这件事做成了"）。
    # 锦鲤心愿的 RseDoubleElevenOfficer 就是这样：整条回包里没有任何状态码，
    # 唯一能说明领到了的就是 ngetdailyawd 由 false 翻成 true。
    if success_flag:
        v = data.get(success_flag)
        if v is True:
            return True, f"成功：{success_flag}=True", False
        if v is False:
            return False, f"{success_flag} 仍为 False，没领到", True
        return False, f"响应里没有 {success_flag}，认不出成败", False
    # 争霸战这一族（RseArenaOpt 等）没有 ret，用的是 **result，而且 1 才是成功**
    # ——语义和 ret 正好相反。不特判的话找不到 ret 就直接判"成功"，
    # 又是一个假成功。2026-08-10 真客户端抓包实测：领取上期排名奖励回
    # {type:3, result:1}，随后 RseArenaInfo.bLastRankGet 由 false 翻 true。
    if rse_msg in RESULT_IS_STATUS:
        r = data.get("result")
        if not is_int(r):
            return False, "响应里没有 result，认不出成败", False
        if r == 1:
            return True, "成功：result=1", False
        return False, f"服务器返回 result={r}（1 才是成功）", True

    # 多数响应用 ret，少数用别的名字：RseBuildingModify（英雄培养）用 error，
    # 有的用 nret。取第一个存在的整数字段当状态码。
    ret = next((data[k] for k in ("ret", "error", "nret") if is_int(data.get(k))),
               None)
    if ret is not None and ret != 0:
        if ret == 1:
            why = "需要花钱才能做（客户端此时会弹商店），已跳过"
        elif rse_msg == "RseHeroVisit":
            # 这个文案键是从 RseHeroVisit 的处理函数里读出来的，只对它成立，
            # 别的消息各有各的错误表，套上去就是误导（曾经把英雄培养的
            # ret=81 写成 heroRecruitError81，其实完全不相干）。
            why = f"服务器返回 ret={ret}（错误码 heroRecruitError{ret}）"
        else:
            why = f"服务器返回 ret={ret}（该消息自己的错误码，含义未知）"
        return False, why, True
    bits = [f"{k}={data[k]}" for k in REWARD_HINT if k in data]
    msg = "成功" + ("：" + " ".join(bits) if bits else "")
    # 成功了，但如果响应说剩余次数已归零，就别再来了。
    # 分档任务除外：动作回包里的 leftFreeCnt 只是**当前这一档**的剩余，
    # 打完 10001 它就是 0，可 10002/10003 各还有一次，换档的事交给闸门判。
    if not ignore_left:
        left = left_count(data)
        if left is not None and left <= 0:
            return True, msg + "（剩余次数已用完，今日到此为止）", True
    return True, msg, False

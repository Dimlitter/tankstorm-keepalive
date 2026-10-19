# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""征战世界：一关一关往下打，打不过为止；输了用免费次数重新开始，接着打。

机制来自 2026-08-08 的抓包（全过程捕捉.pcapng，31 分钟，上下行 RC4 全流校验
通过），8/9、8/10 两份抓包补上了开局状态。全部动作走 RcePVEFightOpt(045b)，
靠 type 区分：

    type:5  查进度，bAutoTreat=false   → result=3，fightdata 是当前进度
    type:6  打下一关，bAutoTreat=true  → 先回 result=0（开打），
            （自动治疗，抓包里一直是 true）  再回 result=1（赢）或 2（输）
    type:2  重新开始，只带 type 一个字段 → result=0，回到第 1 关

fightdata（PVEFightData）里用到三个字段：

    field1  customIndex     下一仗打第几关
    field2  refreshTimes    今天还剩几次免费重新开始，每天 2 次
    field4  customFailTime  这一轮输过几次：打输变 1，重新开始归 0

8/8 那份里玩家打了两轮：第 1~59 关全胜，第 60 关输，点重新开始（免费次数 2→1）；
又从第 1 关打到第 60 关输，再点重新开始（1→0）。8/9、8/10 两天开局都停在
第 1 关、免费 2 次、没输过。

2026-10-15 实盘：开局第 1 关、免费 2 次。打了三轮，每轮都是连赢 59 关、第 60 关输，
用掉 2 次免费重新开始后停在第 60 关收工。共 177 胜 3 负，用时约 36 分钟。

每天开局停在哪，取决于前一天最后一步：最后点的是重新开始，就停在第 1 关，
直接往下打；最后一轮是打输收尾的，就停在输掉的那关，得先重新开始。
所以这里不按固定顺序发，每一步都先看服务端给的进度再决定。

能打到第几关看账号实力，代码里没有写死关数：服务端回"输了"这一轮才算完。
上面的 59、60 只是这个号的情况。

抓包里没有样本的两件事一律不做：
  · 免费次数为 0 时发 type:2。界面上还有"付费重征 1/1"，服务端多半会转扣勋章
  · 输了之后在同一关接着打
"""

import time

from . import wire
from .log import get_logger

log = get_logger()

OPCODE = "045b"          # RcePVEFightOpt
RSE = "RsePVEFightOpt"
ARMY_OPCODE = "045c"     # RceStrategicArmyInfo：客户端打开征战面板时先发这一条

QUERY, FIGHT, RESTART = 5, 6, 2          # 请求的 type
STARTED, WON, LOST = 0, 1, 2             # 打一关的两条回包里 result 的取值

# 开打回包的 cdtime 比结果回包的晚 10 秒，抓包里 120 仗无一例外，这是一仗的冷却。
# 客户端冷却结束后又停了约 2 秒才打下一关，每关间隔稳定在 12 秒上下，照着来。
CLIENT_PAUSE = 2.0
# 两个 cdtime 之差超过这个秒数就当读错了字段（差出几亿秒那种），停手。
# 不是在限冷却该多长：冷却一律以服务端给的为准。
MAX_COOLDOWN = 300


def _progress(fd):
    """fightdata → (关卡, 免费重新开始剩余次数, 本轮输过几次)。读不全返回 None。"""
    if not isinstance(fd, dict):
        return None
    vals = (fd.get("field1"), fd.get("field2"), fd.get("field4"))
    if not all(wire.is_int(v) for v in vals):
        return None
    return vals


def _query(sock, rec, timeout):
    """照客户端的顺序开面板、查进度：先 045c，紧跟着 type:5。返回进度或 None。"""
    wire.send(sock, rec, ARMY_OPCODE, {1: ("int32", 1)})
    since = wire.send(sock, rec, OPCODE, {1: ("bool", False),
                                          2: ("int32", QUERY)})
    data = wire.await_response(sock, rec, RSE, since, timeout,
                               want=lambda d: d.get("type") == QUERY)
    return _progress(data.get("fightdata")) if data else None


def _restart(sock, rec, timeout):
    """发一次重新开始。返回 (回包, 新进度)，没等到回包就是 (None, None)。

    调用方必须先确认免费次数 > 0，这里不再查。
    """
    since = wire.send(sock, rec, OPCODE, {2: ("int32", RESTART)})
    data = wire.await_response(sock, rec, RSE, since, timeout,
                               want=lambda d: d.get("type") == RESTART)
    return data, (_progress(data.get("fightdata")) if data else None)


def _fight(sock, rec, timeout):
    """打一关。返回 (发出时刻, 开打回包, 结果回包)，没等到的那条是 None。

    两条回包同名、type 也一样，只能靠 result 分：0 是开打，非 0 是结果。
    """
    since = wire.send(sock, rec, OPCODE, {1: ("bool", True),
                                          2: ("int32", FIGHT)})
    sent_at = time.time()
    done = wire.await_response(
        sock, rec, RSE, since, timeout,
        want=lambda d: d.get("type") == FIGHT and d.get("result") != STARTED)
    started = wire.pick_recent(
        rec, RSE, since,
        want=lambda d: d.get("type") == FIGHT and d.get("result") == STARTED)
    return sent_at, started, done


def _cooldown(started, done):
    """这一仗的冷却秒数：开打回包的 cdtime 减去结果回包的 cdtime。

    结果回包的 cdtime 就是开打那一刻的服务器时间（拿登录时 RseAuthState 的
    nCurTime 对过表），开打回包的是这一仗打完的时刻。只用两者之差，不拿 cdtime
    去比本机时钟，本机时钟偏了也不受影响。读不到或差值离谱就返回 None。
    """
    a = started.get("cdtime") if isinstance(started, dict) else None
    b = done.get("cdtime") if isinstance(done, dict) else None
    if not (wire.is_int(a) and wire.is_int(b)) or not 0 <= a - b <= MAX_COOLDOWN:
        return None
    return a - b


def run(rec, sock, config: dict) -> dict:
    """按服务端给的进度往下打，返回成果字典。

    "做完"为真表示今天已经没有能做的了：这一轮打输了，免费重新开始也用完了。
    其余情况停下来都是出了意外（没等到回包、回包和抓包对不上），留给下一轮重试，
    下一轮照样先查进度，从停下的地方接着打。
    """
    conf = config.get("每日任务", {}) or {}
    timeout = float(conf.get("响应等待秒", 6))
    gap = float(conf.get("间隔秒", 3))
    out = {"胜": 0, "负": 0, "重新开始": 0, "起始关": None, "最远关": None,
           "剩余免费": None, "做完": False, "停止原因": ""}

    prog = _query(sock, rec, timeout)
    if prog is None:
        out["停止原因"] = "查进度没等到回包，或 fightdata 读不全，停手"
        return out
    stage, free, lost = prog
    out["起始关"], out["剩余免费"] = stage, free
    log.info("[征战] 停在第 %d 关%s，今天还剩 %d 次免费重新开始",
             stage, "（这一轮已打输）" if lost else "", free)

    won_this_run = 0
    while True:
        if lost:
            if free <= 0:
                out["做完"] = True
                out["停止原因"] = f"第 {stage} 关已打输，今天的免费重新开始用完了"
                return out
            wire.nap(gap)
            data, prog = _restart(sock, rec, timeout)
            # 抓包里两次重新开始都是这样：result=0，回到第 1 关，
            # 免费次数正好少 1，失败次数归 0。差一条都不认。
            if not data or data.get("result") != 0 or prog != (1, free - 1, 0):
                got = data.get("result") if data else "无回包"
                out["停止原因"] = (f"重新开始的回包和抓包对不上"
                                f"（result={got}，进度 {prog}），停手")
                return out
            stage, free, lost = prog
            out["重新开始"] += 1
            out["剩余免费"] = free
            won_this_run = 0
            log.info("[征战] 重新开始，回到第 1 关；今天还剩 %d 次免费重新开始",
                     free)
            continue

        sent_at, started, done = _fight(sock, rec, timeout)
        if done is None:
            out["停止原因"] = f"第 {stage} 关没等到战斗结果，停手"
            return out
        result = done.get("result")
        prog = _progress(done.get("fightdata"))
        # 赢了关卡必须正好 +1。打通最后一关之类没见过的情形，服务端多半不会
        # 这样回，落到最后的"对不上"就停手，不会一直打下去。
        if result == WON and prog and prog[0] == stage + 1 and prog[2] == 0:
            out["胜"] += 1
            won_this_run += 1
            log.debug("[征战] 第 %d 关 胜", stage)
        elif result == LOST and prog and prog[0] == stage and prog[2] > 0:
            out["负"] += 1
            log.info("[征战] 第 %d 关打输了，这一轮到此为止（本次连赢 %d 关）",
                     stage, won_this_run)
        else:
            out["停止原因"] = (f"第 {stage} 关的战斗结果和抓包对不上"
                            f"（result={result}，进度 {prog}），停手")
            return out
        out["最远关"] = max(out["最远关"] or 0, stage)
        stage, free, lost = prog
        out["剩余免费"] = free

        cd = _cooldown(started, done)
        if cd is None:
            out["停止原因"] = (f"第 {out['最远关']} 关打完读不到冷却时间"
                            "（cdtime），停手")
            return out
        wire.nap(max(0.0, sent_at + cd + CLIENT_PAUSE - time.time()))


def daily_fight(rec, sock, config):
    """每日任务用：按服务端进度把今天能打的都打完。

    返回 (是否成功, 说明)，签名符合 daily.Task 的 runner 约定。
    这一轮已打输、免费次数也用完，今天就算做完了，哪怕本次一仗没打
    （玩家自己在游戏里打完了就是这种情形）。不这么判的话，每轮都会报一次 ❌
    并且明天之前一直重试。
    """
    out = run(rec, sock, config)
    ok = out["做完"]
    # 汇总按 v.startswith("成功") 判 ✅（jobs.push_daily_summary），
    # 打完了却不以"成功"开头会显示成失败。
    why = (f"{'成功：' if ok else ''}胜 {out['胜']} 关、负 {out['负']} 关，"
           f"重新开始 {out['重新开始']} 次")
    if out["最远关"]:
        why += f"；最远打到第 {out['最远关']} 关"
    return ok, f"{why}；{out['停止原因']}"

# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""任务表的积木：Task 本身，以及闸门、档位、后续步骤和字段占位符。"""

from .. import wire
from ..log import get_logger

log = get_logger()


class FromServer:
    """字段值占位符：发送时从服务器某条消息里现取。

    公会捐献要带自己的游戏名（抓包实测 tarUserName='<玩家名>'），这种值不能
    写死在仓库里 —— 换个号就错，而且属于个人信息。登录时服务器下发的
    RseInit 里就有（字段 username），运行时取即可。

    取不到就**不发这条任务**，不猜、不留空。
    """

    def __init__(self, rse_msg, field):
        self.rse_msg = rse_msg
        self.field = field

    def resolve(self, rec, sock=None, since=0):
        data = wire.latest(rec, self.rse_msg)
        if data is None:
            return None
        v = data.get(self.field)
        return v if isinstance(v, str) and v else None

    def __repr__(self):
        return f"<{self.rse_msg}.{self.field}>"


class FromResponse:
    """字段值占位符：从**前置请求的响应**里算出来。

    和 FromServer 的区别是时机：FromServer 取的是登录时就固定的信息（玩家名），
    FromResponse 要等前置请求发出去、服务器把列表推回来才有 —— 比如
    "哪个演习场没人占"、"七天乐今天是第几天"。所以它在前置之后才解析。

    pick(响应字典) 返回值，或返回 None 表示"没有可用的"，此时整条任务跳过。

    fresh=True  要等**这一轮前置请求之后**才到的响应。军事演习、七天乐属于这类：
                前置发出去，服务器才把场地列表/领取状态推回来，不等就必然读空。
    fresh=False 用最近一条即可，不要求是这轮新到的。每日任务属于这类：
                RseDailyTask 是服务器主动推的，没有对应的前置请求可发。
    """

    def __init__(self, rse_msg, pick, desc="", fresh=True, timeout=6.0):
        self.rse_msg = rse_msg
        self.pick = pick
        self.desc = desc or f"{rse_msg} 里挑一个"
        self.fresh = fresh
        self.timeout = timeout

    def resolve(self, rec, sock=None, since=0):
        if rec is None:
            return None
        if self.fresh and sock is not None:
            # 和闸门一样要**等**：前置刚发出去，响应还在路上。
            # 早先这里是直接读 rec.latest，前置的回包但凡慢一点就读空，
            # 任务被判成"取不到值"而跳过。
            data = wire.await_response(sock, rec, self.rse_msg, since,
                                       self.timeout)
        else:
            data = wire.latest(rec, self.rse_msg)
        if data is None:
            return None
        try:
            return self.pick(data)
        except Exception as exc:                       # 结构和预期不符就当没有
            log.debug("FromResponse(%s) 解析失败: %s", self.rse_msg, exc)
            return None

    def __repr__(self):
        return f"<{self.rse_msg}: {self.desc}>"


class Followup:
    """动作成功之后，按响应内容继续发的后续请求。

    有两类任务光发一包不够：
      · 矿区争夺 —— 先探索，服务器回一串矿，再从里面挑无人的占下来
      · 每日任务 —— 活跃度够几档就领几档，一档一包

    build(上一条响应) 返回下一包的 fields，返回 None 就结束。
    会反复调用（最多 max_rounds 轮），所以"领三档奖励"这种一次跑完。
    """

    def __init__(self, opcode, build, max_rounds=5, desc=""):
        self.opcode = opcode
        self.build = build
        self.max_rounds = max_rounds
        self.desc = desc


class Gate:
    """动作前的免费次数闸门：读**前置请求的响应**，还有免费次数才发动作。

    为什么这样是对的（2026-08-09 从 SWF 反汇编确认）
    ------------------------------------------------
    真实客户端点"开采"时走的是 _-5TJ:_-18n::_-1RB：

        var left:int = _-h1.freeVisitCnt[type];   // _-h1 就是 RseHeroOpen 响应本身
        if (left > 0) {
            req.type = type; req.free = 1; Transport.Send(req);   // 免费档
        } else {
            ... 走扣道具 / 扣勋章的分支 ...
        }

    也就是说**免费次数就在开面板的响应里**，动作之前就能读到。
    此前一版把闸门去掉了，理由是"剩余次数只有动作响应里才有，首次运行必然
    等不到"——那是因为当时读的是动作响应 RseHeroVisit；而客户端读的是
    开面板响应 RseHeroOpen。前置请求本来就要发，它的响应正好就是闸门数据。

    读不到状态就**不发**：宁可这一轮不做，也不能在免费次数已尽时把请求发出去，
    那正是客户端会转而扣券/扣勋章的位置。

    **必须按档位取，不能取数组最大值**（2026-08-10 抓包纠正）
    ------------------------------------------------------
    freeVisitCnt 是 [低级, 中级, 高级] 各自的剩余免费次数，实测低级 3 次、
    中级 1 次、高级 0 次（高级从来就没有免费次数）。三个档位互相独立。
    用完低级之后数组是 `[0, 1, 0]` —— 取最大值会得到 1，闸门放行，
    然后照样发 type=0，服务端照样拒。所以 index 必须对上任务实际发的档位。

    rse_msg  等哪条响应（如 RseHeroOpen），它是 prelude 的回包
    field    读哪个字段（如 freeVisitCnt），支持 wire.read_path 的路径写法
    index    该字段是分档数组时取第几档，要和任务发的 type/subType/sceneID 对上；
             响应给的是标量时忽略此项
    claimed_flag  该字段是"领过了吗"的布尔标记而不是次数：False 放行、True 拦下
    """

    def __init__(self, rse_msg, field, index=0, timeout=6.0,
                 claimed_flag=False):
        self.rse_msg = rse_msg
        self.field = field
        self.index = index
        self.timeout = timeout
        self.claimed_flag = claimed_flag


class Tiers:
    """一个任务的多个档位，每档有各自独立的免费次数。

    闸门读到的数组第 i 项就是第 i 档还剩几次，把 field 换成 values[i]
    就是打那一档。实测：
        英雄开采/将领冶炼  type      = 0 / 1 / 2      freeVisitCnt=[3,1,1]
        特工派遣           subType   = 1001/1002/1003 freeVisitCnt=[3,1,1,…]
        配件探索           sceneID   = 10001/2/3      leftFreeCnt=[3,1,1]
    三档要一档一档领干净 —— 早先只打第 0 档，等于白扔掉中级和高级各一次。
    """

    def __init__(self, field, values):
        self.field = field
        self.values = values


class Task:
    """一个每日任务 = 前置请求 + 一条动作消息。

    **prelude 是必须的**（2026-08-09 定位）：真实客户端每个动作前都会先发一个
    "开面板 / 查询"请求，服务端据此建立会话上下文。此前脚本直接发动作、跳过这步，
    结果就是"参数一模一样却不生效" —— 抓包顺序铁证：

        RceHeroOpen{type:0}      → RceHeroVisit{free:1,type:0}
        RceAdmiralOpen{type:0}   → RceAdmiralVisit{free:1,type:0}
        RceCountryOpen{}         → RceCountryOpt{type:15}
        RceWPCBaseOpen{type:1}   → RceWPCExplore{sceneID:10001}
        RceWarCollegeOpt{type:1} → RceWarCollegeOpt{type:4,...}
        RceWarGameOpt{type:1}    → RceWarGameOpt{type:2,siteID:N}
        RceTrenchMortarOpt{optType:0} → {optType:1,subType:1001}
        RceJunBeiOpt{type:21}    → RceJunBeiOpt{type:22,nExlType:1}
        RceAdviserOpt{noptType:1}→ RceAdviserOpt{noptType:11}
        RceResourceOpt{type:1}   → RceResourceOpt{type:2,...}

    字段
    ----
    key / name     配置里的开关名 / 给人看的全名
    opcode / msg   动作请求的 opcode 和消息名（响应名由 RceXxx → RseXxx 推出）
    fields         {字段号: (类型, 值)}，值可以是 FromServer / FromResponse 占位符
    confidence     "实测" | "待确认"；待确认的默认不执行
    prelude        [(opcode, fields), ...]，按序发送，不判成败
    gate           Gate：读前置的响应，免费次数 > 0 才发动作
    tiers          Tiers：多档位任务，逐档把免费次数领干净
    followup       Followup：动作成功后按响应继续发（占矿、领多档奖励）
    max_per_day    每天最多成功几次
    cooldown_sec   写死的冷却秒数，只给服务器不报下次可用时刻的任务用（英雄培养 8 小时）
    cooldown_until 从响应里读"下次可用时刻"（unix 秒）的字段路径。比写死秒数靠谱：
                   占矿时长随矿等级变，演习场占领时长也是服务端定的
    report         前置/动作响应里值得报给用户看的字段（排名、积分、剩余次数…）
    success_flag   动作回包里哪个布尔字段为 true 才算成功。只给那些既没有 ret
                   也没有 result 的消息用，别的一律走 judge() 的状态码
    runner         自定义执行器 runner(rec, sock, config) -> (是否成功, 说明)。
                   给形状不合流水线的任务用（国战要先召唤支援兵、再从推送里
                   读目标 ID）。有 runner 的任务跳过前置/闸门/安全检查那一整套，
                   执行器内部必须自己守住"先查询、读到依据才做"这条铁律
    """

    def __init__(self, key, name, opcode, msg, fields, confidence,
                 note="", max_per_day=1, gate=None, cooldown_sec=0,
                 prelude=(), followup=None, tiers=None, cooldown_until=None,
                 report=(), runner=None, success_flag=None):
        self.key = key
        self.name = name
        self.opcode = opcode
        self.msg = msg
        self.fields = fields
        self.confidence = confidence
        self.note = note
        self.max_per_day = max_per_day
        self.gate = gate
        self.cooldown_sec = cooldown_sec
        self.prelude = list(prelude)
        self.followup = followup
        self.tiers = tiers
        self.cooldown_until = cooldown_until
        self.report = tuple(report)
        self.runner = runner
        self.success_flag = success_flag

    @property
    def rse(self) -> str:
        """动作请求对应的响应消息名（RceXxx → RseXxx）。"""
        return rse_name(self.msg)


def rse_name(rce_msg: str) -> str:
    """请求消息名 → 对应的响应消息名（RceXxx → RseXxx）。"""
    return "Rse" + rce_msg[3:] if rce_msg.startswith("Rce") else rce_msg

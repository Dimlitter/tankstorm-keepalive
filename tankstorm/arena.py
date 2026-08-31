"""争霸战自动挑战。

机制来自 2026-08-29 抓包（8.29捕捉国战和争霸战.pcapng，上下行 RC4 全流校验
通过，776 条下行 / 328 条上行全部解开）。抓包里玩家手动打了三场，
每一步的请求与响应都拿到了：

    RceArenaInfo{type:1}                        开面板 → RseArenaInfo{type:1}
        nRankSelf 本国名次、nCanFightTimes 剩余挑战次数、nIntegralScore 积分
    RceArenaRankInfo{type:2, nIndex, nCountry}  取可挑战名单
        → RseArenaRankInfo{type:2}  名次→uid（field2.field3）
        → RseArenaRankInfo{type:3}  uid→名字（field3），**只给真人**
    RceArenaOpt{type:1, uidself, uidfight, indexself, indexfight, countryidself}
        → RseArenaOpt{type:1, …同样六个字段回显, result}

打赢之后自己的名次会**变成对手的名次**：693 →(打 686)→ 686 →(打 583)→ 583
→(打 466)→ 466；nCanFightTimes 10→9→8→7；积分每场 +5。

怎么认出 NPC
------------
**协议里根本没有 NPC 的名字**，这是查出来的、不是猜的：

  · 服务端的名字表（RseArenaRankInfo type:3）在整份抓包里共 593 条，
    **全部是 15~16 位的真人 uid**，一条 NPC 都没有
  · 排行榜里另有 24 个 1~2 位的短 uid（1/3/4/5/6/8/9/10/11/13/17/19/20/21/
    23/24/27/28/31/34/35/36/39/46），**没有一个出现在名字表里**
  · 这 24 个数全部落在官方配置表 ArenaNpcSet 的 id 范围 1..126 之内
  · 真客户端打的两个目标 uidfight="11"/"9" 也是短 uid

所以判据是：**uid 能转成 1..126 的整数 = NPC，十几位的长 uid = 真人**。
名字则由客户端自己查 ArenaNpcSet 表得到 —— 玩家说的"铁血部落 / 地狱犬 /
神圣十字军 / 黑寡妇 / 魔多军团 / 游牧民族"正是这张表里仅有的六个派系，
126 个 NPC 每人属于其中之一，所以"优先打这六家"等价于"优先打 NPC"。

⚠️ **别和国战搞混**。本模块里的"魔多军团上等兵"之类只是争霸战排行榜上的
擂台 NPC，靠 RceArenaOpt 挑战、消耗 nCanFightTimes，跟世界地图毫无关系。
国战打的魔多军团是世界地图上的一个国家（country_war.py，国家 ID 21，
配置里沿用"摩多"这个写法），走 RceCountryOpt、消耗行动力、要人站在驻地
旁边的城市。两套东西的 opcode、字段、限流依据没有一处相同。

NPC 表来源
----------
游戏启动时从 config_*.xml 的 pvpNpcSet 项加载：
    https://redwar-cdn.sincetimes.com/100616028/res/20120522/config/
        ArenaNpcSet_2014102701.dat
是一份 GBK 的 TSV，列为 id / npcName / fightPointB / fightPointE / … 。
游戏改版后重新下这个文件替换下表即可（新文件名在 config_*.xml 里）。

⚠️ id **不是**统一的强弱序：1..66 是一条梯队（战力下限 0→356000），
67..126 是另一条（0→108000），两条各自递增但互不衔接。所以排强弱要用
战力下限（fightPointB）而不是 id。抓包里出现过的 24 个 NPC 全在 1..66。
"""

import time

from . import daily as _daily
from . import sender
from .daily import _await_response, _nap
from .log import get_logger
from .proto_encode import encode_message

log = get_logger()

OP_INFO = "0468"        # RceArenaInfo      开面板
OP_OPT = "0469"         # RceArenaOpt       挑战 / 领奖
OP_RANK = "046a"        # RceArenaRankInfo  取排行榜
RSE_INFO = "RseArenaInfo"
RSE_OPT = "RseArenaOpt"
RSE_RANK = "RseArenaRankInfo"

# id -> (名字, 战力下限)。取自官方配置 ArenaNpcSet_2014102701.dat，逐行照抄。
NPC = {
    1: ("神圣十字军列兵", 0), 2: ("游牧民族列兵", 100000), 3: ("黑寡妇列兵", 104000),
    4: ("魔多军团列兵", 108000), 5: ("铁血部落列兵", 112000), 6: ("地狱犬列兵", 116000),
    7: ("神圣十字军上等兵", 120000), 8: ("游牧民族上等兵", 124000),
    9: ("黑寡妇上等兵", 128000), 10: ("魔多军团上等兵", 132000),
    11: ("铁血部落上等兵", 136000), 12: ("地狱犬上等兵", 140000),
    13: ("神圣十字军少尉", 144000), 14: ("游牧民族少尉", 148000),
    15: ("黑寡妇少尉", 152000), 16: ("魔多军团少尉", 156000),
    17: ("铁血部落少尉", 160000), 18: ("地狱犬少尉", 164000),
    19: ("神圣十字军中尉", 168000), 20: ("游牧民族中尉", 172000),
    21: ("黑寡妇中尉", 176000), 22: ("魔多军团中尉", 180000),
    23: ("铁血部落中尉", 184000), 24: ("地狱犬中尉", 188000),
    25: ("神圣十字军上尉", 192000), 26: ("游牧民族上尉", 196000),
    27: ("黑寡妇上尉", 200000), 28: ("魔多军团上尉", 204000),
    29: ("铁血部落上尉", 208000), 30: ("地狱犬上尉", 212000),
    31: ("神圣十字军少校", 216000), 32: ("游牧民族少校", 220000),
    33: ("黑寡妇少校", 224000), 34: ("魔多军团少校", 228000),
    35: ("铁血部落少校", 232000), 36: ("地狱犬少校", 236000),
    37: ("神圣十字军中校", 240000), 38: ("游牧民族中校", 244000),
    39: ("黑寡妇中校", 248000), 40: ("魔多军团中校", 252000),
    41: ("铁血部落中校", 256000), 42: ("地狱犬中校", 260000),
    43: ("神圣十字军上校", 264000), 44: ("游牧民族上校", 268000),
    45: ("黑寡妇上校", 272000), 46: ("魔多军团上校", 276000),
    47: ("铁血部落上校", 280000), 48: ("地狱犬上校", 284000),
    49: ("神圣十字军少将", 288000), 50: ("游牧民族少将", 292000),
    51: ("黑寡妇少将", 296000), 52: ("魔多军团少将", 300000),
    53: ("铁血部落少将", 304000), 54: ("地狱犬少将", 308000),
    55: ("神圣十字军中将", 312000), 56: ("游牧民族中将", 316000),
    57: ("黑寡妇中将", 320000), 58: ("魔多军团中将", 324000),
    59: ("铁血部落中将", 328000), 60: ("地狱犬中将", 332000),
    61: ("神圣十字军上将", 336000), 62: ("游牧民族上将", 340000),
    63: ("黑寡妇上将", 344000), 64: ("魔多军团上将", 348000),
    65: ("铁血部落上将", 352000), 66: ("地狱犬上将", 356000),
    67: ("神圣十字军列兵", 0), 68: ("游牧民族列兵", 12000), 69: ("黑寡妇列兵", 12100),
    70: ("魔多军团列兵", 12200), 71: ("铁血部落列兵", 12300), 72: ("地狱犬列兵", 12400),
    73: ("神圣十字军上等兵", 12500), 74: ("游牧民族上等兵", 12600),
    75: ("黑寡妇上等兵", 12700), 76: ("魔多军团上等兵", 12800),
    77: ("铁血部落上等兵", 12900), 78: ("地狱犬上等兵", 13000),
    79: ("神圣十字军少尉", 13100), 80: ("游牧民族少尉", 13200),
    81: ("黑寡妇少尉", 13300), 82: ("魔多军团少尉", 13400),
    83: ("铁血部落少尉", 16000), 84: ("地狱犬少尉", 16200),
    85: ("神圣十字军少尉", 16400), 86: ("游牧民族少尉", 16600),
    87: ("黑寡妇中尉", 16800), 88: ("魔多军团中尉", 25000),
    89: ("铁血部落中尉", 25500), 90: ("地狱犬中尉", 26000),
    91: ("神圣十字军上尉", 26500), 92: ("游牧民族上尉", 27000),
    93: ("黑寡妇上尉", 27500), 94: ("魔多军团上尉", 28000),
    95: ("铁血部落上尉", 28500), 96: ("地狱犬上尉", 29000),
    97: ("神圣十字军少校", 29500), 98: ("游牧民族少校", 40000),
    99: ("黑寡妇少校", 41000), 100: ("魔多军团少校", 42000),
    101: ("铁血部落少校", 43000), 102: ("地狱犬少校", 44000),
    103: ("神圣十字军中校", 45000), 104: ("游牧民族中校", 46000),
    105: ("黑寡妇中校", 47000), 106: ("魔多军团中校", 48000),
    107: ("铁血部落上校", 49000), 108: ("地狱犬上校", 70000),
    109: ("神圣十字军上校", 71000), 110: ("游牧民族上校", 72000),
    111: ("黑寡妇上校", 73000), 112: ("魔多军团上校", 74000),
    113: ("铁血部落上校", 75000), 114: ("地狱犬上校", 76000),
    115: ("神圣十字军上校", 77000), 116: ("游牧民族上校", 78000),
    117: ("黑寡妇少将", 79000), 118: ("魔多军团少将", 100000),
    119: ("铁血部落少将", 101000), 120: ("地狱犬少将", 102000),
    121: ("神圣十字军中将", 103000), 122: ("游牧民族中将", 104000),
    123: ("黑寡妇中将", 105000), 124: ("魔多军团中将", 106000),
    125: ("铁血部落中将", 107000), 126: ("地狱犬中将", 108000),
}


def npc_id(uid) -> int:
    """uid 是 NPC 就返回它的 NPC 编号，是真人（十几位长 uid）则返回 0。"""
    s = str(uid or "")
    if not s.isdigit():
        return 0
    n = int(s)
    return n if n in NPC else 0


def who(uid, names=None) -> str:
    """把 uid 说成人话，只用于日志。names 是服务端给的真人名字表。"""
    n = npc_id(uid)
    if n:
        return f"{NPC[n][0]}(NPC#{n})"
    nick = (names or {}).get(str(uid))
    return f"{nick}（真人）" if nick else f"真人 {uid}"


# ---------------------------------------------------------------- 请求

def _send(sock, rec, opcode, fields):
    """发一条请求，返回发送前的消息序号（之后只认序号更大的回包）。"""
    before = rec.seq_mark() if rec else 0
    sender.send_frame(sock, opcode, encode_message(fields, omit_zero=False),
                      rec.rc4_c2s)
    return before


def panel(sock, rec, timeout=6.0):
    """开争霸战面板，返回 RseArenaInfo 的数据；读不到返回 None。

    严格判据是 type==1（就是我们请求的那个）；挑不到时退回"只要带
    nCanFightTimes 就行" —— 面板类回包的 type 未必是对请求的回显，
    硬要求会把唯一带次数的那条否掉（踩坑记录第 14 条）。
    """
    since = _send(sock, rec, OP_INFO, {1: ("int32", 1)})
    return _await_response(
        sock, rec, RSE_INFO, since, timeout,
        want=lambda d: d.get("type") == 1 and d.get("nCanFightTimes") is not None,
        relaxed=lambda d: d.get("nCanFightTimes") is not None)


def _rank_of(v):
    """把面板里的名次读成 int；**未上榜返回 None**。

    赛季刚开时服务端把 nRankSelf 填成 -1，protobuf 按无符号读出来就是
    18446744073709551615 这个天文数字。直接拿它当名次去要名单，服务端只会
    回一个空壳（2026-08-31 实测，各种 nIndex 试了 11 个值全是空）。
    """
    if not isinstance(v, int) or isinstance(v, bool):
        return None
    return v if 0 <= v < 1_000_000 else None


def _read_panel(data):
    """从面板响应里取 (本国名次, 剩余挑战次数, 积分)。名次未上榜时为 None。"""
    if not isinstance(data, dict):
        return None, None, None
    return (_rank_of(data.get("nRankSelf")), data.get("nCanFightTimes"),
            data.get("nIntegralScore"))


def rank_window(sock, rec, my_rank, country, timeout=6.0):
    """取可挑战名单。返回 (名单, 真人名字表)，名单是 [(名次, uid), …]。

    一条请求会引来**两条**同名回包：type:2 是"名次→uid"，type:3 是
    "uid→名字"（只含真人）。两条都要，靠 type 区分，别混。

    ⚠️ 只取 field2.field3 —— 那才是可挑战的窗口（十个人都排在自己前面）。
    同一条回包里的 field4 是**全国前十**，拿它当目标就等于去打榜首。
    """
    since = _send(sock, rec, OP_RANK,
                  {1: ("int32", 2), 2: ("int32", my_rank),
                   3: ("int32", country)})
    board = _await_response(sock, rec, RSE_RANK, since, timeout,
                            want=lambda d: d.get("type") == 2)
    if not isinstance(board, dict):
        return [], {}

    # 名字表紧跟着来，已经到了就立刻挑得到，没到最多再等 2 秒。
    # 拿不到不算错 —— 名字只用来写日志，判 NPC 靠的是 uid 本身。
    names = {}
    tbl = _await_response(sock, rec, RSE_RANK, since, 2.0,
                          want=lambda d: d.get("type") == 3)
    if isinstance(tbl, dict):
        rows = tbl.get("field3") or []
        for e in (rows if isinstance(rows, list) else [rows]):
            if isinstance(e, dict) and isinstance(e.get("field1"), str):
                names[e["field1"]] = e.get("field2")

    return _parse_board(board, country), names


def _parse_board(board, country):
    """从榜单回包里抽出本国的 [(名次, uid), …]。

    ⚠️ 只取 field3。同一条回包里的 field4 是**全国前十**，拿它当目标就等于
    去打榜首。type:1（全服榜）是六个国家一段一段的，所以必须按国家挑对那段。
    """
    blocks = board.get("field2") or []
    if isinstance(blocks, dict):
        blocks = [blocks]
    out = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("field1") not in (None, country):
            continue
        rows = b.get("field3") or []
        for e in (rows if isinstance(rows, list) else [rows]):
            if (isinstance(e, dict) and isinstance(e.get("field2"), str)
                    and isinstance(e.get("field1"), int)
                    and not isinstance(e.get("field1"), bool)):
                out.append((e["field1"], e["field2"]))
    return out


def join_board(sock, rec, timeout=8.0):
    """让服务端把自己排进本期榜单，返回拿到的名次；失败返回 None。

    **赛季刚开时必须先做这一步。** 每周一上午十点开新一期，此前的名次清零，
    服务端把 nRankSelf 填成 -1（未上榜）；这时 type:2 的"围绕我的名次取
    十个人"无从算起，只会回一个空壳，硬从全服榜里挑目标发起挑战会被
    `result:32` 拒掉（不消耗挑战次数）。2026-08-31 实测：

        面板 nRankSelf=-1        type:2 名单 0 条
        发 RceArenaOpt{type:5} → 回包 indexself=658
        面板 nRankSelf=658       type:2 名单 10 条   ✅

    ⚠️ **只发字段 1（type）**。同一条请求多带 uidself / countryidself 会被
    `result:31` 拒掉 —— 实测过。真客户端每次打开争霸战都发这一条，
    早先把它归成"只是取自己的排名、不要实现"，漏了"没名次时会给你安排一个"
    这一半。
    """
    since = _send(sock, rec, OP_OPT, {1: ("int32", 5)})
    r = _await_response(
        sock, rec, RSE_OPT, since, timeout,
        want=lambda d: (d.get("type") == 5 and d.get("result") == 1
                        and _rank_of(d.get("indexself"))),
        relaxed=lambda d: d.get("type") == 5)
    if not isinstance(r, dict) or r.get("result") != 1:
        return None
    return _rank_of(r.get("indexself"))


def pick_target(entries, my_rank, prefer="最弱优先", allow_player=True,
                avoid=()):
    """从名单里挑一个目标，返回 (名次, uid)；挑不出返回 None。

    玩家的要求是"优先打铁血部落/地狱犬/神圣十字军/黑寡妇/魔多军团/游牧民族
    这些 NPC，真没有了再打真人"。这六家正好就是 NPC 表里的全部派系，
    所以实现成"NPC 优先，没有 NPC 才退回真人"。

    NPC 之间怎么选：
      最弱优先（默认）—— 按官方表里的**战力下限**取最低的，赢面最大；
                        战力相同则取名次更靠前的。
      最高名次优先   —— 取名次数字最小的，赢一场跨得最远，但风险大。
    真人只在名单里一个 NPC 都没有时才打，且取**名次离自己最近**的那个，
    也就是窗口里最保守的一个。

    avoid 是已经打输过的 (名次, uid)。打输了名次不动，下一轮拿到的名单
    一模一样，不排掉就会对着同一个人一直撞到次数耗光。
    """
    avoid = set(avoid)
    usable = [e for e in entries if e[0] < my_rank and e not in avoid]
    npcs = [(r, u) for r, u in usable if npc_id(u)]
    if npcs:
        if prefer == "最高名次优先":
            return min(npcs, key=lambda x: (x[0], NPC[npc_id(x[1])][1]))
        return min(npcs, key=lambda x: (NPC[npc_id(x[1])][1], x[0]))
    if not allow_player:
        return None
    players = [(r, u) for r, u in usable if not npc_id(u)]
    if not players:
        return None
    return max(players, key=lambda x: x[0])


def _fight(sock, rec, me, my_rank, target, country, timeout=8.0):
    """发一次挑战，返回 RseArenaOpt 的响应；没等到返回 None。

    判据要死死咬住**本次目标**：同一条 RceArenaOpt(0469) 还兼着"领上期
    排名奖励"（type:3），而抓包里挑战之后服务端另会推一条只有
    {indexself:-1, result:1} 的裸回包。只认 type==1 且 uidfight/indexfight
    都对得上的那条，才不会把别的东西当成这一击的结果（踩坑记录第 8 条）。
    """
    rank, uid = target
    since = _send(sock, rec, OP_OPT,
                  {1: ("int32", 1), 2: ("string", str(me)),
                   3: ("string", str(uid)), 4: ("int32", my_rank),
                   5: ("int32", rank), 6: ("int32", country)})
    return _await_response(
        sock, rec, RSE_OPT, since, timeout,
        want=lambda d: (d.get("type") == 1 and d.get("indexfight") == rank
                        and str(d.get("uidfight")) == str(uid)))


def _my_uid(rec):
    """自己的 uid，取不到返回空串。

    请求里的 uidself 要用它，而服务端回包里从来不出现它，只能从 FlashVars
    上下文来（Recorder.enable_crypto 会填 rec.uid）。
    """
    return str(getattr(rec, "uid", "") or "") if rec else ""


# ---------------------------------------------------------------- 对外

def daily_challenge(rec, sock, config):
    """每日任务用：把今天的挑战次数打完。

    返回 (是否成功, 说明)，签名符合 daily.Task 的 runner 约定。
    """
    conf = (config.get("争霸战", {}) or {})
    want = int(conf.get("每日挑战次数", 10))
    out = run(rec, sock, config, rounds=want)
    done = out["挑战"]
    # 服务端说次数已经归零，这件事今天就算做完了 —— 哪怕本次一场没打
    # （玩家自己在游戏里打完了就是这种情形）。不这么判的话，每轮都会
    # 报一次 ❌ 并且明天之前一直重试。
    ok = done >= want or out["剩余次数"] == 0
    # 汇总按 v.startswith("成功") 判 ✅（socket_keepalive._push_daily_summary），
    # 打满了却不以"成功"开头会显示成失败。
    why = (f"{'成功：' if ok else ''}挑战 {done}/{want} 次（胜 {out['胜']}）；"
           f"名次 {out['起始名次']}→{out['名次']}；积分 +{out['积分']}；"
           f"剩余挑战 {out['剩余次数']} 次")
    if out["停止原因"] and not ok:
        why += f"；{out['停止原因']}"
    return ok, why


def run(rec, sock, config: dict, rounds: int = 0) -> dict:
    """自动挑战争霸战。rounds 是最多打几次，返回成果字典。

    ⚠️ **不要在这里碰 `_daily._BEAT`**。争霸战只作为每日任务运行，进来之前
    `daily.run()` 已经把心跳器装好了；这里再赋一次值（哪怕是 None）就会把它
    废掉，任务全程一次心跳都不发 —— 国战 2026-08-29 实盘正是这么栽的。
    等回包时的心跳由 `daily._await_response` / `_nap` 负责续。
    """
    conf = (config.get("争霸战", {}) or {})
    # 自己的国家 ID：争霸战的名次是**本国内部**排名，这个值错了整张名单都不对，
    # 而面板回包里偏偏没有它。改成从服务端读（登录时就推来了），
    # config 里填了非 0 才覆盖 —— 以前写死 3，那只是这个号是英国。
    country = (int(conf.get("自己国家ID") or 0)
               or int((config.get("国战", {}) or {}).get("自己国家ID") or 0)
               or _daily.read_my_country(rec))
    gap = float(conf.get("挑战间隔秒", 5))
    prefer = str(conf.get("选目标", "最弱优先"))
    allow_player = bool(conf.get("没有NPC时打真人", True))
    out = {"挑战": 0, "胜": 0, "积分": 0, "名次": None, "起始名次": None,
           "剩余次数": None, "停止原因": ""}
    if rounds <= 0:
        out["停止原因"] = "次数为 0，什么都没做"
        return out
    if not country:
        out["停止原因"] = ("读不到自己的国家ID（RseFightSimpInfo.countryid 和 "
                        "RseLoad.countryData.field5 都没有），停手；"
                        "可在 config 的「争霸战.自己国家ID」里手填")
        return out
    return _loop(rec, sock, rounds, country, gap, prefer, allow_player, out)


def _loop(rec, sock, rounds, country, gap, prefer, allow_player, out):
    me = _my_uid(rec)
    if not me:
        out["停止原因"] = ("拿不到自己的 uid（rec.uid 为空），"
                        "构造不出 RceArenaOpt.uidself，一枪不发")
        return out
    p0 = panel(sock, rec)
    my_rank, left, score = _read_panel(p0)
    if left is None:
        out["停止原因"] = "读不到争霸战面板（剩余次数未知），停手"
        return out

    # 赛季刚开（每周一上午十点）时自己还没上榜，服务端给的名次是 -1，
    # 这时 type:2 取不到名单。先让它把我们排进去。
    if my_rank is None:
        log.info("[争霸战] 本期还没上榜（新赛季），先发 type:5 登记")
        my_rank = join_board(sock, rec)
        if my_rank is None:
            out["停止原因"] = "未上榜，且 RceArenaOpt{type:5} 没能排进榜单，停手"
            return out
        log.info("[争霸战] 已登记，本期名次 %s", my_rank)
        _nap(0.6)
        p0 = panel(sock, rec) or p0
        r2, l2, s2 = _read_panel(p0)
        my_rank = r2 if r2 is not None else my_rank
        left = l2 if l2 is not None else left
        score = s2 if s2 is not None else score

    out["起始名次"] = out["名次"] = my_rank
    out["剩余次数"] = left
    score0 = score or 0
    log.info("[争霸战] 开局：本国名次 %s，剩余挑战 %s 次，积分 %s（国家 %s）",
             my_rank, left, score, country)

    fails = 0
    last_act = 0.0
    lost_to = set()          # 打输过的 (名次, uid)，下一轮别再挑同一个
    # ⚠️ 按**实际打成的场数**循环，不是按轮数 —— 一次"没生效"的试探
    # （比如新赛季在试 indexself）不该吃掉一个名额，否则就打不满十场了。
    # attempts 只是防死循环的兜底。
    attempts, max_attempts = 0, rounds * 3 + 10
    while out["挑战"] < rounds and attempts < max_attempts:
        attempts += 1
        i = out["挑战"] + 1
        # 铁律：先查询、没次数就不做。nCanFightTimes 是服务端给的唯一依据，
        # 读不到或已归零就收手 —— 面板里另有 nBuyFightTimes，说明次数用完
        # 之后是可以花钱买的，绝不能撞上去试。
        if left <= 0:
            out["停止原因"] = "今日挑战次数已用完（nCanFightTimes=0）"
            break

        entries, names = rank_window(sock, rec, my_rank, country)
        if not entries:
            out["停止原因"] = "取不到可挑战名单，停手"
            break
        target = pick_target(entries, my_rank, prefer, allow_player, lost_to)
        if target is None and lost_to:
            # 打输过的都排除完了，说明这一圈名单已经被打了个遍。
            # **目标是把十次挑战用满**（输赢无所谓，每日任务只认次数），
            # 所以这里不能收手 —— 清掉排除名单，从头再挑一个。
            log.info("[争霸战] 名单里的人都打过一轮了，清空排除名单接着打")
            lost_to.clear()
            target = pick_target(entries, my_rank, prefer, allow_player)
        if target is None:
            out["停止原因"] = ("名单里没有 NPC，且配置不允许打真人"
                             if not allow_player else "名单里挑不出可打的目标")
            break

        wait = gap - (time.time() - last_act)
        if wait > 0:
            _nap(wait)

        rank, uid = target
        r = _fight(sock, rec, me, my_rank, target, country)
        last_act = time.time()
        if not isinstance(r, dict):
            out["停止原因"] = "挑战没有回包，停手"
            break

        # 回包的 result 只说明"服务端受理了"（抓包里同一场先后回过 1 和 2），
        # 成败一律以**面板状态的变化**为准：次数少了才算真打了一场，
        # 名次前进了才算赢。一个请求"成功"不等于这件事做成了（踩坑记录第 15 条）。
        _nap(1.0)
        new_rank, new_left, new_score = _read_panel(panel(sock, rec))
        if new_left is None:
            out["停止原因"] = "打完读不到面板，停手"
            break
        if new_left >= left:
            # 注意：**打输了也会扣次数**，所以次数没少只可能是这一击压根没生效
            # （被拒、或者面板还没刷新），不是"输了"。输赢在下面按名次判。
            # 目标是把十次用满，所以给足重试，连着三次没生效才认定是真出问题。
            fails += 1
            log.info("[争霸战] 第 %d 轮打 %s 没消耗次数（result=%s，仍剩 %s 次），"
                     "重试（连续 %d 次）", i, who(uid, names), r.get("result"),
                     new_left, fails)
            if fails >= 3:
                out["停止原因"] = "连续三次挑战没有生效，停手"
                break
            lost_to.add(target)      # 换个目标再试，别对着同一个撞
            left = new_left
            if new_rank is not None:
                my_rank = new_rank
            continue

        fails = 0
        out["挑战"] += 1
        won = new_rank is not None and new_rank < my_rank
        if won:
            out["胜"] += 1
        else:
            # 输了照样算一次挑战（次数已经扣了），每日任务只认次数，不认输赢。
            # 但名次不动、下一轮名单一模一样，所以把这个对手记下来换一个打，
            # 免得十次机会全喂给同一个人。名单打完一圈会自动清空重来。
            lost_to.add(target)
        log.info("[争霸战] 第 %d/%d 轮：打 %s（名次 %s）%s，"
                 "自己名次 %s→%s，剩余 %s 次",
                 i, rounds, who(uid, names), rank, "胜" if won else "负",
                 my_rank, new_rank, new_left)
        if new_rank is not None:
            my_rank = new_rank
        left, score = new_left, new_score

    out["名次"] = my_rank
    out["剩余次数"] = left
    out["积分"] = (score or 0) - score0
    return out

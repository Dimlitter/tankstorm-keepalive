# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""公会战 —— 参加（报名）。

这一项欠了很久：此前两份抓包里都找不到参战流程，任务表里长期写着
「参加尚未实现」。2026-08-30 的抓包解决了它，而且答案就在眼皮底下 ——
**`RceGuildOpt{type:70}` 就是"参加公会战"**。

为什么以前没认出来
------------------
type:70 的回包顶层只有 `type / ret / userGuild` 三个字段，看着就是"查看自己
的公会"，所以一直被当成浏览类请求放过了。真正的状态变化埋在两层嵌套加一个
通用字段名里：`userGuild.field17.field1`。

8/30 抓包的铁证（时刻是从 pcap 的包时间还原的，不是估的）：

| 请求 | 发出时刻 | 回包里的 userGuild.field17.field1 |
|---|---|---|
| `type:73`（公会列表）| 1788068602.4 | 1787556135 ← 5.93 天前的旧值 |
| `type:80`（列表翻页）| 1788068603.5 | 1787556135 ← 还是旧值 |
| **`type:70`** | **1788068607.7** | **1788068608** ← **同一秒，被刷新了** |

前后差 0.3 秒，而它前面 5 秒发的两条请求读到的都还是旧值。所以这个时间戳
就是**上次参加公会战的时刻**，由 type:70 写入。

⚠️ 那个 5.93 天只是"玩家有将近六天没参加过"，**不是活动周期**。
公会战是**一天一场**（用户确认）。所以限流按自然日算就对了，
别把这个间隔当成什么周期性依据。

限流依据
--------
两个条件都要满足才发：

  · `dayHasPK`（来自 type:73 的回包）为 true —— 今天有公会战活动
  · `userGuild.field17.field1` 不在今天之内 —— 今天还没参加过

判成败也落在同一个字段上：回包里的时间戳必须被刷新到"刚刚"，
而不是看 `ret`（type:70 的 ret 恒为 0，看它等于没看）。这是踩坑记录
第 15 条的做法：一个请求"成功"不等于这件事做成了。

⚠️ 与国战无关。这里是公会之间的战斗，走 RceGuildOpt；国战打魔多军团走的是
RceCountryOpt，两回事。
"""

import time

from . import sender
from .daily import _await_response, _nap, _read_path
from .log import get_logger
from .proto_encode import encode_message

log = get_logger()

OPCODE = "0479"
RSE = "RseGuildOpt"

TYPE_LIST = 73          # 公会列表，回包带 dayHasPK 和自己的参战时刻
TYPE_JOIN = 70          # 参加公会战

# 上次参加公会战的时刻。名字是 field17.field1 —— schema 里这两层都没解出真名，
# 含义靠 8/30 抓包的时刻对齐锁死（见模块开头的表）。
F_JOINED_AT = "userGuild.field17.field1"


def _fields(type_):
    """照抓包的字段集构造请求。

    真客户端 7 个字段全都显式写出来（其余 6 个都是 0），proto2 里"写了 0"
    和"没写"是两回事，所以一个都不能省 —— 这是踩坑记录第 6 条。
    """
    return {2: ("int32", type_), 4: ("int32", 0), 13: ("int32", 0),
            17: ("int32", 0), 18: ("int32", 0), 22: ("int32", 0),
            23: ("int32", 0)}


def _send(sock, rec, type_):
    before = rec.seq_mark() if rec else 0
    sender.send_frame(sock, OPCODE, encode_message(_fields(type_),
                                                   omit_zero=False),
                      rec.rc4_c2s)
    return before


def _wait(sock, rec, since, type_, timeout=6.0):
    """等 RseGuildOpt，且必须是**本次 type** 的回包。

    公会这一族全走同一个 opcode（列表/战报/捐献/参战都是 0479），
    不核对 type 就会把上一步的回包当成这一步的结果。
    """
    return _await_response(sock, rec, RSE, since, timeout,
                           want=lambda d: d.get("type") == type_)


def _same_day(ts, now=None):
    """两个 unix 秒是不是同一个自然日（按本机时区）。

    公会战一天一场，所以"今天参加过没有"就是限流依据。按**本机自然日**算，
    和 logs/daily-state.json 的跨天逻辑保持一致 —— 整个每日任务体系都是这个
    模型，只给这一项换一套反而会前后矛盾。

    已知的小局限：服务端的"一天"要是不在本地零点翻篇，跨过它那一刻的那一次
    可能会被当成"今天已经参加过"而跳过。实际影响很小（每天本来也只跑一次），
    真遇到了再按服务端给的时刻去校准。
    """
    if not isinstance(ts, int) or isinstance(ts, bool) or ts <= 0:
        return False
    now = now if now is not None else time.time()
    return time.localtime(ts)[:3] == time.localtime(now)[:3]


def daily_join(rec, sock, config):
    """每日任务用：今天有公会战、且还没参加过，就参加。

    返回 (是否成功, 说明)，签名符合 daily.Task 的 runner 约定。
    """
    conf = (config.get("公会战", {}) or {})
    if not conf.get("自动参加", True):
        return True, "成功：未开启自动参加公会战，什么都没做"

    # 前置：开公会列表。dayHasPK 和"上次参加时刻"都在这条回包里。
    since = _send(sock, rec, TYPE_LIST)
    panel = _wait(sock, rec, since, TYPE_LIST)
    if not isinstance(panel, dict):
        return False, "读不到公会面板（type:73 没回包），不参加"

    has_pk = panel.get("dayHasPK")
    if has_pk is not True:
        return True, f"成功：今天没有公会战活动（dayHasPK={has_pk}），不用参加"

    joined_at = _read_path(panel, F_JOINED_AT)
    if joined_at is None:
        # 铁律：读不到依据就不做。这个字段是唯一能说明"参加过没有"的东西。
        return False, (f"读不到上次参加时刻（{F_JOINED_AT}），"
                       f"判断不了今天参加过没有，不发（读不到就不做）")
    if _same_day(joined_at):
        return True, (f"成功：今天已经参加过了"
                      f"（{time.strftime('%m-%d %H:%M', time.localtime(joined_at))}）")

    log.info("[公会战] 今天有活动，上次参加是 %s，准备报名",
             time.strftime("%m-%d %H:%M", time.localtime(joined_at))
             if joined_at else "（没有记录）")

    _nap(0.6)
    sent_at = time.time()
    since = _send(sock, rec, TYPE_JOIN)
    r = _wait(sock, rec, since, TYPE_JOIN)
    if not isinstance(r, dict):
        return False, "参加请求没有回包，停手"
    ret = r.get("ret")
    if ret not in (0, None):
        return False, f"服务器拒绝 ret={ret}"

    # 成败以状态变化为准：时间戳必须被刷新到"刚刚"。type:70 的 ret 恒为 0，
    # 光看它等于没看（踩坑记录第 15 条）。留 5 秒余量给时钟差。
    now_at = _read_path(r, F_JOINED_AT)
    if not isinstance(now_at, int) or isinstance(now_at, bool):
        return False, f"回包里读不到 {F_JOINED_AT}，认不出成败"
    if now_at < sent_at - 5:
        return False, (f"参加时刻没有被刷新（还是 "
                       f"{time.strftime('%m-%d %H:%M', time.localtime(now_at))}），"
                       f"没报上名")
    log.info("[公会战] 已参加，服务端记的时刻 %s",
             time.strftime("%m-%d %H:%M:%S", time.localtime(now_at)))
    return True, (f"成功：已参加公会战（服务端记录时刻 "
                  f"{time.strftime('%m-%d %H:%M', time.localtime(now_at))}）")

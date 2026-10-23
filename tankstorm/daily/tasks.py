# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""每日任务表。表的顺序即执行顺序，ORDER_LAST 里的排到末尾。

fields 形如 {字段号: ("int32"|"string", 值)}；字段号来自 docs/redwar.proto。
标 "实测" 的参数来自解密后的真实上行请求；"待确认" 的默认跳过，
需先用 tools/capture_daily.py 从抓包里提取真实值后再转正。

加新任务前先看 README「每日任务」一节。铁律是
**先查询、没次数就不做；没有次数依据就不重复**。
"""

from .. import arena, campaign, country_war, guild, shop
from . import pickers
from .model import FromResponse, FromServer, Followup, Gate, Task, Tiers

TASKS = [
    # ---- 纯领取（第一批）----
    # ⚠️ 2026-08-13 纠正：**以前根本没在签到**，只发了查询那一条。
    # 原注释写"客户端只发 nType=0"是当初看漏了第二条。8/10 真客户端抓包实际是两条：
    #   {nType:0, nActivetype:0} 查询 → bSignIn:false, nSignInDays:8
    #   {nType:1, nActivetype:0} 签到 → bSignIn:true,  nSignInDays:9
    # 响应里 field9 是当月 31 天的 bool 数组，签到后对应那天由 false 翻 true。
    # nDay 和 nGiftID 客户端确实不发（服务端自己算今天是第几天），这点原来没错。
    Task("每日签到", "每日签到", "04a4", "RceDailySignIn",
         {2: ("int32", 1), 4: ("int32", 0)},
         "实测", "8/10 抓包：{nType:0} 查询 → {nType:1} 签到。"
                 "闸门读 bSignIn：false 才签，true 说明今天签过了",
         prelude=[("04a4", {2: ("int32", 0), 4: ("int32", 0)})],
         gate=Gate("RseDailySignIn", "bSignIn", claimed_flag=True),
         report=("bSignIn", "nSignInDays", "nReplenishDays")),

    # 限时活动，会不定期上下线。2026-08-29 下线过一次：请求 18:55:21 发出，
    # 到 18:55:29 收到别的任务的回包为止整整 7 秒零回应，同一轮里其它任务
    # 都是 1 秒内回包。09-02 又恢复了。下线期间发出去只是超时跳过，
    # 不影响别的任务，所以默认开着。
    Task("七天乐", "七天乐领奖", "047a", "RceSevenDays",
         {1: ("int32", 1),
          2: ("int32", FromResponse("RseSevenDays", pickers.sevendays_day,
                                    "取当天且未领的那天")),
          3: ("int32", 1)},
         "实测", "8/10 抓包：{type:0} 查询 → {type:1,day:3,gifttype:1} 领取。"
                 "响应 logonDays=登录到第几天，hasGetGift[day-1]=该天是否已领。"
                 "只领当天那份，不去猜历史几天能否补领",
         prelude=[("047a", {1: ("int32", 0)})],
         # ⚠️ RseSevenDays 既没有 ret 也没有 result，judge() 找不到状态码就直接
         # 判成功。真正能说明领到了的是 hasGetGift[day-1] 由 false 翻 true
         # （2026-09-02 实盘：领前全 false，领第 2 天后变成 [F,T,F,F,F,F,F]）。
         # 眼下先把它 report 出来，让这个状态在日志和推送里看得见 ——
         # 判据本身还差一步：success_flag 只能盯单个布尔字段，表达不了
         # "数组里第 day-1 格"，要补得先扩 success_flag 的写法。
         report=("logonDays", "hasGetGift")),

    Task("每日资源", "每日免费资源", "0444", "RceGetDailyRes",
         {}, "待确认", "无字段或字段未知，需实测"),

    Task("战功排名", "战功榜奖励", "04a2", "RceZhanGongRank",
         {1: ("int32", 0)}, "待确认", "需实测"),

    Task("月卡领取", "月卡每日额度", "0408", "RceRedwarMonthCard",
         {1: ("int32", 0), 2: ("int32", 0)},
         "待确认", "未开通月卡则无意义，config 里默认关闭"),

    # ---- 有免费次数的：必须带 guard，免费用完就停 ----
    #
    # 截图实测各模块的免费额度与付费变体：
    #   开采石油   免费 3/3、1/1        第三档 500 勋章
    #   金属冶炼   免费 3/3、1/1        高级   500 勋章
    #   特工派遣   免费 3/3、1/1        高级   需紫色特工令
    #   配件探索   免费 1/1             10次 100 勋章、50次 500 勋章
    #   军备制造   免费 3/3             10次 300 勋章、50次 1500 勋章
    #   矿区探索   低/中级 刷新 5/5     高级   100 勋章
    #   征战世界   免费重征 2/2         付费重征 1/1
    # 一律只做免费档，且 guard 读到剩余次数 >0 才发。

    Task("英雄开采", "英雄中心·开采石油", "0402", "RceHeroVisit",
         {3: ("int32", 1), 4: ("int32", 0)},
         "实测", "实测顺序：RceHeroOpen{type:0} 开面板 → RceHeroVisit{free:1,type:0}。"
                 "8/10 抓包：RseHeroOpen.freeVisitCnt=[低级,中级,高级]=[3,1,0]，"
                 "三次免费后依次 [2,1,0]→[1,1,0]→[0,1,0]。"
                 "第四次客户端改发 {free:0,credit:20} 走付费档 —— 我们只做第 0 档免费",
         max_per_day=5,
         prelude=[("0400", {3: ("int32", 0)})],
         gate=Gate("RseHeroOpen", "freeVisitCnt"),
         tiers=Tiers(4, [0, 1, 2])),

    Task("将领冶炼", "将领·金属冶炼", "0450", "RceAdmiralVisit",
         {3: ("int32", 1), 4: ("int32", 0)},
         "实测", "实测顺序：RceAdmiralOpen{type:0} → RceAdmiralVisit{free:1,type:0}",
         max_per_day=5,
         prelude=[("044e", {3: ("int32", 0)})],
         gate=Gate("RseAdmiralOpen", "freeVisitCnt"),
         tiers=Tiers(4, [0, 1, 2])),

    # 8/12 抓包：英雄培养走的是通用的建筑操作 opcode，不是英雄那套。
    #   RceBuildingModify {id:10049(英雄中心), type:75(培养),
    #                      heroType:1122, heroupgradeIndex:0}
    # 响应 RseBuildingModify 用的是 error 字段而不是 ret，error=0 为成功。
    # ⚠️ heroType 是**这个号自己的英雄编号**，换号要重新抓。
    # 这一项和别的不一样：它**本来就不是"免费次数"型**的。
    # 培养消耗的是石油和金属 —— 会自然回复的产出资源，不是券也不是勋章，
    # 花掉不心疼；一轮 8 小时，做完就能再做。所以没有剩余次数字段是正常的，
    # 不是漏找了。用冷却限流即可，每 8 小时一次、一天至多 3 次。
    #
    # 安全性同参谋/军备：这条消息里 10=credit、13=usehonorcredit、
    # 17/18=itemID/itemCount 才是花钱字段，付费变体要显式带上它们，
    # 我们一个都不发，安全检查也会拦，所以不可能变成花钱的那一档。
    Task("英雄培养", "英雄中心·培养（8 小时一轮）", "0414", "RceBuildingModify",
         {2: ("int32", 10049), 3: ("int32", 75),
          15: ("int32", 1122), 16: ("int32", 0)},
         "实测", "8/12 抓包实测。消耗石油/金属（自然回复的资源），一轮 8 小时可重复，"
                 "因此靠冷却而非免费次数限流。响应 RseBuildingModify 用 error 字段判成败。"
                 "⚠️ heroType=1122 是本账号的英雄编号，换号必须重抓",
         max_per_day=3, cooldown_sec=8 * 3600),

    # 将领和参谋是两份独立的技能书，各领各的。原先是一个任务 max_per_day=2，
    # 但 fields 写死 ActiveType=0，跑第二次只是把将领那份又领一遍。
    # 8/10 抓包实测客户端确实发了两组：{0,0}→{1,0} 和 {0,1}→{1,1}。
    #
    # 技能书不是"一天一次"，是**每 24 小时一次**，而且以前既没闸门也没冷却，
    # 每轮都照发。2026-08-13 03:13 帧日志实测：
    #   查询 {OptType:0,ActiveType:0} → ret=0, field4=[
    #        {field1:1, field2:0, field3:1786609115},   ← 免费档：剩 0 次
    #        {field1:2, field2:3, field3:0},
    #        {field1:3, field2:0, field3:0}]
    #   领取 {OptType:1,ActiveType:0} → **ret=1**（被拒，当时还在冷却里）
    # field3 是"下次可用时刻"（unix 秒），实测正好是上次领取 + 24 小时；
    # 将领和参谋各有各的计时（两者相差 4 秒，正是昨天两次领取的间隔）。
    # 所以闸门读免费档的剩余次数，冷却读免费档的下次可用时刻，都按 field1==1
    # 选元素而不是按下标。
    Task("技能书将领", "将领·免费技能书", "048a", "RceBookCollection",
         {1: ("int32", 1), 2: ("int32", 0)},
         "实测", "实测 {OptType:0,ActiveType:0} 查询 → {OptType:1,ActiveType:0} 领取。"
                 "闸门读 field4 里 field1==1（免费档）的 field2 剩余次数，"
                 "冷却读同一元素的 field3（上次领取+24 小时）",
         prelude=[("048a", {1: ("int32", 0), 2: ("int32", 0)})],
         gate=Gate("RseBookCollection", "field4[field1=1].field2"),
         cooldown_until="field4[field1=1].field3"),

    Task("技能书参谋", "参谋·免费技能书", "048a", "RceBookCollection",
         {1: ("int32", 1), 2: ("int32", 1)},
         "实测", "实测 {OptType:0,ActiveType:1} 查询 → {OptType:1,ActiveType:1} 领取。"
                 "闸门与冷却同「技能书将领」，但参谋这一档自己单独计时",
         prelude=[("048a", {1: ("int32", 0), 2: ("int32", 1)})],
         gate=Gate("RseBookCollection", "field4[field1=1].field2"),
         cooldown_until="field4[field1=1].field3"),

    # ⚠️ 只做一次。曾经放开到 3 次、靠"服务器拒绝再收手"，那是错的：
    # 读不到剩余次数就等于不知道还免不免费，撞过头就开始扣勋章。
    # 规矩是**先查询、没次数就不做**，查不到次数就只做一次。
    # RseAdviserOpt 有 nVistAdvCnt 字段疑似次数，但实测响应里没带，暂不敢用。
    # 参谋分两档：noptType 11=普通、12=高级，各有独立的免费次数。
    # **不是每天都能做** —— 8/12 抓包实测高级用掉后，下次可用时刻推到了
    # 2.6 天以后。所以绝不能按"一天一次"发，必须读次数。
    #   sVisitData.field4 = [普通剩余, 高级剩余]   实测 [0,1] → 用掉高级 → [0,0]
    #   sVisitData.field5 = [普通下次可用时刻, 高级下次可用时刻]（unix 秒）
    # 用券版是再带一个 nitemID 字段，我们永远不发，所以花不掉券。
    Task("参谋操作", "参谋·免费招募（普通+高级）", "04da", "RceAdviserOpt",
         {1: ("int32", 11)},
         "实测", "8/12 抓包：{noptType:1} 查询 → {noptType:11} 普通 / {noptType:12} 高级。"
                 "闸门读 sVisitData.field4=[普通,高级] 剩余次数；"
                 "field5 是下次可用时刻，实测高级间隔约 2.6 天，绝非每日",
         max_per_day=4,
         prelude=[("04da", {1: ("int32", 1)})],
         gate=Gate("RseAdviserOpt", "sVisitData.field4"),
         tiers=Tiers(1, [11, 12])),

    # 争霸战·领取上期排名奖励。2026-08-10 真客户端抓包（全过程-低级英雄招募）实证：
    #   开面板 RceArenaInfo{type:1} → RseArenaInfo{..., bLastRankGet:false,
    #                                 nRankSelf:633, nRankSelfLast:870,
    #                                 nCanFightTimes:10, nIntegralScore:14798}
    #   领奖   RceArenaOpt{type:3, indexself:0} → RseArenaOpt{type:3, result:1}
    #   领完再开面板 → bLastRankGet **翻成 true**，nIntegralScore 14798→15015
    # 所以闸门就是 bLastRankGet：false 才领，true 说明领过了。
    # ⚠️ RseArenaOpt 没有 ret，成败看 result（1=成功），见 RESULT_IS_STATUS。
    # 抓包里客户端还发过 RceArenaOpt{type:5}：已上榜时它只回自己的名次，
    # 未上榜（新赛季）时它负责把自己排进榜单。那一步归「争霸战挑战」
    # （arena.join_board），领奖这一项不发。
    # RseArenaInfo.bScoreGiftGain（积分礼包是否已领）和 RceArenaOpt.nGainScoreGift
    # 看着就是"领积分"那一档，但抓包里客户端从没领过，**没有实测参数，先不做**。
    Task("争霸战领奖", "争霸战·领取上期排名奖励", "0469", "RceArenaOpt",
         {1: ("int32", 3), 4: ("int32", 0)},
         "实测", "8/10 真客户端抓包：RceArenaInfo{type:1} 开面板 → "
                 "RceArenaOpt{type:3,indexself:0} 领奖，回包 result=1，"
                 "随后 bLastRankGet 由 false 翻 true",
         prelude=[("0468", {1: ("int32", 1)})],
         gate=Gate("RseArenaInfo", "bLastRankGet", claimed_flag=True),
         report=("nRankSelf", "nRankSelfLast", "nIntegralScore",
                 "nCanFightTimes", "nJoinPlayers", "bScoreGiftGain")),

    # 争霸战·挑战。形状不合流水线（每打一场都要重新取一次可挑战名单、
    # 从里面挑目标，名次还会跟着变），所以走 runner。排在领奖之后：
    # 先把上期的奖领了，再开打。
    Task("争霸战挑战", "争霸战·挑战 10 次", "0469", "RceArenaOpt",
         {}, "实测",
         "8/29 抓包实测三场：RceArenaInfo{type:1} 开面板读 nCanFightTimes → "
         "RceArenaRankInfo{type:2,nIndex,nCountry} 取可挑战名单 → "
         "RceArenaOpt{type:1,uidself,uidfight,indexself,indexfight,"
         "countryidself} 挑战。优先打 NPC（短 uid），成败以面板变化为准",
         runner=arena.daily_challenge),

    # 锦鲤心愿宝箱（限时活动）。8/30 抓包实测：
    #   {optype:0, activetype:1} 查询 → ngetdailyawd=false
    #   {optype:2, activetype:1} 领取 → ngetdailyawd=true
    # 消息名叫 RceDoubleElevenOfficer（双十一军官），游戏把同一个 opcode 复用给
    # 各种限时活动，靠 activetype 区分；activetype=1 就是锦鲤心愿这一档。
    # ⚠️ 回包里**既没有 ret 也没有 result**，judge() 找不到状态码会直接判成功。
    # 所以用 success_flag 盯住 ngetdailyawd —— 它翻成 true 才是真领到了。
    # 真客户端只发 optype(1) 和 activetype(4)，id(2)/recday(3) 一个都不发，照抄。
    # 限时活动，会不定期上下线（七天乐 08-29 下线、09-02 又回来了，这一族都这样）。
    # 下线期间发出去只是超时跳过，不影响别的任务，所以默认开着。
    Task("锦鲤心愿", "限时活动·领锦鲤心愿宝箱", "04af", "RceDoubleElevenOfficer",
         {1: ("int32", 2), 4: ("int32", 1)},
         "实测", "8/30 抓包实测：optype:0 查询 → optype:2 领取，"
                 "ngetdailyawd 由 false 翻 true。不消耗任何资源",
         prelude=[("04af", {1: ("int32", 0), 4: ("int32", 1)})],
         gate=Gate("RseDoubleElevenOfficer", "ngetdailyawd", claimed_flag=True),
         success_flag="ngetdailyawd",
         report=("ngetdailyawd", "ncangetCnt")),

    # 功勋商城补支援兵。形状不合流水线（要先读背包库存、算差额、还要在买完之后
    # 核对勋章有没有被扣），所以走 runner。**默认关闭**，见 tankstorm/shop.py。
    Task("补支援兵", "功勋商城·补摩多军团支援兵", "041f", "RcePurchase",
         {}, "实测",
         "8/30 抓包实测：RcePurchase{credit:100n, type:SHOP, shopID:1199, "
         "credittype:2, buynum:n} → 背包 10118 增加 n 个、功勋减少 100n、"
         "勋章分文未动。这是全项目唯一会主动花钱的请求，默认关闭",
         runner=shop.daily_restock),

    # 国战：世界地图里攻击摩多军团。形状不合流水线（要先召唤支援兵、再从服务端
    # 推来的 RseCountryUserLst 里读出目标 ID 才能打），所以走 runner。
    Task("国战攻击", "国战·攻击摩多军团 10 次", "0463", "RceCountryOpt",
         {}, "实测",
         "8/29 抓包实测：type:3 开驻地面板 → type:45 召唤支援兵 → "
         "type:14 普通攻击（每次 5 点行动力、dayatktimes+1）。"
         "五种请求已与真客户端逐字段比对一致",
         runner=country_war.daily_attack),

    Task("特工派遣", "远程火炮·特工派遣", "04d6", "RceTrenchMortarOpt",
         {1: ("int32", 1), 2: ("int32", 1001)},
         "实测", "实测 optType:0 查询 → optType:1 + subType 1001/1002/1003 三档。"
                 "查询响应 RseTrenchMortarOpt.freeVisitCnt 给出各档剩余免费次数",
         max_per_day=5,
         prelude=[("04d6", {1: ("int32", 0)})],
         gate=Gate("RseTrenchMortarOpt", "freeVisitCnt"),
         tiers=Tiers(2, [1001, 1002, 1003])),

    # 8/10 抓包：客户端把 7 个字段全都显式写了（除 sceneID 外一律 0），
    # 不是只发 sceneID。protobuf 里"显式写 0"和"不写"在服务端是
    # hasX=true 和 false 的区别，既然不要钱就照抄客户端。
    Task("配件探索", "配件中心·基地探索", "043a", "RceWPCExplore",
         {1: ("int32", 10001), 2: ("int32", 0), 3: ("int32", 0),
          4: ("int32", 0), 5: ("int32", 0), 6: ("int32", 0), 7: ("int32", 0)},
         "实测", "实测 RceWPCBaseOpen{type:1} 开面板 → RceWPCExplore{sceneID:10001}。"
                 "useItemID/useItemCnt 是库存上报不是消耗，此处一律不发。"
                 "开面板响应 RseWPCBaseOpen.leftFreeCnt 才是剩余免费次数，"
                 "leftTime 是冷却秒数（此前把它当次数用过）",
         max_per_day=5,
         prelude=[("0437", {1: ("int32", 1)})],
         gate=Gate("RseWPCBaseOpen", "leftFreeCnt"),
         tiers=Tiers(1, [10001, 10002, 10003])),

    Task("军备制造", "军备研究·制造", "04e1", "RceJunBeiOpt",
         {1: ("int32", 22), 5: ("int32", 1)},
         "实测", "8/10+8/12 抓包：type:21 → type:0 → type:21 三步前置，"
                 "再 {type:22, nExlType:1} 普通 / nExlType:4 自动化制造厂（高级）。"
                 "nExlType 是 5 号字段 —— 此前误写成 2 号(nJunBeiID)。"
                 "闸门读 field14[].field1=[普通剩余, 高级剩余]，"
                 "field14[].field2 是下次可用时刻；高级实测用掉后推到 0.9 天以后，"
                 "同样不是每天都能做。用券版再带 nItemID，我们不发",
         max_per_day=4,
         prelude=[("04e1", {1: ("int32", 21)}),
                  ("04e1", {1: ("int32", 0)}),
                  ("04e1", {1: ("int32", 21)})],
         gate=Gate("RseJunBeiOpt", "field14[].field1"),
         tiers=Tiers(5, [1, 4])),

    # 开面板响应里的 skilltraintimes 就是剩余训练次数（实测 5→4→…→0）。
    # 次数用完后服务器直接回 ret=11 拒绝，**不会扣费**（要加次数得自己去买），
    # 所以这里本来就是安全的；加闸门只是别再发那两个注定失败的包。
    Task("战略训练", "战争学院·战略技能训练", "04a5", "RceWarCollegeOpt",
         {1: ("int32", 4), 2: ("int32", 0), 3: ("int32", 1)},
         "实测", "8/10 抓包：{type:1} 开面板 → {type:4,trainskilltype:1} 训练。"
                 "开面板响应 skilltraintimes = 剩余次数；用完后 ret=11 拒绝，不扣费",
         max_per_day=7,
         prelude=[("04a5", {1: ("int32", 1), 2: ("int32", 0),
                            3: ("int32", 0)})],
         gate=Gate("RseWarCollegeOpt", "skilltraintimes")),

    # 占领是真正拿收益的那一步，此前只发了查询，等于什么也没做。
    # tokenNum 是当天剩余占领次数（实测 3，占一次变 2），正好当闸门。
    Task("军事演习", "战争学院·军事演习（占场地）", "04a7", "RceWarGameOpt",
         {1: ("int32", 2),
          2: ("int32", FromResponse("RseWarGameOpt", pickers.free_wargame_site,
                                    "挑一个无人占领的演习场"))},
         "实测", "8/10 抓包：{type:1} 查询（分页推 300 个场地/页）→ "
                 "{type:2,siteID:409} 占领。空场地 = 列表条目里没有 field6(占领者)。"
                 "占领响应 bOccupySite=true、siteEndTime 给出结束时刻",
         max_per_day=3,
         prelude=[("04a7", {1: ("int32", 1)})],
         gate=Gate("RseWarGameOpt", "tokenNum"),
         # 占领时长由服务端定，别猜。占领响应里 startTime→siteEndTime
         # 实测相差 14400 秒（4 小时），此前写死的 60 分钟错了四倍。
         cooldown_until="siteEndTime"),

    # 探索只是找矿，占下来才有产出。占矿的 resourceID 来自探索响应。
    Task("矿区争夺", "矿区争夺·探索并占矿", "049a", "RceResourceOpt",
         {1: ("int32", 2), 2: ("int32", 0), 3: ("int32", 1), 4: ("string", "")},
         "实测", "8/10 抓包：{type:1} 查询 → {type:2,searchType:1} 探索 → "
                 "{type:3,resourceID:120007} 占矿。探索响应 field5 是探到的矿，"
                 "只有 field1 没有 field6 的就是无人占领的那个。"
                 "查询响应 searchTimes 是当天剩余搜索次数（实测 5）",
         max_per_day=5,
         prelude=[("049a", {1: ("int32", 1), 2: ("int32", 0),
                            3: ("int32", 0), 4: ("string", "")})],
         gate=Gate("RseResourceOpt", "searchTimes"),
         # 占矿时长跟矿的等级有关，写死必错：实测 8/10 那次约 1.06 小时，
         # 8/12 那次约 1.4 小时。resourceEndTime 在占矿（后续步骤）的回包里。
         cooldown_until="resourceEndTime",
         followup=Followup("049a", pickers.next_mine_to_occupy, max_rounds=1,
                           desc="占下探到的无主矿")),

    # 客户端把 11 个字段全写了（除 type 外都是 0），照抄。
    # 1=costCredit 虽然命中危险字段名，但值是 0，安全检查照样放行。
    Task("国家宝箱", "国家·宝箱领取并开箱", "0463", "RceCountryOpt",
         pickers.country_fields(15),
         "实测", "8/10 抓包三步：RceCountryOpen{} 开面板 → {type:15} 查询 → "
                 "{type:10,count:N} 领取 → {type:11,count:1} 开箱。"
                 "N 取自查询响应的 boxPage.field4（实测 6）。"
                 "此前只发了 type:15，等于只查询没领取",
         prelude=[("0462", {})],
         followup=Followup("0463", pickers.next_country_box, max_rounds=3,
                           desc="领取并开箱")),

    # 参加公会战。2026-08-30 抓包定案：**type:70 就是"参加"**，判据是回包里的
    # userGuild.field17.field1（上次参加时刻）被刷新到"刚刚"。
    #
    # 这一项欠了很久，之前两次都判错，原因值得记下来：type:70 的回包顶层只有
    # type/ret/userGuild，看着就是"查看自己的公会"，于是被归进浏览类放过了。
    # 真正的状态变化埋在两层嵌套加一个通用字段名里。8/30 抓包按 pcap 包时间还原：
    #   type:73 发于 …602.4 → 回包 field17.field1 = 1787556135（六天前的旧值）
    #   type:80 发于 …603.5 → 回包 还是 1787556135
    #   type:70 发于 …607.7 → 回包 **1788068608**，同一秒
    # 前后差 0.3 秒，而它前面两条读到的都还是旧值 —— 只可能是 type:70 写的。
    # 公会战**一天一场**（用户确认），所以按自然日限流：今天参加过就不再发。
    #
    # 形状不合流水线（闸门要同时看 dayHasPK 和那个时间戳是不是今天），走 runner。
    Task("公会战参加", "公会战·参加", "0479", "RceGuildOpt",
         {}, "实测",
         "8/30 抓包实测：type:73 读 dayHasPK 和上次参加时刻 → type:70 参加。"
         "回包里 userGuild.field17.field1 被刷新成请求发出的同一秒。"
         "公会战一天一场，故按自然日限流",
         runner=guild.daily_join),

    # 顺序按抓包来。抓包里每个 RceGuildOpt 都带着这一串 0，照抄。
    # 这一项只打开战报面板（type:14 回包是 userGuild + log），不参战、不领奖。
    Task("公会战", "公会战·看战报", "0479", "RceGuildOpt",
         {2: ("int32", 14), 4: ("int32", 0), 13: ("int32", 0), 17: ("int32", 0),
          18: ("int32", 0), 22: ("int32", 0), 23: ("int32", 0)},
         "实测", "8/10 抓包：type:0 → type:2 → type:73 → type:14。"
                 "type:73 的回包带 dayHasPK＝今天有没有公会战活动。"
                 "这一项只是看战报；「参加」是单独的『公会战参加』任务（type:70）",
         prelude=[("0479", {2: ("int32", 0), 4: ("int32", 0), 13: ("int32", 0),
                            17: ("int32", 0), 18: ("int32", 0),
                            22: ("int32", 0), 23: ("int32", 0)}),
                  ("0479", {2: ("int32", 2), 4: ("int32", 0), 13: ("int32", 0),
                            17: ("int32", 0), 18: ("int32", 0),
                            22: ("int32", 0), 23: ("int32", 0)}),
                  ("0479", {2: ("int32", 73), 4: ("int32", 0), 13: ("int32", 0),
                            17: ("int32", 0), 18: ("int32", 0),
                            22: ("int32", 0), 23: ("int32", 0)})],
         report=("dayHasPK", "lastBtlRank", "curSesionBtlOver", "bRankGet")),

    Task("公会捐献", "公会·捐献", "0479", "RceGuildOpt",
         {2: ("int32", 16), 6: ("string", FromServer("RseInit", "username")),
          12: ("int32", 1)},
         "实测", "8/10 抓包：…→ type:14 → {type:16, tarUserName:'自己的游戏名', "
                 "contributeID:1}。名字不写死，登录时从 RseInit.username 取",
         prelude=[("0479", {2: ("int32", 0)}), ("0479", {2: ("int32", 2)}),
                  ("0479", {2: ("int32", 14)})]),

    # 征战世界：一关一关往下打，打不过为止；输了用免费次数重新开始，接着打。
    # 开局可能停在第 1 关，也可能停在昨天输掉的那关，每一步都得先看服务端给的
    # 进度再决定，套不进流水线，走 runner。能打到第几关看账号实力，每关约 12 秒，
    # 一轮打到第 60 关的号一天两三轮就是半小时上下，所以排在普通任务的最后，
    # 免得把别的任务压后；同时要在「每日任务」领奖之前，它推进的活跃度才领得到。
    #
    # 早先这里只发两次 type:2（重新开始）推进活跃度，不真打。那是照 8/10 抓包做的，
    # 那天玩家确实只点了两次重新开始；8/8 那份才是真打了两轮的完整样本。
    Task("征战世界", "征战世界·打到打不过为止", "045b", "RcePVEFightOpt",
         {}, "实测",
         "8/8 抓包实测两轮：045c{type:1} 开面板 → 045b{type:5} 查进度 → "
         "045b{type:6,bAutoTreat:true} 逐关打（第 1~59 关胜、第 60 关负）→ "
         "045b{type:2} 用免费次数重新开始。细节见 tankstorm/campaign.py",
         runner=campaign.daily_fight),

    # ---- 必须最后执行 ----
    Task("周任务", "周任务领奖", "04de", "RceWeekQuestOpt",
         {1: ("int32", 0), 2: ("int32", 0)}, "待确认", "需实测"),

    # 必须最后执行：前面每做一项，活跃度就涨一截，先领就少领。
    # 实测活跃度 64 时能领 10/30/50 三档，80/100 没达标领不了。
    Task("每日任务", "每日任务·按活跃度领奖", "043d", "RceDailyTask",
         {5: ("int32", FromResponse("RseDailyTask", pickers.eligible_gift_tier,
                                    "取一个达标且未领的档位", fresh=False))},
         "实测", "8/10 抓包：客户端发 {giftID:10} / {giftID:30} / {giftID:50}，"
                 "giftID 是 5 号字段（此前写的 getGift/taskId 是错的）。"
                 "服务器持续推 RseDailyTask，dailyTask.field2 是当前活跃度，"
                 "getGift=[{档位, 是否已领}]。一轮把所有达标档位领完",
         followup=Followup("043d", pickers.next_daily_gift, max_rounds=5,
                           desc="继续领剩下达标的档位")),
]


# 这些任务放到最后执行（它们领的是"前面动作累积出来的"奖励）
ORDER_LAST = {"每日任务", "周任务"}

# 白名单：动作 opcode 和前置 opcode 都要在内 —— 前置请求同样是真实发出的包，
# 漏掉的话等于给它开了后门，绕过白名单检查。
ALLOWED_OPCODES = ({t.opcode for t in TASKS}
                   | {op for t in TASKS for op, _ in t.prelude})


def ordered_tasks():
    """按执行顺序返回任务：普通任务在前，ORDER_LAST 里的排到最后。

    "每日任务领奖"(RceDailyTask) 必须排在最后 —— 前面那些操作本身会推进
    每日任务进度，先领就漏了。尾部内部再排：周任务 → 每日任务。
    """
    head = [t for t in TASKS if t.key not in ORDER_LAST]
    tail = [t for t in TASKS if t.key in ORDER_LAST]
    tail.sort(key=lambda t: 0 if t.key == "周任务" else 1)
    return head + tail

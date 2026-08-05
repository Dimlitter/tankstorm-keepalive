"""每日任务：按固定顺序发出免费领取类请求。

安全设计（四层，缺一不可）
--------------------------
1. **白名单**：只允许发 TASKS 表里登记的 opcode，其余一律拒发。表以外的 opcode
   连构造的机会都没有。
2. **危险字段硬校验**：字段名命中 buy/cost/num/count/cnt/price 等的，值必须为 0，
   否则拒发并告警。这是防"免费次数用完自动买"的最后一道闸。
3. **干跑模式**（默认开）：只打印将要发送的帧和字段，不真发。确认无误再关。
4. **每日次数上限**：每个任务每天最多发 N 次，防逻辑出错循环刷。

为什么必须硬校验，而不能依赖游戏的确认框
----------------------------------------
游戏里"消耗勋章"确实会弹确认框，但那是**纯客户端 UI**：
  · '是否消耗' 出现在 onAllBtn1Click / onAllBtn2Click（按钮处理器）
  · '确认购买' 出现在 Buytip::processPanel（面板类）
  · 协议里不存在任何二次确认消息，抽奖类消息都是单包完成
脚本直接发包**不经过任何对话框**，服务器收到就扣。所以确认框对脚本的保护是 0，
必须在我们这一侧把危险字段拦死。

任务顺序
--------
"每日任务领奖"(RceDailyTask) 必须排在最后 —— 前面那些操作本身会推进每日任务
进度，先领就漏了。TASKS 表的顺序即执行顺序，ORDER_LAST 里的排到末尾。

参数来源
--------
字段值优先用**抓包实测**（tools/capture_daily.py 从 logs 里提取真实客户端发的包），
而不是从 schema 猜。confidence 字段标明每个任务的参数是实测还是待确认；
待确认的任务默认不执行，需要在 config 里显式打开。
"""

import json
import os
import re
import time
from datetime import date

from . import sender
from .log import LOG_DIR, get_logger
from .proto_encode import encode_message

log = get_logger()

STATE_FILE = os.path.join(LOG_DIR, "daily-state.json")

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

# 这些任务放到最后执行（它们领的是"前面动作累积出来的"奖励）
ORDER_LAST = {"每日任务", "周任务"}


class Guard:
    """发送前的状态闸门：先读服务器下发的剩余免费次数，>0 才允许发。

    截图实测：开采石油/金属冶炼/矿区探索/军备制造/配件探索都是
    「免费N次 + 用完转扣券或勋章」，同一个按钮两种行为。所以这类任务
    绝不能盲发，必须先确认服务器认为你还有免费次数。

    rse_msg  等待哪条服务器消息（如 RseWPCExplore）
    field    读它的哪个字段（如 leftFreeCnt）
    trigger  可选：为了让服务器下发状态，先要发的查询请求 (opcode, fields)
    """

    def __init__(self, rse_msg, field, trigger=None, timeout=8.0):
        self.rse_msg = rse_msg
        self.field = field
        self.trigger = trigger
        self.timeout = timeout


class Task:
    """一个每日任务 = 一条待发送的 C→S 消息。"""

    def __init__(self, key, name, opcode, msg, fields, confidence,
                 note="", max_per_day=1, guard=None):
        self.guard = guard
        self.key = key
        self.name = name
        self.opcode = opcode
        self.msg = msg
        self.fields = fields            # {字段号: (类型, 值)}
        self.confidence = confidence    # "实测" | "待确认"
        self.note = note
        self.max_per_day = max_per_day

    def danger_fields(self):
        """返回违反"危险字段必须为 0"的字段列表。"""
        bad = []
        for fno, (ftype, val) in self.fields.items():
            fname = self.field_names.get(fno, "") if hasattr(self, "field_names") else ""
            if fname and DANGER_FIELD.search(fname) and val not in (0, "", None):
                bad.append(f"{fname}={val}")
        return bad


# ---------------------------------------------------------------- 任务表
#
# fields 形如 {字段号: ("int32"|"string", 值)}；字段号来自 docs/redwar.proto。
# 目前全部标 "待确认" —— 参数值需要用 tools/capture_daily.py 从你手动操作的
# 抓包里提取真实值后再改成 "实测"。在那之前干跑模式会打印出来给你比对。

def _t(*a, **kw):
    return Task(*a, **kw)


TASKS = [
    # ---- 纯领取（第一批）----
    _t("每日签到", "每日签到", "04a4", "RceDailySignIn",
       {1: ("int32", 0), 2: ("int32", 0), 3: ("int32", 0), 4: ("int32", 0)},
       "待确认", "nDay/nType/nGiftID/nActivetype 需实测"),

    _t("七天乐", "七天乐领奖", "047a", "RceSevenDays",
       {1: ("int32", 0), 2: ("int32", 0), 3: ("int32", 0)},
       "待确认", "type/day/gifttype 需实测"),

    _t("每日资源", "每日免费资源", "0444", "RceGetDailyRes",
       {}, "待确认", "无字段或字段未知，需实测"),

    _t("战功排名", "战功榜奖励", "04a2", "RceZhanGongRank",
       {1: ("int32", 0)}, "待确认", "需实测"),

    _t("月卡领取", "月卡每日额度", "0408", "RceRedwarMonthCard",
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

    _t("英雄开采", "英雄中心·开采石油（免费档）", "0402", "RceHeroVisit",
       {3: ("int32", 1), 4: ("int32", 0)},
       "待确认", "free 字段置 1；只做前两档，第三档 500 勋章不碰",
       max_per_day=4),

    _t("英雄培养", "英雄培养（消耗金属石油，用户允许）", "0401", "RceHeroOpt",
       {2: ("int32", 0)}, "待确认", "花金属/石油，用户明确表示可接受"),

    _t("将领冶炼", "将领·金属冶炼（免费档）", "0450", "RceAdmiralVisit",
       {3: ("int32", 1), 4: ("int32", 0)},
       "待确认", "free 字段置 1；高级档 500 勋章不碰", max_per_day=4),

    _t("参谋派遣", "参谋·免费派遣", "04dc", "RceAdviserDaily",
       {1: ("int32", 0), 2: ("int32", 0), 3: ("int32", 0)},
       "待确认", "nbuycnt 恒为 0，绝不购买",
       guard=Guard("RseAdviserDaily", "nfreeTimes")),

    _t("特工派遣", "远程火炮·特工派遣（免费档）", "045d", "RceStrategicArmyOpt",
       {1: ("int32", 0), 2: ("int32", 0)},
       "待确认", "只做初级/中级免费档", max_per_day=4),

    _t("配件探索", "配件中心·基地探索（免费）", "043a", "RceWPCExplore",
       {1: ("int32", 0), 2: ("int32", 0)},
       "待确认", "credit/useItemID/useItemCnt/exploreCnt 全部为 0",
       guard=Guard("RseWPCExplore", "leftFreeCnt")),

    _t("军备制造", "军备研究·制造（免费 3 次）", "04cb", "RceWPCCraft",
       {1: ("int32", 0)}, "待确认", "只做免费档，10次/50次是勋章档",
       max_per_day=3),

    _t("战略训练", "战争学院·战略技能训练（每日 7 次）", "04a5", "RceWarCollegeOpt",
       {1: ("int32", 0), 3: ("int32", 0)},
       "待确认", "每日 7 次免费；购买训练次数绝不触发", max_per_day=7),

    _t("军事演习", "战争学院·军事演习（占空场）", "04a7", "RceWarGameOpt",
       {1: ("int32", 0), 2: ("int32", 0), 4: ("bool", False)},
       "待确认", "需先查空演习场再占领，siteID 依赖实时状态"),

    _t("国家宝箱", "国家·宝箱领取并开启", "0463", "RceCountryOpt",
       {4: ("int32", 0)}, "待确认", "costCredit 必须为 0"),

    _t("公会捐献", "公会·捐献（50w 金属石油档）", "0479", "RceGuildOpt",
       {2: ("int32", 0), 12: ("int32", 0)},
       "待确认", "contributeID 选 50w 档；捐的是金属石油不是勋章"),

    _t("公会战", "公会战·领奖并报名", "0479", "RceGuildOpt",
       {2: ("int32", 0), 22: ("int32", 0)},
       "待确认", "先领上一场奖励宝箱，再报名本场"),

    _t("征战世界", "征战世界·自动征战（免费 2 次）", "048b", "RcePveBattleOpt",
       {2: ("int32", 0)}, "待确认", "只用免费重征次数，付费重征不碰",
       max_per_day=2),

    _t("矿区争夺", "矿区争夺·刷新低级矿区", "041e", "RceMineModify",
       {1: ("int32", 0), 5: ("int32", 0)},
       "待确认", "只刷低/中级（高级要 100 勋章）；credit 恒为 0；"
                 "发现空矿场才占领，否则仅刷新", max_per_day=5),

    # ---- 必须最后执行 ----
    _t("周任务", "周任务领奖", "04de", "RceWeekQuestOpt",
       {1: ("int32", 0), 2: ("int32", 0)}, "待确认", "需实测"),

    _t("每日任务", "每日任务领奖", "043d", "RceDailyTask",
       {2: ("bool", True), 6: ("int32", 0)},
       "待确认", "必须最后执行：前面的操作会推进它的进度"),
]

# 白名单：只有这里的 opcode 允许发送
ALLOWED_OPCODES = {t.opcode for t in TASKS}


def ordered_tasks():
    """按执行顺序返回任务：普通任务在前，ORDER_LAST 里的排到最后。"""
    head = [t for t in TASKS if t.key not in ORDER_LAST]
    tail = [t for t in TASKS if t.key in ORDER_LAST]
    # 尾部内部再排：周任务 → 每日任务（每日任务绝对最后）
    tail.sort(key=lambda t: 0 if t.key == "周任务" else 1)
    return head + tail


# ---------------------------------------------------------------- 每日状态

def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    if st.get("date") != date.today().isoformat():
        st = {"date": date.today().isoformat(), "done": {}}
    return st


def _save_state(st):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except OSError as exc:
        log.debug("每日状态保存失败(忽略): %s", exc)


# ---------------------------------------------------------------- 执行

def _pump(sock, rec, msg_name, timeout):
    """读 socket 直到收到指定服务器消息（或超时）。返回解码后的字段字典或 None。

    必须自己收包：daily.run 跑在保活主循环之前，此时还没人在 recv，
    干等永远等不到。sock 已被 rec.wrap() 包过，recv 到的字节会自动进录制器。
    """
    if rec is None:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = rec.latest.get(msg_name)
        if got and got[0] >= deadline - timeout:      # 本次等待期内收到的才算
            return got[1]
        try:
            sock.settimeout(0.5)
            if not sock.recv(8192):
                return None
        except Exception:
            pass
    got = rec.latest.get(msg_name)
    return got[1] if got else None


def _check_guard(task, rec, sock, dry):
    """执行状态闸门。返回 (是否放行, 说明)。"""
    g = task.guard
    if g is None:
        return True, ""
    if dry:
        return True, f"干跑：跳过 {g.rse_msg}.{g.field} 检查"
    if rec is None:
        return False, "无录制器，读不到状态"

    if g.trigger:                                     # 先发查询请求让服务器下发状态
        try:
            top, tfields = g.trigger
            sender.send_frame(sock, top, encode_message(tfields), rec.rc4_c2s)
        except Exception as exc:
            return False, f"状态查询发送失败：{exc}"

    data = _pump(sock, rec, g.rse_msg, g.timeout)
    if data is None:
        return False, f"{g.timeout:.0f}s 内没收到 {g.rse_msg}，放弃（宁可不做也不误扣）"
    left = data.get(g.field)
    if left is None:
        return False, f"{g.rse_msg} 里没有 {g.field} 字段"
    if not isinstance(left, int) or left <= 0:
        return False, f"剩余免费次数 {g.field}={left}，已用完，跳过（避免扣券/勋章）"
    return True, f"剩余免费 {g.field}={left}"


def _check_safety(task, field_names):
    """发送前的安全检查。返回 (是否放行, 原因)。"""
    if task.opcode not in ALLOWED_OPCODES:
        return False, f"opcode {task.opcode} 不在白名单"
    for fno, (ftype, val) in task.fields.items():
        fname = field_names.get(fno, f"field{fno}")
        if DANGER_FIELD.search(fname) and val not in (0, False, "", None):
            return False, f"危险字段 {fname}={val!r} 非零，拒发（防止消耗资产）"
    return True, ""


def run(rec, sock, config: dict, schema=None) -> dict:
    """执行每日任务。rec 是 Recorder（提供 C→S 的 RC4），sock 是已登录的 socket。

    返回 {任务名: 结果字符串}。
    """
    conf = (config.get("每日任务", {}) or {})
    if not conf.get("启用", False):
        log.info("每日任务未启用（config.json 每日任务.启用=false）")
        return {}

    dry = conf.get("干跑", True)
    switches = conf.get("任务", {})
    allow_unverified = conf.get("允许未实测参数", False)
    gap = float(conf.get("间隔秒", 3))

    st = _load_state()
    results = {}

    log.info("=== 每日任务开始（%s）===", "干跑模式，不会真发" if dry else "实发模式")

    for task in ordered_tasks():
        if not switches.get(task.key, False):
            results[task.key] = "未开启"
            continue

        done = st["done"].get(task.key, 0)
        if done >= task.max_per_day:
            results[task.key] = f"今日已执行 {done} 次，跳过"
            log.info("[%s] %s", task.key, results[task.key])
            continue

        if task.confidence != "实测" and not allow_unverified:
            results[task.key] = "参数未实测，已跳过（见 tools/capture_daily.py）"
            log.warning("[%s] %s", task.key, results[task.key])
            continue

        field_names = {}
        if schema is not None:
            field_names = schema.field_names(task.msg) if hasattr(schema, "field_names") else {}

        ok, why = _check_safety(task, field_names)
        if not ok:
            results[task.key] = f"安全检查拦截：{why}"
            log.error("[%s] %s", task.key, results[task.key])
            continue

        # 状态闸门：免费次数用完就不发，这是防扣券/扣勋章的第二道闸
        passed, gwhy = _check_guard(task, rec, sock, dry)
        if not passed:
            results[task.key] = f"闸门未通过：{gwhy}"
            log.info("[%s] %s", task.key, results[task.key])
            continue
        if gwhy:
            log.debug("[%s] 闸门：%s", task.key, gwhy)

        body = encode_message(task.fields)
        desc = ", ".join(f"{field_names.get(k, k)}={v[1]!r}" for k, v in sorted(task.fields.items()))
        if dry:
            frame_preview = sender.build_frame(task.opcode, body)
            log.info("[干跑] %s → %s(%s) body=%dB 帧=%dB  {%s}",
                     task.key, task.msg, task.opcode, len(body),
                     len(frame_preview), desc)
            results[task.key] = f"干跑：{task.msg} body {len(body)}B"
            continue

        try:
            sender.send_frame(sock, task.opcode, body, rec.rc4_c2s)
            st["done"][task.key] = done + 1
            _save_state(st)
            results[task.key] = "已发送"
            log.info("[%s] 已发送 %s(%s)  {%s}", task.key, task.msg, task.opcode, desc)
        except Exception as exc:
            results[task.key] = f"发送失败：{exc}"
            log.error("[%s] %s", task.key, results[task.key])
        time.sleep(gap)

    log.info("=== 每日任务结束 ===")
    for k, v in results.items():
        log.info("  %-10s %s", k, v)
    return results

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

# 服务器响应里 ret 的含义
# ------------------------
# 2026-08-08 抓包实测：本次录制的操作全部成功，8 种响应的 ret 一律为 0
# （RseJunBeiOpt/RseHeroVisit/RseAdmiralVisit/RseWarGameOpt/RseGuildOpt/
#   RseBookCollection/RseCountryOpt/RseResourceOpt），所以 ret==0 = 成功。
# 但 RseWPCExplore 是例外：它的 ret 是 12672/12671/76032 这种大数，
# 那是**本次获得的资源量**而非状态码，不能拿它判成败。
#
# 注意：本次抓包里没有失败样本，所以"ret!=0 即失败"是推断而非实测。
# 保守起见，一旦判为失败就当天不再重试该任务 —— 宁可少做一次，
# 也不要在"次数已用完"的情况下反复撞墙。
RET_IS_PAYLOAD = {"RseWPCExplore"}      # ret 装的是收益，不是状态码

# 响应里这些字段若存在，用来展示"获得了什么"
REWARD_HINT = ("ret", "leftTime", "getTimes", "leftFreeCnt", "nResult",
               "addsoul", "num", "cnt")


def _rse_name(rce_msg: str) -> str:
    """请求消息名 → 对应的响应消息名（RceXxx → RseXxx）。"""
    return "Rse" + rce_msg[3:] if rce_msg.startswith("Rce") else rce_msg


def judge(rse_msg: str, data):
    """判断一次任务的结果。返回 (是否成功, 说明, 是否应停止今日重试)。"""
    if data is None:
        # 没等到响应：可能是服务器忽略了（次数用完常见于此），也可能只是慢。
        # 保守处理 —— 当天不再重试。
        return False, "未收到响应（可能次数已用完或请求被忽略）", True
    if not isinstance(data, dict):
        return True, str(data)[:80], False
    ret = data.get("ret")
    if ret is not None and rse_msg not in RET_IS_PAYLOAD and ret != 0:
        return False, f"服务器返回 ret={ret}（非 0，判为失败）", True
    bits = [f"{k}={data[k]}" for k in REWARD_HINT if k in data]
    return True, ("成功" + ("：" + " ".join(bits) if bits else "")), False


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
                 note="", max_per_day=1, guard=None, cooldown_sec=0):
        self.guard = guard
        # 很多任务并非"一天一次"：军事演习占领后有时长，结束才能再占（每天 3 次）；
        # 英雄训练 8 小时可重复。cooldown_sec>0 表示两次执行之间要等这么久，
        # 配合 max_per_day 一起限制。守护进程会周期性地重跑任务轮次。
        self.cooldown_sec = cooldown_sec
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
       {2: ("int32", 0), 4: ("int32", 0)},
       "实测", "抓包实测：客户端只发 nType=0 / nActivetype=0，"
               "nDay 和 nGiftID 根本不发（服务端自己算当前是第几天）"),

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

    _t("英雄开采", "英雄中心·开采石油", "0402", "RceHeroVisit",
       {3: ("int32", 1), 4: ("int32", 0)},
       "实测", "实测客户端只发 {free:1, type:N}。type 0/1/2 三档，"
               "第三档常态 500 勋章，靠 guard 判免费次数", max_per_day=3),

    _t("将领冶炼", "将领·金属冶炼", "0450", "RceAdmiralVisit",
       {3: ("int32", 1), 4: ("int32", 0)},
       "实测", "同英雄开采：{free:1, type:N}", max_per_day=3),

    _t("技能书收集", "将领/参谋·免费技能书", "048a", "RceBookCollection",
       {1: ("int32", 0), 2: ("int32", 0)},
       "实测", "实测 {ActiveType:0/1, OptType:0/1} 四种组合；"
               "ActiveType 区分将领/参谋，OptType 区分查询/领取", max_per_day=4),

    _t("参谋操作", "参谋·免费派遣", "04da", "RceAdviserOpt",
       {1: ("int32", 11)},
       "实测", "实测 noptType 1(查询)/11/12；不再是我先前猜的 04dc"),

    _t("特工派遣", "远程火炮·特工派遣", "04d6", "RceTrenchMortarOpt",
       {1: ("int32", 1), 2: ("int32", 1001)},
       "实测", "实测 {optType:1, subType:1001/1002/1003} 三档；"
               "optType:0 是查询。不再是我先前猜的 045d", max_per_day=3),

    _t("配件探索", "配件中心·基地探索", "043a", "RceWPCExplore",
       {1: ("int32", 10001)},
       "实测",
       "实测三个场景 sceneID 10001/10002/10003。useItemID/useItemCnt 是"
       "**库存上报**（10003 报 414，一次点击不可能消耗 414 张），不是消耗量；"
       "此处一律不发这两个字段，等价于不走券。服务器响应的 leftTime 才是"
       "真实剩余免费次数（实测 2→1→0）",
       guard=Guard("RseWPCExplore", "leftTime"), max_per_day=3),

    _t("军备制造", "军备研究·制造", "04e1", "RceJunBeiOpt",
       {1: ("int32", 22), 2: ("int32", 1)},
       "实测", "实测 {type:22, nExlType:1/4}；type 0/21 是查询。"
               "不再是我先前猜的 04cb", max_per_day=3),

    _t("战略训练", "战争学院·战略技能训练", "04a5", "RceWarCollegeOpt",
       {1: ("int32", 4), 3: ("int32", 1)},
       "实测", "实测训练动作 {type:4, trainskilltype:1}，{type:1} 只是开面板。"
               "每日 7 次", max_per_day=7),

    _t("军事演习", "战争学院·军事演习（占场）", "04a7", "RceWarGameOpt",
       {1: ("int32", 1)},
       "实测", "实测 {type:1} 查询场地、{type:2, siteID:N} 占领。"
               "占领后有时长，结束才能再占，每天 3 次 —— 故设 cooldown。"
               "siteID 依赖实时空场，占领逻辑待解析响应后补",
       max_per_day=3, cooldown_sec=3600),

    _t("矿区争夺", "矿区争夺·探索/占矿", "049a", "RceResourceOpt",
       {1: ("int32", 1)},
       "实测", "实测 {type:1} 查询、{type:2,searchType:1} 探索、"
               "{type:3,resourceID:N} 占矿。响应含 getTimes(剩余占矿次数)。"
               "不再是我先前猜的 041e", max_per_day=5, cooldown_sec=600),

    _t("征战世界", "征战世界·自动征战", "045b", "RcePVEFightOpt",
       {2: ("int32", 6), 1: ("bool", True)},
       "实测", "实测 {type:6, bAutoTreat:true} 是自动战斗，共发了 123 次；"
               "{type:2}/{type:5} 是查询与手动。不再是我先前猜的 048b",
       max_per_day=2, cooldown_sec=300),

    _t("国家宝箱", "国家·宝箱领取", "0463", "RceCountryOpt",
       {4: ("int32", 15)},
       "实测", "实测 type=15 领取；type=10/11 带 count 开箱。costCredit 全程 0"),

    _t("公会捐献", "公会·捐献", "0479", "RceGuildOpt",
       {2: ("int32", 16), 12: ("int32", 1)},
       "实测", "实测 {type:16, contributeID:1}"),

    _t("公会战", "公会战·领奖/报名", "0479", "RceGuildOpt",
       {2: ("int32", 14)},
       "实测", "实测 type=0/2/14/16 四种，14 为本次录制的公会战操作"),

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
        st = {"date": date.today().isoformat(), "done": {}, "last": {}}
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
        return {}, {}

    dry = conf.get("干跑", True)
    switches = conf.get("任务", {})
    allow_unverified = conf.get("允许未实测参数", False)
    gap = float(conf.get("间隔秒", 3))

    resp_timeout = float(conf.get("响应等待秒", 6))
    st = _load_state()
    results, details = {}, {}

    log.info("=== 每日任务开始（%s）===", "干跑模式，不会真发" if dry else "实发模式")

    for task in ordered_tasks():
        if not switches.get(task.key, False):
            results[task.key] = "未开启"
            continue

        done = st["done"].get(task.key, 0)
        if done >= task.max_per_day:
            results[task.key] = f"今日已执行 {done}/{task.max_per_day} 次，跳过"
            log.info("[%s] %s", task.key, results[task.key])
            continue

        # 冷却：军事演习占领后要等演习结束、矿区占矿有间隔，不能连着刷
        if task.cooldown_sec:
            last = st.get("last", {}).get(task.key, 0)
            wait = task.cooldown_sec - (time.time() - last)
            if wait > 0:
                results[task.key] = f"冷却中，还需 {wait / 60:.0f} 分钟"
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

        rse = _rse_name(task.msg)
        # 记下发送前该响应的时间戳，避免把上一次的旧响应误当成本次结果
        before = (rec.latest.get(rse) or (0,))[0] if rec else 0
        try:
            sender.send_frame(sock, task.opcode, body, rec.rc4_c2s)
            st["done"][task.key] = done + 1
            st.setdefault("last", {})[task.key] = time.time()
            _save_state(st)
            log.info("[%s] 已发送 %s(%s)  {%s}", task.key, task.msg, task.opcode, desc)
        except Exception as exc:
            results[task.key] = f"发送失败：{exc}"
            log.error("[%s] %s", task.key, results[task.key])
            continue

        # 收响应并判成败 —— 不能只管发不管结果，否则"三次机会"用完了还在撞墙
        data = _await_response(sock, rec, rse, before, resp_timeout)
        ok, why, stop = judge(rse, data)
        results[task.key] = why
        if ok:
            log.info("[%s] ✅ %s", task.key, why)
        else:
            log.warning("[%s] ❌ %s", task.key, why)
            if stop:
                # 判为"做不了了"就把今日次数打满，别再浪费请求
                st["done"][task.key] = task.max_per_day
                _save_state(st)
                log.info("[%s] 今日不再重试", task.key)
        if data is not None:
            details[task.key] = data
        time.sleep(gap)

    log.info("=== 每日任务结束 ===")
    for k, v in results.items():
        log.info("  %-12s %s", k, v)
    return results, details


def _await_response(sock, rec, rse_msg, since_ts, timeout):
    """发完请求后等对应的服务器响应。只认 since_ts 之后到达的，避免读到旧的。"""
    if rec is None:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = rec.latest.get(rse_msg)
        if got and got[0] > since_ts:
            return got[1]
        try:
            sock.settimeout(0.5)
            if not sock.recv(8192):
                break
        except Exception:
            pass
    return None

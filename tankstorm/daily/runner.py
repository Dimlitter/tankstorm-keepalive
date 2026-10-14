# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""每日任务执行器。

一个任务做一次的流水线：

    前置请求 → 闸门 → 解析占位符 → 安全检查 → 动作 → 判成败 → 后续步骤

一轮里把当天的次数做完，而不是做一次就走：freeVisitCnt=[3,1,1] 是三个档位
各自的免费次数，三档都要领；战略训练更是一天 7 次同样的包。
"""

import time

from .. import schema as _schema
from .. import wire
from ..log import get_logger
from . import state
from .checks import (check_gate, check_safety, echo_want, field_names,
                     read_counts)
from .judge import judge, show
from .model import rse_name
from .tasks import ALLOWED_OPCODES, ordered_tasks

log = get_logger()

# 连续多少轮收不到响应就放弃该任务（当天）。
# 设 3 是为了容忍偶发的网络抖动/响应慢，又不至于无限期地空发。
MAX_MISS = 3


def run(rec, sock, config: dict, schema=None, beat=None):
    """执行每日任务。rec 是 Recorder（提供 C→S 的 RC4），sock 是已登录的 socket。

    返回 ({任务名: 结果字符串}, {任务名: 最后一条响应})。
    beat 是心跳器（wire.Beater），任务执行期间在等待处顺手发。
    """
    # schema 默认用项目自带的那份。
    #
    # 2026-08-13 定位：两个调用点（保活带跑和 --daily）都是 run(rec, sock, config)，
    # schema 一直是 None，于是 field_names() 永远返回 {}，echo_want() 拿不到
    # 字段名、造不出判据，**所有任务都失去了"前置回包 vs 动作回包"的区分能力**
    # —— 谁先到算谁。实盘后果：技能书发出 {OptType:1} 后 2 毫秒就"成功"，
    # 其实读到的是前置 {OptType:0} 的回包；真正的动作回包 4 秒后才到，ret=1。
    if schema is None:
        schema = _schema
    with wire.heartbeat(beat):
        return _run(rec, sock, config, schema)


def _skip_reason(task, st, switches, allow_unverified):
    """进任务之前的检查。返回 (说明, 日志级别)；可以做就返回 None。"""
    if not switches.get(task.key, False):
        return "未开启", None

    done = st["done"].get(task.key, 0)
    if done >= task.max_per_day:
        return f"今日已执行 {done}/{task.max_per_day} 次，跳过", "info"

    # 冷却之一：服务器明确告诉我们"到这个时刻才能再做"（占领结束时刻等）。
    # 这个是读来的，优先于任何写死的秒数。
    until = st.get("until", {}).get(task.key, 0)
    if until and time.time() < until:
        wait = until - time.time()
        return f"占用中，服务器给的结束时刻还有 {wait / 60:.0f} 分钟", "info"

    # 冷却之二：写死的秒数，只在服务器没给时刻时才用（如英雄培养的 8 小时）
    if task.cooldown_sec:
        last = st.get("last", {}).get(task.key, 0)
        wait = task.cooldown_sec - (time.time() - last)
        if wait > 0:
            return f"冷却中，还需 {wait / 60:.0f} 分钟", "info"

    if task.confidence != "实测" and not allow_unverified:
        return "参数未实测，已跳过（见 tools/capture_daily.py）", "warning"
    return None


def _run(rec, sock, config, schema):
    conf = (config.get("每日任务", {}) or {})
    if not conf.get("启用", False):
        log.info("每日任务未启用（config.json 每日任务.启用=false）")
        return {}, {}

    switches = conf.get("任务", {})
    allow_unverified = conf.get("允许未实测参数", False)
    gap = float(conf.get("间隔秒", 3))
    resp_timeout = float(conf.get("响应等待秒", 6))
    st = state.load()
    results, details = {}, {}

    log.info("=== 每日任务开始（实发）===")

    for task in ordered_tasks():
        skip = _skip_reason(task, st, switches, allow_unverified)
        if skip:
            results[task.key], level = skip
            if level:
                getattr(log, level)("[%s] %s", task.key, results[task.key])
            continue

        if task.runner is not None:
            _run_custom(task, rec, sock, config, st, results)
            continue

        ctx = _Attempt(task, sock, rec, st, results, details, schema,
                       resp_timeout, gap)
        ran = 0
        while st["done"].get(task.key, 0) < task.max_per_day:
            if not ctx.once():
                break
            ran += 1
            # 服务器说这东西被占用到某时刻（占了演习场/占了矿），就别接着刷了
            if st.get("until", {}).get(task.key, 0) > time.time():
                break
            # 靠**固定冷却**限流的任务，成功一次就到此为止。
            # 冷却只在进任务前查了一次，循环里不再查，于是英雄培养成功之后
            # 3 秒又发一次，服务端回 error=81（已在培养中）——
            # 报出来像是任务失败，其实第一次已经成功了。2026-08-29 实盘复现。
            if task.cooldown_sec:
                log.info("[%s] 靠 %.0f 小时冷却限流，本轮做完一次即止",
                         task.key, task.cooldown_sec / 3600)
                break
            wire.nap(gap)
        if ran > 1:
            log.info("[%s] 本轮共成功 %d 次（今日 %d/%d）", task.key, ran,
                     st["done"].get(task.key, 0), task.max_per_day)

    log.info("=== 每日任务结束 ===")
    for k, v in results.items():
        log.info("  %-12s %s", k, v)
    return results, details


def _run_custom(task, rec, sock, config, st, results):
    """形状不合流水线的任务交给它自己的执行器，成功就记满当天次数。"""
    try:
        ok, why = task.runner(rec, sock, config)
    except Exception as exc:
        ok, why = False, f"执行异常：{exc}"
        log.exception("[%s] 自定义执行器抛异常", task.key)
    results[task.key] = why
    log.info("[%s] %s %s", task.key, "✅" if ok else "❌", why)
    if ok:
        st["done"][task.key] = task.max_per_day
        state.save(st)


def _bits(task, data):
    """把响应里 task.report 列出的字段拼成一句给人看的话。"""
    if not task.report or not isinstance(data, dict):
        return ""
    return " ".join(f"{f}={show(data[f])}" for f in task.report if f in data)


class _Attempt:
    """把一个流水线任务做一次。once() 返回 True 表示成功且可以接着再做一次。

    返回 False 的情形都不该在本轮重试：闸门拦下、服务器拒绝、没等到响应、
    发送失败。
    """

    def __init__(self, task, sock, rec, st, results, details, schema,
                 resp_timeout, gap):
        self.task = task
        self.sock = sock
        self.rec = rec
        self.st = st
        self.results = results
        self.details = details
        self.schema = schema
        self.resp_timeout = resp_timeout
        self.gap = gap
        self.names = field_names(schema, task.opcode)

    # ---------------------------------------------------------------- 主流程

    def once(self) -> bool:
        task = self.task
        self.done = self.st["done"].get(task.key, 0)
        # 发前置之前先记下消息序号：闸门和 FromResponse 都只认这之后到的响应，
        # 免得把登录爆发期推来的旧数据当成这一轮的回包。
        self.since = self.rec.seq_mark() if self.rec else 0
        self._send_prelude()

        tier, note = None, ""
        if task.gate:
            passed, tier, note = self._gate()
            if not passed:
                return False
        if task.report and not note and task.prelude:
            note = self._prelude_report()

        fields = self._action_fields(tier)
        if fields is None:
            return False

        data = self._send_action(fields)
        if data is False:
            return False

        ok, why, stop = judge(task.rse, data, ignore_left=bool(task.tiers),
                              success_flag=task.success_flag)
        # 动作回包里若也带着这些字段，用它 —— 那是**做完之后**的状态。
        # 否则会出现"✅ 成功（面板：bSignIn=False）"这种自相矛盾的话：
        # False 是签到**前**闸门读到的值，签完已经是 True 了。
        note = _bits(task, data) or note
        if note:
            why = f"{why}（{note}）"
        self.results[task.key] = why
        if ok:
            ok = self._on_success(why, data, stop)
        else:
            self._on_failure(why, data, stop)
        if data is not None:
            self.details[task.key] = data
        return ok

    def _send_prelude(self):
        """前置请求：真实客户端每个动作前都会先开面板/查询，服务端据此建上下文。

        少了这步，动作发出去参数再对也不生效 —— 这是 2026-08-09 定位到的根因。
        前置不判成败，发失败也只记一条警告。
        """
        task = self.task
        for pop, pfields in task.prelude:
            try:
                wire.send(self.sock, self.rec, pop, pfields)
                log.debug("[%s] 前置 %s", task.key, pop)
                wire.nap(0.4)
            except Exception as exc:
                log.warning("[%s] 前置请求 %s 失败: %s", task.key, pop, exc)
        if task.prelude:
            wire.nap(0.6)           # 给服务端一点时间把面板数据推回来

    def _prelude_want(self):
        """前置最后一条请求的区分字段判据（没有前置或造不出时为 None）。"""
        if not self.task.prelude:
            return None
        pop, pfields = self.task.prelude[-1]
        return echo_want(pfields, field_names(self.schema, pop))

    def _gate(self):
        """读前置响应里的剩余免费次数。返回 (是否放行, 档位下标, 面板说明)。

        客户端正是读这个决定走免费档还是扣券/扣勋章档，读不到就不发。
        """
        task, gate = self.task, self.task.gate
        # 只认真正带着次数字段的那条 —— 同名消息可能连来好几条。
        # 还要认前置自己的区分字段：技能书将领({ActiveType:0})和参谋
        # ({ActiveType:1}) 是同一条消息、各有各的计时，光看"有没有 field4"
        # 会把对方的那条读进来。
        pre_want = self._prelude_want()

        def has_counts(d):
            return read_counts(d, gate.field) is not None

        def strict(d):
            return has_counts(d) and (pre_want is None or pre_want(d))

        # 严格判据挑不到就退回"只要带次数字段就行"：面板回包不一定回显
        # 请求里的 type，硬要求会把唯一带次数的那条否掉。
        gdata = wire.await_response(self.sock, self.rec, gate.rse_msg,
                                    self.since, gate.timeout,
                                    want=strict, relaxed=has_counts)
        passed, why, exhausted, tier = check_gate(gate, gdata, task.tiers)
        if not passed:
            if self.done == 0 or "已用完" not in why:
                self.results[task.key] = f"闸门拦截：{why}"
            log.info("[%s] 闸门拦截：%s", task.key, why)
            if exhausted:
                if task.cooldown_until:
                    # 靠冷却限流的任务，"次数为 0"只说明**现在**不能做，
                    # 不等于今天做不了了。技能书是 24 小时一轮，
                    # 上午拦下、下午到点还该再领 —— 这时候记满当天次数，
                    # 当天就再也不会重试。改为把服务端给的下次可用时刻记下来。
                    self._note_until(gdata)
                else:
                    # 确定没免费次数了，今天别再来
                    self.st["done"][task.key] = task.max_per_day
                state.save(self.st)
            return False, None, ""
        log.info("[%s] 闸门放行：%s", task.key, why)
        # 前置响应里的看点（排名/积分/剩余次数）记下来，最后并进结果一起推送
        note = _bits(task, gdata)
        if note:
            log.info("[%s] 面板：%s", task.key, note)
        return True, tier, note

    def _prelude_report(self):
        """没有闸门的任务也要能报面板（公会战就靠它把 dayHasPK 读出来）。"""
        pop, _ = self.task.prelude[-1]
        pname = (getattr(self.schema, "SCHEMA", {}) or {}).get(pop, {}).get("name")
        if not pname:
            return ""
        pdata = wire.await_response(self.sock, self.rec, rse_name(pname),
                                    self.since, 3.0, want=self._prelude_want())
        note = _bits(self.task, pdata)
        if note:
            log.info("[%s] 面板：%s", self.task.key, note)
        return note

    def _action_fields(self, tier):
        """解析占位符、换上档位、过安全检查。返回要发的字段表，不能发就返回 None。

        占位符现在才解析：像"挑一个空演习场""七天乐第几天"这类值，必须等前置
        请求的响应回来才算得出。解析在安全检查之前，所以检查看到的就是真正
        要发出去的内容。
        """
        task = self.task
        fields = {}
        for fno, (ftype, val) in task.fields.items():
            if hasattr(val, "resolve"):
                val = val.resolve(self.rec, self.sock, self.since)
                if val is None:
                    # 多数时候这不是故障，而是"今天这份已经领过了"之类的正常状态
                    why = (f"{task.fields[fno][1]!r} 没有可做的（已领过或暂时没有），"
                           "跳过")
                    if self.done == 0:
                        self.results[task.key] = why
                    log.info("[%s] %s", task.key, why)
                    return None
            fields[fno] = (ftype, val)

        # 档位：把档位字段换成本次要打的那一档（低级/中级/高级）
        if task.tiers and tier is not None:
            fields[task.tiers.field] = ("int32", task.tiers.values[tier])

        ok, why = check_safety(task, self.names, fields, ALLOWED_OPCODES)
        if not ok:
            self.results[task.key] = f"安全检查拦截：{why}"
            log.error("[%s] %s", task.key, self.results[task.key])
            return None

        # 前置和动作同一个 opcode 时，响应也同名，必须靠区分字段（OptType/type…）
        # 才能认出哪条是动作的回包。造不出判据就**不发** —— 发了也分不清结果，
        # 只会把前置的 ret=0 当成"成功"。2026-08-13 实盘就是这么假成功的。
        if any(pop == task.opcode for pop, _ in task.prelude) \
                and echo_want(fields, self.names) is None:
            seen = "、".join(f"{n}={self.names.get(n, '?')}"
                            for n in sorted(fields)) or "（一个字段都没解出来）"
            self.results[task.key] = (
                f"认不出动作回包（前置与动作同 opcode，而请求里的"
                f"字段 {seen} 没有一个在 DISCRIMINATORS 里），这一轮不做")
            log.error("[%s] %s", task.key, self.results[task.key])
            return None
        return fields

    def _desc(self, fields):
        return ", ".join(f"{self.names.get(k, k)}={v[1]!r}"
                         for k, v in sorted(fields.items()))

    def _send_action(self, fields):
        """发动作并等回包。返回回包（没等到是 None）；发送失败返回 False。"""
        task = self.task
        try:
            before = wire.send(self.sock, self.rec, task.opcode, fields)
        except Exception as exc:
            self.results[task.key] = f"发送失败：{exc}"
            log.error("[%s] %s", task.key, self.results[task.key])
            return False
        # 注意：这里**不能**立刻给 done 计数。之前在这里就 +1，导致失败的尝试
        # 也在烧每日配额 —— 跑几次失败之后所有任务都显示"今日已执行 N/N，跳过"，
        # 明明一次都没成。计数放到判定成功之后；冷却仍按发送计时。
        self.st.setdefault("last", {})[task.key] = time.time()
        state.save(self.st)
        log.info("[%s] 已发送 %s(%s)  {%s}", task.key, task.msg, task.opcode,
                 self._desc(fields))
        # 必须按区分字段挑出**本次动作**的回包，别把前置的回包当成结果。
        return wire.await_response(self.sock, self.rec, task.rse, before,
                                   self.resp_timeout,
                                   want=echo_want(fields, self.names))

    # ---------------------------------------------------------------- 结算

    def _on_success(self, why, data, stop) -> bool:
        task, st = self.task, self.st
        # 只有**确认成功**才算用掉一次每日额度
        st["done"][task.key] = self.done + 1
        st.setdefault("miss", {}).pop(task.key, None)   # 成功就清零重试计数
        state.save(st)
        log.info("[%s] ✅ %s（今日 %d/%d）", task.key, why,
                 self.done + 1, task.max_per_day)
        extra, last_data, fu_ok = self._followup(data)
        if extra:
            self.results[task.key] = why + "；" + "；".join(extra)
        if fu_ok is False:
            # 有些任务的"动作"其实只是开面板（国家宝箱的 type=15 恒回 ret=0），
            # 真正干活的是后续步骤。后续被拒还把当天次数记满，就等于这一天
            # 再也不会重试了 —— 国家宝箱要早上六点后才能领，凌晨那轮必然被拒，
            # 记满之后当天就永远领不到。所以后续失败时退回这一次计数。
            st["done"][task.key] = self.done
            state.save(st)
            log.info("[%s] 后续步骤未成，本次不计入当天次数，稍后可再试", task.key)
            return False
        # 记下服务器给的"下次可用时刻"。矿区的 resourceEndTime 只在占矿
        # （后续步骤）的回包里，所以后续的响应也要看。
        self._note_until(last_data or data)
        if stop:
            # 成功了，但响应说剩余次数已归零 —— 别再循环了
            st["done"][task.key] = task.max_per_day
            state.save(st)
            return False
        return True

    def _on_failure(self, why, data, stop):
        task, st = self.task, self.st
        log.warning("[%s] ❌ %s", task.key, why)
        if stop:
            st.setdefault("miss", {}).pop(task.key, None)
            if task.cooldown_sec or task.cooldown_until:
                # 靠冷却限流的任务，被拒多半只是"还没到时候"，不是"今天没了"。
                # 英雄培养就是这样：上一轮 8 小时没走完，服务器回 ret=81，
                # 这时候把当天次数打满是错的 —— 等冷却过去还该再试。
                log.info("[%s] 本次被拒，等冷却过去再试", task.key)
            else:
                # 服务器明确拒绝，且没有冷却依据，今天别再撞了
                st["done"][task.key] = task.max_per_day
                log.info("[%s] 今日不再重试", task.key)
            state.save(st)
        elif data is None:
            # 只是没等到响应。允许后面几轮再试，但不能无限试下去，
            # 连续 MAX_MISS 轮都收不到就认了。
            miss = st.setdefault("miss", {}).get(task.key, 0) + 1
            st["miss"][task.key] = miss
            if miss >= MAX_MISS:
                st["done"][task.key] = task.max_per_day
                log.info("[%s] 连续 %d 轮收不到响应，今日不再重试",
                         task.key, miss)
            else:
                log.info("[%s] 第 %d/%d 次没等到响应，下一轮还会再试",
                         task.key, miss, MAX_MISS)
            state.save(st)

    def _note_until(self, data):
        note_until(self.task, self.st, self.results, data)

    def _followup(self, data):
        """动作成功后按响应继续发（占矿、把达标的奖励档位领完）。

        返回 (每一轮的说明列表, 最后一条响应, 成败)。第二项用来读"下次可用时刻"
        —— 矿区的 resourceEndTime 只出现在占矿那一步的回包里。
        第三项：True=至少发出去一轮且都成了，False=发出去但被拒，
        None=压根没有后续步骤可发（这时不该影响主动作的判定）。
        任何一轮失败或没东西可发就停 —— 后续步骤本来就是"有就做，没有就算"，
        不该把整条任务判成失败。
        """
        task, fu = self.task, self.task.followup
        if fu is None or data is None:
            return [], None, None
        out, last, seen, verdict = [], data, set(), None
        for _ in range(fu.max_rounds):
            try:
                nxt = fu.build(last)
            except Exception as exc:
                log.debug("[%s] 后续步骤构造失败: %s", task.key, exc)
                break
            if not nxt:
                break
            # 同一包别发第二遍。服务器对领奖的即时回包里状态还没更新
            # （实测领了 giftID=10，回包里它仍标着"未领"），照着算就会再领一次。
            sig = tuple(sorted((k, v[1]) for k, v in nxt.items()))
            if sig in seen:
                log.debug("[%s] 后续步骤重复（%s），停", task.key, sig)
                break
            seen.add(sig)
            ok, why = check_safety(task, self.names, nxt, ALLOWED_OPCODES)
            if not ok:
                log.error("[%s] 后续步骤被安全检查拦截：%s", task.key, why)
                out.append(f"{fu.desc}: 被安全检查拦截（{why}）")
                verdict = False   # 拦下了就是没做成，别让主动作顶着"成功"过关
                break
            try:
                before = wire.send(self.sock, self.rec, fu.opcode, nxt)
                log.info("[%s] 后续 %s(%s) {%s}", task.key, fu.desc, fu.opcode,
                         self._desc(nxt))
            except Exception as exc:
                log.warning("[%s] 后续步骤发送失败: %s", task.key, exc)
                break
            wire.nap(self.gap)
            last = wire.await_response(self.sock, self.rec, task.rse, before,
                                       self.resp_timeout)
            ok, why, _stop = judge(task.rse, last)
            log.info("[%s] 后续结果：%s %s", task.key, "✅" if ok else "❌", why)
            out.append(f"{fu.desc}: {why}")
            verdict = bool(ok)
            if not ok or last is None:
                break
        return out, last, verdict


def note_until(task, st, results, data):
    """把服务器给的"下次可用时刻"记进 st["until"]，供下一轮判冷却，并在结果里补一句。

    只认落在未来 30 天以内的 unix 秒（既不能是过去，也不能远到离谱），
    免得把某个恰好是大整数的字段错当成时间戳。
    """
    if not task.cooldown_until or not isinstance(data, dict):
        return
    ts = wire.read_path(data, task.cooldown_until)
    if isinstance(ts, list):
        ts = max((x for x in ts if isinstance(x, int)), default=None)
    now = time.time()
    if not wire.is_int(ts):
        return
    if not (now < ts < now + 30 * 86400):
        return
    st.setdefault("until", {})[task.key] = ts
    state.save(st)
    mins = (ts - now) / 60
    log.info("[%s] 服务器给的下次可用时刻：%s（还有 %.0f 分钟）", task.key,
             time.strftime("%m-%d %H:%M", time.localtime(ts)), mins)
    results[task.key] = results.get(task.key, "") + f"；占用至 {mins:.0f} 分钟后"

# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""一次性任务：连一次游戏、跑完、断开退出。

    run_daily_once         main.py --daily
    run_country_war_once   main.py --country-war N

和保活的区别是不常驻；但任务执行期间照样发心跳 —— 每日任务一跑就是
好几分钟，国战打几百次动辄十几分钟，心跳周期只有 10 秒。
"""

from . import country_war, daily, notify, session, shop, wire
from .log import get_logger

log = get_logger()

# 结果文案里出现这些词说明这一项根本没动手（今日额度已用完、冷却中、未开启），
# 不该算进成败统计，否则一份"全是跳过"的日志会显示成"未成 14"。
_IDLE = ("未开启", "冷却中", "未实测", "跳过")


def daily_round(rec, sock, spec, ctx, config) -> dict:
    """在已登录的连接上跑一轮每日任务，推送成果。返回 {任务名: 结果}。"""
    beater = session.make_beater(sock, spec, ctx, config)
    results, details = daily.run(rec, sock, config, beat=beater)
    log.info("任务执行期间共发心跳 %d 次", beater.count)
    push_daily_summary(config, results, details)
    return results


def push_daily_summary(config: dict, results: dict, details: dict) -> None:
    """把每日任务成果推到 PushPlus，并在日志里打一份对照表。

    只有真正执行过的才推 —— 全是"未开启/冷却中"的轮次不值得打扰。
    成功与否看结果文案是不是以"成功"开头，所以执行器（runner）成功时
    返回的说明必须以"成功"开头。
    """
    acted = {k: v for k, v in results.items()
             if v and not any(s in v for s in _IDLE)}
    if not acted:
        return

    okn = sum(1 for v in acted.values() if v.startswith("成功"))
    bad = len(acted) - okn

    log.info("―― 每日任务成果 ―― 成功 %d，未成 %d", okn, bad)
    rows = []
    for k, v in acted.items():
        icon = "✅" if v.startswith("成功") else "❌"
        log.info("  %s %-12s %s", icon, k, v)
        extra = ""
        d = details.get(k)
        if isinstance(d, dict):
            kv = [f"{a}={d[a]}" for a in daily.REWARD_HINT if a in d]
            if kv:
                extra = f"<br><span style='color:#888'>{' '.join(kv)}</span>"
        rows.append(f"<tr><td>{icon}</td><td><b>{k}</b></td>"
                    f"<td>{v}{extra}</td></tr>")

    title = f"坦克风暴每日任务：成功 {okn}" + (f"，未成 {bad}" if bad else "")
    html = ("<p>本轮共执行 %d 项</p><table border='1' cellpadding='6' "
            "style='border-collapse:collapse;font-size:14px'>%s</table>"
            % (len(acted), "".join(rows)))
    notify.send(config, title, html, template="html")


def run_daily_once(qq, config: dict) -> int:
    """连一次游戏、跑一轮每日任务、断开退出。有任务失败或被闸门拦下时退出码为 1。"""
    def work(rec, sock, spec, ctx):
        results = daily_round(rec, sock, spec, ctx, config)
        failed = sum(1 for v in results.values() if "失败" in v or "拦截" in v)
        return 1 if failed else 0

    return session.run_once(qq, config, work)


def run_country_war_once(qq, config: dict, rounds: int) -> int:
    """连一次游戏、自动打 N 次摩多军团、断开退出。"""
    def work(rec, sock, spec, ctx):
        beater = session.make_beater(sock, spec, ctx, config)
        with wire.heartbeat(beater):
            # 支援兵是国战的消耗品，开打之前先按配置补货（默认关闭，开了才买）。
            # 放在这里而不是打完之后：库存不够的话这一轮就打不动了。
            if (config.get("功勋商城", {}) or {}).get("自动补支援兵"):
                ok, why = shop.daily_restock(rec, sock, config)
                log.info("[国战] 开打前补支援兵：%s %s", "✅" if ok else "❌", why)
            out = country_war.run(rec, sock, config, rounds=rounds)
        log.info("―― 国战成果 ―― 扫荡 %d 次，攻击 %d 次，召唤 %d 次，战功 +%s",
                 out["扫荡"], out["攻击"], out["召唤"], out["战功"])
        log.info("   剩余行动力 %s，今日攻击次数 %s；结束原因：%s",
                 out.get("剩余行动力"), out.get("今日攻击次数"), out["停止原因"])
        log.info("   任务执行期间共发心跳 %d 次", beater.count)
        notify.send(config, "坦克风暴：国战自动战斗完成",
                    f"扫荡 {out['扫荡']} 次，攻击 {out['攻击']} 次，"
                    f"战功 +{out['战功']}，"
                    f"剩余行动力 {out.get('剩余行动力')}。<br>"
                    f"结束原因：{out['停止原因']}")
        return 0 if (out["扫荡"] + out["攻击"]) else 1

    return session.run_once(qq, config, work)

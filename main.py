# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""坦克风暴（QQ空间 appid 100616028）—— 命令行入口。

这个脚本干**两件互相独立**的事，别把它们混在一起：

  保活   `--keepalive`   常驻，连着游戏 socket 定时发心跳，防止被踢下线。
                         唯一职责就是别掉线，跑几天几周不停。
  每日   `--daily`       跑一轮每日任务然后退出。一次性批处理。

分开的理由：保活断线会自动重连，如果每日任务挂在里面，每次重连都要重跑一轮；
而且任务出错会牵连保活这个更重要的进程。要"连上顺带领一轮"就显式写
`--keepalive --daily`。

常用：
  python main.py --login       扫码登录（cookie 存 cookies.json，之后自动续期）
  python main.py --check       验证登录态，打印 uid/sid/level
  python main.py --keepalive   保活常驻
  python main.py --daily       跑一轮每日任务
  python main.py --list        列出每日任务及今日进度
  python main.py --reset       清空今日任务计数
  python main.py --country-war 10   单独跑国战：自动打摩多军团 10 次
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tankstorm import daily, jobs, qzone, session, settings  # noqa: E402
from tankstorm import socket_keepalive                       # noqa: E402
from tankstorm.log import get_logger                         # noqa: E402
from tankstorm.qq_login import QQSession                     # noqa: E402

log = get_logger()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="坦克风暴：保活守护 + 每日任务 + 国战（三件独立的事）",
        epilog="本程序是自由软件，依 GNU AGPL-3.0 或更新版本授权，不提供任何担保。"
               "源码：https://github.com/Dimlitter/tankstorm-keepalive",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    g1 = parser.add_argument_group("登录")
    g1.add_argument("--login", action="store_true", help="强制重新扫码登录")
    g1.add_argument("--check", action="store_true", help="验证登录态并打印上下文")
    g1.add_argument("--import-device", metavar="文件",
                    help="从浏览器搬一次设备记录以启用推送登录（一次性，"
                         "文件里放浏览器的 Cookie；给 - 表示从标准输入读）")

    g2 = parser.add_argument_group("保活（常驻）")
    g2.add_argument("--keepalive", action="store_true",
                    help="连游戏 socket 定时心跳，防掉线；断线自动重连")

    g3 = parser.add_argument_group("每日任务（一次性）")
    g3.add_argument("--daily", action="store_true",
                    help="跑一轮每日任务后退出；与 --keepalive 同时给则由保活带着跑")
    g3.add_argument("--list", action="store_true", help="列出每日任务及今日进度")
    g3.add_argument("--reset", action="store_true", help="清空今日任务计数")

    g4 = parser.add_argument_group("国战")
    g4.add_argument("--country-war", type=int, metavar="次数", default=0,
                    help="自动扫荡摩多军团 N 次（行动力够就扫荡，不够改普通攻击，"
                         "低于 5 点停手）")

    # 早已废弃的开关，只为不让旧的 cron/计划任务命令行报错
    parser.add_argument("--real", action="store_true", help=argparse.SUPPRESS)
    return parser


def _print_task_list(config: dict) -> None:
    conf = config.get("每日任务", {}) or {}
    sw = conf.get("任务", {})
    st = daily.state.load()
    print(f"\n每日任务（{'已启用' if conf.get('启用') else '未启用'}，实发模式）")
    print(f"{'执行顺序':<4} {'任务':<12} {'opcode':<8} {'消息':<22} "
          f"{'上限':<5} {'今日':<5} {'参数':<6} 开关")
    print("-" * 92)
    for i, t in enumerate(daily.ordered_tasks(), 1):
        done = st.get("done", {}).get(t.key, 0)
        on = "✅开" if sw.get(t.key) else "  关"
        cd = f"/{t.cooldown_sec // 60}分冷却" if t.cooldown_sec else ""
        mark = "实测" if t.confidence == "实测" else "待确认"
        print(f"{i:>4}   {t.key:<12} {t.opcode:<8} {t.msg:<22} "
              f"{str(t.max_per_day) + cd:<5} {done:<5} {mark:<6} {on}")
    print("-" * 92)
    print("『实测』= 参数来自真实抓包，可放心开；『待确认』= 默认跳过，需先抓包核对")
    print("周任务/每日任务由代码强制排最后（前面的操作会推进它们的进度）\n")


def _import_device(qq: QQSession, src: str) -> int:
    """一次性引导：把浏览器的设备记录搬进来，之后推送登录才有 dev_mid_sig 可用。

    pt_fetch_dev_uin 只能给已有的续期，签发不出第一个，所以只能这么来。
    """
    if src == "-":
        text = sys.stdin.read()
    else:
        with open(src, encoding="utf-8") as f:
            text = f.read()
    try:
        got = qq.import_device_cookies(text)
    except (ValueError, KeyError, TypeError) as exc:
        log.error("设备记录解析失败：%s", exc)
        return 1
    if not got:
        log.error("没找到可用的设备记录。至少要有 dev_mid_sig —— "
                  "在浏览器里打开 ptlogin2.qq.com 的页面，从开发者工具里"
                  "把 Cookie 复制出来")
        return 1
    log.info("已导入 %s", "、".join(got))
    log.info("设备状态：%s", qq.device_status())
    return 0


def _login_or_check(qq: QQSession, config: dict, args) -> int:
    """--login 强制重新登录；--check 确保已登录后打印从游戏页提取到的参数。"""
    uin = session.push_uin(qq, config)
    on_qr = session.qr_notifier(config, uin)
    if args.login:
        if not qq.qr_login(on_qr=on_qr, push_uin=uin):
            return 1
    elif not qq.ensure_login(on_qr=on_qr, push_uin=uin):
        return 1

    if args.check:
        ctx = qzone.get_game_context(qq)
        printable = {k: (v[:12] + "…" if isinstance(v, str) and len(v) > 16 else v)
                     for k, v in ctx.items() if k not in ("skey",)}
        log.info("提取到的上下文: %s", json.dumps(printable, ensure_ascii=False))
        log.info("设备状态：%s", qq.device_status())
        return 0

    log.info("登录完成，uin=%s", qq.uin)
    return 0


def main() -> int:
    parser = _parser()
    args = parser.parse_args()

    if not any((args.login, args.check, args.keepalive, args.daily,
                args.list, args.reset, args.import_device, args.country_war)):
        parser.print_help()
        return 0

    for name in settings.ensure_user_copies():
        log.info("已在程序目录生成 %s，可直接编辑", name)
    try:
        config = settings.load()
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 1

    if args.reset:
        if daily.state.reset():
            print(f"已清空今日任务计数：{daily.state.STATE_FILE}")
        else:
            print("今日计数本来就是空的")
        if not args.daily:
            return 0

    if args.list:
        _print_task_list(config)
        return 0

    qq = QQSession()

    if args.import_device:
        return _import_device(qq, args.import_device)

    # 保活：常驻。--keepalive --daily 时才顺带跑一轮任务
    if args.keepalive:
        return socket_keepalive.run(qq, config, with_daily=args.daily)

    # 国战自动战斗：连一次、打 N 次、退出
    if args.country_war:
        return jobs.run_country_war_once(qq, config, args.country_war)

    # 每日任务：连一次、跑一轮、退出
    if args.daily:
        config.setdefault("每日任务", {})["启用"] = True
        return jobs.run_daily_once(qq, config)

    return _login_or_check(qq, config, args)


if __name__ == "__main__":
    sys.exit(main())

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
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tankstorm import engine, notify, paths, qzone, socket_keepalive  # noqa: E402
from tankstorm.log import get_logger                 # noqa: E402
from tankstorm.qq_login import QQSession             # noqa: E402

log = get_logger()

# 打包成 exe 后，第一次运行把出厂配置复制到 exe 旁边，用户改那一份。
# 不复制的话用户看不到 config.json，也就无从配置。已存在则绝不覆盖。
for _f in ("config.json", "endpoints.json", "protocol.json"):
    if paths.ensure_user_copy(_f):
        log.info("已在程序目录生成 %s，可直接编辑", _f)

BASE_DIR = paths.app_dir()
# 配置和协议表：exe 旁边有就用用户那份，没有才用随包默认值
CONFIG_FILE = paths.data_file("config.json")
ENDPOINTS_FILE = paths.data_file("endpoints.json")
# 这两个一律写在程序目录：密钥文件和运行状态都是用户数据
LOCAL_CONFIG_FILE = paths.user_path("config.local.json")   # 放密钥，已 gitignore
STATE_FILE = paths.user_path("state.json")


def load_json(path: str, required: bool = True) -> dict:
    if not os.path.exists(path):
        if required:
            log.error("缺少文件: %s", path)
            sys.exit(1)
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _deep_merge(base: dict, over: dict) -> dict:
    """把 over 深度合并进 base（用于 config.local.json 覆盖 config.json 里的密钥）。"""
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config() -> dict:
    config = load_json(CONFIG_FILE)
    local = load_json(LOCAL_CONFIG_FILE, required=False)  # 本地密钥文件，不进仓库
    return _deep_merge(config, local)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="坦克风暴：保活守护 + 每日任务（两件独立的事）")
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

    g5 = parser.add_argument_group("国战")
    g5.add_argument("--country-war", type=int, metavar="次数", default=0,
                    help="自动扫荡摩多军团 N 次（行动力够就扫荡，不够改普通攻击，"
                         "低于 5 点停手）")

    g4 = parser.add_argument_group("其它")
    g4.add_argument("--task", help="（旧的 HTTP 接口任务，见 endpoints.json）")
    g4.add_argument("--real", action="store_true",
                    help="（已废弃，保留兼容：现在 --daily 一律真实发送）")
    args = parser.parse_args()

    # 什么都不给就打印用法。以前默认会去跑 endpoints.json 里那套早已废弃的
    # HTTP 任务，全部失败还把退出码带成 1，看着像登录坏了。
    if not any((args.login, args.check, args.keepalive, args.daily,
                args.list, args.reset, args.task, args.import_device,
                args.country_war)):
        parser.print_help()
        return 0

    config = load_config()
    endpoints = load_json(ENDPOINTS_FILE)

    if args.reset:
        from tankstorm import daily as _daily
        if os.path.exists(_daily.STATE_FILE):
            os.remove(_daily.STATE_FILE)
            print(f"已清空今日任务计数：{_daily.STATE_FILE}")
        else:
            print("今日计数本来就是空的")
        if not args.daily:
            return 0

    if args.list:
        from tankstorm import daily as _daily
        sw = (config.get("每日任务", {}) or {}).get("任务", {})
        conf = config.get("每日任务", {}) or {}
        st = _daily._load_state()
        print(f"\n每日任务（{'已启用' if conf.get('启用') else '未启用'}，"
              f"实发模式）")
        print(f"{'执行顺序':<4} {'任务':<12} {'opcode':<8} {'消息':<22} "
              f"{'上限':<5} {'今日':<5} {'参数':<6} 开关")
        print("-" * 92)
        for i, t in enumerate(_daily.ordered_tasks(), 1):
            done = st.get("done", {}).get(t.key, 0)
            on = "✅开" if sw.get(t.key) else "  关"
            cd = f"/{t.cooldown_sec // 60}分冷却" if t.cooldown_sec else ""
            mark = "实测" if t.confidence == "实测" else "待确认"
            print(f"{i:>4}   {t.key:<12} {t.opcode:<8} {t.msg:<22} "
                  f"{str(t.max_per_day) + cd:<5} {done:<5} {mark:<6} {on}")
        print("-" * 92)
        print("『实测』= 参数来自真实抓包，可放心开；『待确认』= 默认跳过，需先抓包核对")
        print("周任务/每日任务由代码强制排最后（前面的操作会推进它们的进度）\n")
        return 0

    # 同日去重只对旧的 HTTP 任务有意义。每日任务自己按 logs/daily-state.json
    # 记每项的次数，比"今天整体跑没跑过"精确得多，不该再被这个开关拦住。
    if config.get("同日去重", False) and args.task:
        state = load_json(STATE_FILE, required=False)
        if state.get("last_run") == date.today().isoformat():
            log.info("今天已经跑过（state.json），退出。删掉 state.json 可强制重跑")
            return 0

    qq = QQSession()

    # 一次性引导：把浏览器的设备记录搬进来，之后推送登录才有 dev_mid_sig 可用。
    # pt_fetch_dev_uin 只能给已有的续期，签发不出第一个，所以只能这么来。
    if args.import_device:
        if args.import_device == "-":
            text = sys.stdin.read()
        else:
            with open(args.import_device, encoding="utf-8") as f:
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

    # 保活：常驻。--keepalive --daily 时才顺带跑一轮任务
    if args.keepalive:
        return socket_keepalive.run(qq, config, with_daily=args.daily)

    # 国战自动战斗：连一次、打 N 次、退出
    if args.country_war:
        return socket_keepalive.run_country_war_once(qq, config,
                                                     args.country_war)

    # 每日任务：连一次、跑一轮、退出
    if args.daily:
        config.setdefault("每日任务", {})["启用"] = True
        return socket_keepalive.run_daily_once(qq, config)

    # 需要人工介入时把二维码推到 PushPlus。配了 QQ 号则走「推送登录」，
    # 手机QQ点确认即可，不用扫码（存图后同机扫码会被腾讯拒）。
    push_uin = (config.get("登录", {}) or {}).get("推送登录QQ号") or qq.uin or None

    def on_qr(path, pushed=False):
        if pushed:
            notify.send_qrcode(config, "坦克风暴：请在手机QQ点「确认登录」", path,
                               note=f"已向 QQ {push_uin} 推送登录确认，"
                                    f"<b>打开手机QQ点确认即可，不用扫码</b>。")
        else:
            notify.send_qrcode(config, "坦克风暴：请扫码登录", path,
                               note="请用<b>另一台设备</b>打开本条消息再扫码。")

    if args.login:
        if not qq.qr_login(on_qr=on_qr, push_uin=push_uin):
            return 1
    elif not qq.ensure_login(on_qr=on_qr, push_uin=push_uin):
        return 1

    ctx = qzone.get_game_context(qq)
    if args.check:
        printable = {k: (v[:12] + "…" if isinstance(v, str) and len(v) > 16 else v)
                     for k, v in ctx.items() if k not in ("skey",)}
        log.info("提取到的上下文: %s", json.dumps(printable, ensure_ascii=False))
        log.info("设备状态：%s", qq.device_status())
        return 0

    if args.login:      # --login 到这里就算完了，别再去跑那套废弃的 HTTP 任务
        log.info("登录完成，uin=%s", qq.uin)
        return 0

    # 旧的 HTTP 接口任务（endpoints.json），只有显式 --task 才会走到
    results = engine.run_tasks(qq.session, endpoints, config, ctx, only=args.task)
    summary = engine.summarize(results)
    log.info("执行完毕:\n%s", summary)

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_run": date.today().isoformat()}, f)

    failed = any(r.status == "失败" for r in results)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

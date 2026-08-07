"""坦克风暴（QQ空间 appid 100616028）每日任务脚本 —— 入口。

用法：
  python main.py                 按 config.json 开关执行全部任务
  python main.py --login         强制重新扫码登录
  python main.py --task 每日签到  只执行指定任务（调试单个接口用）
  python main.py --list          列出 endpoints.json 中已配置的任务
  python main.py --check         只检查登录态和参数提取，不执行任务
  python main.py --keepalive     保持在线守护进程（连游戏 socket 定时心跳，防掉线）
"""

import argparse
import json
import os
import sys
from datetime import date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from tankstorm import engine, notify, qzone, socket_keepalive   # noqa: E402
from tankstorm.log import get_logger                 # noqa: E402
from tankstorm.qq_login import QQSession             # noqa: E402

log = get_logger()

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LOCAL_CONFIG_FILE = os.path.join(BASE_DIR, "config.local.json")  # 放密钥，已 gitignore
ENDPOINTS_FILE = os.path.join(BASE_DIR, "endpoints.json")
STATE_FILE = os.path.join(BASE_DIR, "state.json")


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
    parser = argparse.ArgumentParser(description="坦克风暴每日任务（纯请求版）")
    parser.add_argument("--login", action="store_true", help="强制重新扫码登录")
    parser.add_argument("--task", help="只执行指定名称的任务")
    parser.add_argument("--list", action="store_true", help="列出已配置任务")
    parser.add_argument("--check", action="store_true", help="只验证登录与参数提取")
    parser.add_argument("--keepalive", action="store_true",
                        help="保持在线守护进程（连游戏 socket 定时心跳）")
    parser.add_argument("--daily", action="store_true",
                        help="只跑一轮每日任务后退出（测试用，不常驻）")
    parser.add_argument("--real", action="store_true",
                        help="配合 --daily：真实发送（覆盖配置里的干跑）")
    args = parser.parse_args()

    config = load_config()
    endpoints = load_json(ENDPOINTS_FILE)

    if args.list:
        from tankstorm import daily as _daily
        sw = (config.get("每日任务", {}) or {}).get("任务", {})
        conf = config.get("每日任务", {}) or {}
        st = _daily._load_state()
        print(f"\n每日任务（{'已启用' if conf.get('启用') else '未启用'}，"
              f"{'干跑模式' if conf.get('干跑', True) else '实发模式'}）")
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

    # 同日去重：cron 重复触发/手动补跑时避免重复执行
    if config.get("同日去重", False) and not (args.task or args.check):
        state = load_json(STATE_FILE, required=False)
        if state.get("last_run") == date.today().isoformat():
            log.info("今天已经跑过（state.json），退出。删掉 state.json 可强制重跑")
            return 0

    qq = QQSession()

    # 保持在线：长驻守护进程；登录（含二维码 PushPlus 推送）由它内部处理
    if args.keepalive:
        return socket_keepalive.run(qq, config)

    # 只跑一轮每日任务就退出 —— 测试用，不必启动整个守护进程
    if args.daily:
        if args.real:
            config.setdefault("每日任务", {})["干跑"] = False
            log.warning("⚠️ --real：本次会真实发送请求")
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
        return 0

    results = engine.run_tasks(qq.session, endpoints, config, ctx, only=args.task)
    summary = engine.summarize(results)
    log.info("执行完毕:\n%s", summary)

    if not (args.task or args.check):
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_run": date.today().isoformat()}, f)

    failed = any(r.status == "失败" for r in results)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

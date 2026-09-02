#!/usr/bin/env python3
# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""从录制日志里提取**真实客户端发出的** Rce* 请求参数。

为什么需要这个
--------------
每日任务的字段值（哪个 type 是"免费抽"、nDay 填几）**没法从 schema 推断**。
猜错的代价是真金白银——游戏里那个"是否消耗勋章"的确认框是纯客户端 UI
（'是否消耗' 在 onAllBtn1Click、'确认购买' 在 Buytip::processPanel，
协议里根本没有二次确认消息），脚本直接发包不经过它，服务器收到就扣。

所以正确做法是：**看真实客户端在你点"免费"时发了什么，原样复刻。**

重要：守护进程录不到你在浏览器里的操作
--------------------------------------
保活进程和浏览器里的 Flash 是**两条独立的 TCP 连接**，录制器只录守护进程自己
那条。你在浏览器里点按钮，守护进程完全看不见（而且一个账号通常只能一个会话，
两边还会互相踢）。所以要抓"真实客户端点免费按钮时发了什么"，只能：

  **在你自己电脑上开游戏 + Wireshark 抓包 + 用 RC4 解密**

完整流程
--------
0. **先停掉服务器上的保活**，否则两个会话互踢。

1. 浏览器打开游戏，F12 看 iframe 的 FlashVars，记下 `uid` / `sid` / `level`
   / `firstLogin` —— 解密要用（`python main.py --check` 也会打印这些）。

2. Wireshark 开抓，过滤器：

       ip.addr == 193.112.238.18 && tcp.port == 8001

   **必须在游戏加载前就开始抓** —— RC4 密钥流从连接建立起累积，
   中间少一个字节后面全部解不开。

3. 在游戏里把要自动化的操作**手动点一遍**（签到、免费开采、免费冶炼、
   配件探索、军备制造…）。

4. 停止抓包 → 右键那条连接 → 追踪 → TCP 流 → 显示为 Raw →
   **两个方向分别导出** 成 c2s.bin 和 s2c.bin（上下行密钥不同，不能混）。

5. 解密上行（就是客户端发的请求）：

       python tools/redwar_rc4.py c2s.bin --uid <uid> --sid <sid> \
              --level <level> --first-login <firstLogin> --write

6. 从解密结果里提参数：

       python tools/capture_daily.py c2s.bin.decrypted/frames.jsonl

也可以直接读守护进程自己的日志（只含守护进程发的包，用于核对脚本行为）：

       python tools/capture_daily.py                 # 默认读 logs/frames-*.jsonl
       python tools/capture_daily.py --op 04a4       # 只看某个 opcode
       python tools/capture_daily.py --since 14:30   # 只看某时刻之后

输出里的字段值可以直接填进 tankstorm/daily.py 的 TASKS 表，
并把该任务的 confidence 改成 "实测"。
"""

import argparse
import glob
import json
import os
import sys
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

LOG_DIR = os.path.join(BASE, "logs")

# 已知与每日任务相关的 opcode（与 tankstorm/daily.py 的 TASKS 对应）
INTERESTING = {
    "04a4": "RceDailySignIn 每日签到",
    "047a": "RceSevenDays 七天乐",
    "0444": "RceGetDailyRes 每日资源",
    "04a2": "RceZhanGongRank 战功榜",
    "0408": "RceRedwarMonthCard 月卡",
    "04a7": "RceWarGameOpt 军事演习",
    "043a": "RceWPCExplore 军备探索",
    "041e": "RceMineModify 矿区",
    "04de": "RceWeekQuestOpt 周任务",
    "043d": "RceDailyTask 每日任务",
    "0402": "RceHeroVisit 英雄抽奖",
    "0450": "RceAdmiralVisit 将领抽奖",
    "04dc": "RceAdviserDaily 参谋每日",
    "04a5": "RceWarCollegeOpt 战争学院",
}


def main():
    ap = argparse.ArgumentParser(description="提取真实客户端发出的每日任务请求")
    ap.add_argument("--op", help="只看这个 opcode")
    ap.add_argument("--since", help="只看 HH:MM 之后")
    ap.add_argument("--all", action="store_true", help="不限于已知每日任务 opcode")
    ap.add_argument("files", nargs="*", help="jsonl 文件（默认全部）")
    args = ap.parse_args()

    paths = args.files or sorted(glob.glob(os.path.join(LOG_DIR, "frames-*.jsonl")))
    if not paths:
        print(f"没有录制日志。先跑保活（录制上行=true），再手动操作游戏。\n目录：{LOG_DIR}")
        return 1

    # 显式给了文件（多半是 redwar_rc4.py 解密出的 frames.jsonl）就不过滤方向：
    # 那种文件本身只含单个方向，没有 dir 字段。
    explicit = bool(args.files)
    recs = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if explicit or r.get("dir") == "c2s":
                    recs.append(r)

    if not recs:
        print("没有可用记录。\n"
              "  · 读守护进程日志时：确认 config.json 的 录制.录制上行 = true 并重启保活\n"
              "  · 想抓你手动操作的包：守护进程录不到浏览器的连接，\n"
              "    需 Wireshark 抓包 + redwar_rc4.py 解密，步骤见本文件开头注释")
        return 1

    if args.since:
        recs = [r for r in recs if r.get("ts", "")[11:16] >= args.since]
    if args.op:
        recs = [r for r in recs if r.get("op") == args.op]
    elif not args.all:
        recs = [r for r in recs if r.get("op") in INTERESTING]

    if not recs:
        print("没有命中的记录。用 --all 看全部上行，或确认操作时保活确实在跑。")
        return 0

    print(f"共 {len(recs)} 条上行请求\n" + "=" * 74)

    grouped = defaultdict(list)
    for r in grouped_key(recs):
        grouped[r[0]].append(r[1])

    for op, items in sorted(grouped.items()):
        label = INTERESTING.get(op, "")
        print(f"\n[{op}] {label or items[0].get('msg', '')}   共 {len(items)} 次")
        for r in items[:6]:
            ts = r.get("ts", "")[11:]
            data = r.get("data")
            if data:
                print(f"  {ts}  字段: {json.dumps(data, ensure_ascii=False)}")
                print(f"           → TASKS 里写: {as_fields(data)}")
            else:
                hx = r.get("hex", "")
                print(f"  {ts}  (未解密) hex={hx[:60]}{'…' if len(hx) > 60 else ''}")
        if len(items) > 6:
            print(f"  …… 另外 {len(items) - 6} 次")

    print("\n" + "=" * 74)
    print("把上面的字段值填进 tankstorm/daily.py 的 TASKS 表，")
    print("并把该任务的 confidence 从 '待确认' 改成 '实测'，它才会被执行。")
    return 0


def grouped_key(recs):
    for r in recs:
        yield r.get("op", "????"), r


def as_fields(data: dict) -> str:
    """把解码出的字段字典转成 TASKS 表能直接用的形式（字段号未知时给提示）。"""
    if not isinstance(data, dict):
        return str(data)
    parts = []
    for k, v in data.items():
        t = "string" if isinstance(v, str) else ("bool" if isinstance(v, bool) else "int32")
        parts.append(f'"{k}": ("{t}", {v!r})')
    return "{" + ", ".join(parts) + "}   # 字段名→字段号见 docs/redwar.proto"


if __name__ == "__main__":
    sys.exit(main())

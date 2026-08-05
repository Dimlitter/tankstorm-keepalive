#!/usr/bin/env python3
"""从录制日志里提取**真实客户端发出的** Rce* 请求参数。

为什么需要这个
--------------
每日任务的字段值（哪个 type 是"免费抽"、nDay 填几）**没法从 schema 推断**。
猜错的代价是真金白银——游戏里那个"是否消耗勋章"的确认框是纯客户端 UI
（'是否消耗' 在 onAllBtn1Click、'确认购买' 在 Buytip::processPanel，
协议里根本没有二次确认消息），脚本直接发包不经过它，服务器收到就扣。

所以正确做法是：**看真实客户端在你点"免费"时发了什么，原样复刻。**

用法
----
1. 确保 config.json 里 录制.录制上行 = true、录制.实时解密 = true
2. 跑 python main.py --keepalive
3. 手动在游戏里点一次要自动化的操作（签到 / 免费抽奖 / 领任务奖…）
4. 停下来，运行：

       python tools/capture_daily.py                 # 列出今天抓到的所有上行请求
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
                if r.get("dir") == "c2s":        # 只要客户端发出的
                    recs.append(r)

    if not recs:
        print("日志里没有上行(c2s)记录。确认 config.json 的 录制.录制上行 = true 并重启保活。")
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

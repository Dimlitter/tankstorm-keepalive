"""分析录制日志 logs/frames-*.jsonl，找出异常事件（如超级强攻的验证码通知）。

用法：
  python tools/analyze_frames.py                    分析今天的日志
  python tools/analyze_frames.py logs/frames-2026-08-01.jsonl
  python tools/analyze_frames.py --around "09:52"   看某个时刻前后 5 分钟的消息
  python tools/analyze_frames.py --unknown          只看未见过的消息类型

被超级强攻后：先用 --around 定位到事件时刻，再看那前后出现了哪些平时没有的消息，
把结果发给我，我就能把这类事件的 opcode 固化成专门的告警规则。
"""

import argparse
import glob
import json
import os
import sys
from collections import Counter
from datetime import date

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE, "logs")


def load(paths):
    recs = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    recs.sort(key=lambda r: r.get("ts", ""))
    return recs


def hexdump(h, limit=192):
    b = bytes.fromhex(h)[:limit]
    out = []
    for i in range(0, len(b), 16):
        c = b[i:i + 16]
        out.append(f"  {i:04x}  {' '.join(f'{x:02x}' for x in c):<47}  "
                   f"{''.join(chr(x) if 32 <= x < 127 else '.' for x in c)}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="分析坦克风暴录制日志")
    ap.add_argument("files", nargs="*", help="jsonl 文件（默认今天）")
    ap.add_argument("--around", help="时刻 HH:MM，显示前后 5 分钟的消息")
    ap.add_argument("--window", type=int, default=5, help="--around 的分钟窗口，默认5")
    ap.add_argument("--unknown", action="store_true", help="只看未知消息类型")
    ap.add_argument("--keywords", action="store_true", help="只看命中关键词的消息")
    args = ap.parse_args()

    paths = args.files or sorted(glob.glob(os.path.join(LOG_DIR, "frames-*.jsonl")))
    if not paths:
        print(f"没有找到录制日志。保活运行后会生成在 {LOG_DIR}/frames-日期.jsonl")
        return 1
    today = os.path.join(LOG_DIR, f"frames-{date.today().isoformat()}.jsonl")
    if not args.files and today in paths:
        paths = [today]

    recs = load(paths)
    print(f"读取 {len(paths)} 个文件，共 {len(recs)} 条记录")
    if not recs:
        return 0
    print(f"时间范围: {recs[0].get('ts')}  →  {recs[-1].get('ts')}\n")

    sel = recs
    if args.around:
        hhmm = args.around.strip()
        def minutes(ts):
            try:
                h, m = ts.split(" ")[1].split(":")[:2]
                return int(h) * 60 + int(m)
            except Exception:
                return -10**6
        try:
            th, tm = hhmm.split(":")
            target = int(th) * 60 + int(tm)
        except ValueError:
            print("--around 格式应为 HH:MM，如 09:52")
            return 1
        sel = [r for r in recs if abs(minutes(r.get("ts", "")) - target) <= args.window]
        print(f"== {hhmm} 前后 {args.window} 分钟内共 {len(sel)} 条 ==\n")
    if args.unknown:
        sel = [r for r in sel if r.get("unknown")]
    if args.keywords:
        sel = [r for r in sel if r.get("keywords")]

    # 消息类型统计
    cnt = Counter(r["op"] for r in sel)
    print("消息类型分布：")
    for op, c in cnt.most_common(20):
        flag = ""
        if any(r.get("unknown") for r in sel if r["op"] == op):
            flag = "  ← 未知类型！"
        print(f"  {op}  ×{c}{flag}")

    # 重点：未知 / 关键词命中
    hot = [r for r in sel if r.get("unknown") or r.get("keywords")]
    if hot:
        print(f"\n{'='*60}\n重点事件 {len(hot)} 条（未知类型 / 命中关键词）\n{'='*60}")
        for r in hot:
            print(f"\n[{r.get('ts')}] op={r['op']} seq={r.get('seq')} len={r.get('len')}")
            if r.get("keywords"):
                print(f"  命中关键词: {r['keywords']}")
            if r.get("text"):
                print(f"  可读文本: {r['text'][:300]}")
            if r.get("hex"):
                print(hexdump(r["hex"]))
    else:
        print("\n未发现未知类型或关键词命中的消息。")
        if args.around:
            print("提示：事件消息可能用的是已知 opcode。可以看上面分布里"
                  "平时很少出现的类型，或把这段日志发我人工比对。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

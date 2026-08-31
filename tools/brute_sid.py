#!/usr/bin/env python3
# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""抓包时忘了记 FlashVars 的 sid？用已知明文爆破出来。

原理
----
RC4 密钥 = uid ‖ mid ‖ sid（接收方向）。uid 在登录行里就有，
mid = level*100 + (firstLogin?0:1) 只有几百种，真正未知的只有 8 位的 sid。

而每条连接的**第一条加密消息**是固定的 RseAuthState(020c)，它的 protobuf
开头极其可预测 —— 实测明文为 08 00 10 <varint时间戳> 18 00 20 01 28 00，
前三字节 08 00 10 是结构性的。拿这三字节当已知明文筛，再用完整 protobuf
校验复核，就能把 sid 捞出来。

用法
----
    python tools/brute_sid.py streams/<端口>/s2c.bin --uid 1764591629467676 --level 34

找到后照常解密：
    python tools/redwar_rc4.py ... --sid <爆破出的sid>
"""

import argparse
import os
import struct
import sys
import time
from multiprocessing import Pool, cpu_count

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tankstorm import crypto as C            # noqa: E402

KNOWN_PREFIX = bytes([0x08, 0x00, 0x10])     # RseAuthState 明文开头
_G = {}


def _init(uid, mid, body, reversed_sbox):
    _G.update(uid=uid, mid=mid, body=body, rev=reversed_sbox)


def _pb_ok(b):
    """粗校验：能否整体解析成合法 protobuf。"""
    i, n = 0, len(b)
    while i < n:
        key = b[i]; i += 1
        fno, wt = key >> 3, key & 7
        if fno == 0 or wt in (3, 4, 6, 7):
            return False
        if wt == 0:
            while i < n and b[i] & 0x80:
                i += 1
            i += 1
        elif wt == 2:
            if i >= n:
                return False
            ln = b[i]; i += 1 + ln
        elif wt == 5:
            i += 4
        elif wt == 1:
            i += 8
    return i == n


def _scan(rng):
    lo, hi = rng
    uid, mid, body, rev = _G["uid"], _G["mid"], _G["body"], _G["rev"]
    head = body[:3]
    hits = []
    for sid in range(lo, hi):
        key = f"{uid}{mid}{sid}".encode()
        try:
            p = C.RC4(key, rev).crypt(head)
        except ValueError:
            continue
        if p == KNOWN_PREFIX:
            full = C.RC4(key, rev).crypt(body)      # 复核整条
            if _pb_ok(full):
                hits.append((sid, full.hex()))
    return hits


def main():
    ap = argparse.ArgumentParser(description="爆破未知的 sid")
    ap.add_argument("stream", help="s2c.bin")
    ap.add_argument("--uid", required=True)
    ap.add_argument("--level", type=int, help="已知 level 可大幅缩小范围")
    ap.add_argument("--first-login", default="false")
    ap.add_argument("--digits", type=int, default=8, help="sid 位数，默认 8")
    ap.add_argument("--jobs", type=int, default=0, help="进程数，默认按 CPU")
    args = ap.parse_args()

    d = open(args.stream, "rb").read()
    ln = struct.unpack(">H", d[:2])[0]
    op = d[2:4].hex()
    body = d[8:2 + ln]
    if op != "020c":
        print(f"⚠️ 首条消息是 {op} 而不是预期的 020c，已知明文可能不适用")
    print(f"首条加密消息 {op}，body {len(body)} 字节")

    mids = ([C.middle(args.level, args.first_login)] if args.level
            else [lv * 100 + f for lv in range(1, 100) for f in (0, 1)])
    lo, hi = 10 ** (args.digits - 1), 10 ** args.digits
    jobs = args.jobs or max(1, cpu_count() - 1)
    total = (hi - lo) * len(mids)
    print(f"搜索 sid {lo:,}–{hi:,} × 中段 {len(mids)} 种 = {total:,} 组合，{jobs} 进程")

    t0 = time.time()
    for mid in mids:
        step = 200_000
        chunks = [(a, min(a + step, hi)) for a in range(lo, hi, step)]
        with Pool(jobs, _init, (args.uid, mid, body, True)) as pool:
            for i, hits in enumerate(pool.imap_unordered(_scan, chunks), 1):
                for sid, plain in hits:
                    el = time.time() - t0
                    print(f"\n★ 命中 sid={sid}  中段={mid}  用时 {el / 60:.1f} 分钟")
                    print(f"  明文 {plain}")
                    print(f"\n  接着跑：\n    python tools/redwar_rc4.py "
                          f"{args.stream} --uid {args.uid} --sid {sid} "
                          f"--level {mid // 100} "
                          f"--first-login {'true' if mid % 100 == 0 else 'false'} --write")
                    return 0
                if i % 20 == 0:
                    done = i * step
                    el = time.time() - t0
                    print(f"  已扫 {done:,}/{hi - lo:,}（{done / (hi - lo):.0%}），"
                          f"用时 {el / 60:.1f} 分钟", flush=True)
    print("未找到 —— 检查 uid 是否正确、sid 是否真是 8 位、流是否从连接建立起录的")
    return 1


if __name__ == "__main__":
    sys.exit(main())

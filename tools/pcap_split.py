#!/usr/bin/env python3
"""从 pcapng/pcap 里把游戏 socket 的两个方向拆成独立的 .bin，供 redwar_rc4.py 解密。

为什么需要它
------------
RC4 上下行用两个不同实例、两条独立密钥流，所以解密必须拿到**按方向分离**的
字节流。Wireshark 里「追踪TCP流 → 显示为Raw」手动导两次很容易搞混，
而 pcapng 本身就带完整的方向信息 —— 直接按 IP/端口拆更稳。

更重要的是：密钥流逐字节累积，**中间缺一个字节后面就全错**。本工具会按 TCP
序列号重组并**检测空洞**，有缺失会明确报出来，而不是悄悄产出一份解不开的流。

用法
----
    python tools/pcap_split.py 抓包.pcapng                 # 自动找游戏连接
    python tools/pcap_split.py 抓包.pcapng -o out/         # 指定输出目录
    python tools/pcap_split.py 抓包.pcapng --port 443      # 备用端口

每条连接输出到 out/<客户端端口>/ 下的 c2s.bin、s2c.bin、meta.json。
之后：
    python tools/redwar_rc4.py out/<端口>/c2s.bin --uid ... --sid ... --level ... --write
"""

import argparse
import json
import os
import struct
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pcap_analyze import iter_packets, parse_frame          # noqa: E402

DEFAULT_HOST = "193.112.238.18"          # tankstorm-proxy.sincetimes.com
DEFAULT_PORT = 8001


class Stream:
    """按 TCP 序列号重组单向字节流，并记录空洞。"""

    def __init__(self):
        self.chunks = {}        # 相对偏移 -> 数据
        self.base = None
        self.retrans = 0

    def add(self, seq, data):
        if not data:
            return
        if self.base is None:
            self.base = seq
        off = seq - self.base
        if off < 0:                       # 序号回绕或乱序早于起点
            self.base = seq
            self.chunks = {k + (-off): v for k, v in self.chunks.items()}
            off = 0
        old = self.chunks.get(off)
        if old is not None:
            if old != data[:len(old)]:
                self.retrans += 1         # 同位置不同内容 = 重传/覆盖
            if len(data) <= len(old):
                return
        self.chunks[off] = data

    def assemble(self):
        """返回 (字节流, 空洞列表)。空洞用 \\x00 填充但会如实报告。"""
        if not self.chunks:
            return b"", []
        end = max(o + len(d) for o, d in self.chunks.items())
        buf = bytearray(end)
        filled = bytearray(end)
        for off, data in sorted(self.chunks.items()):
            buf[off:off + len(data)] = data
            for i in range(off, off + len(data)):
                filled[i] = 1
        holes, start = [], None
        for i, f in enumerate(filled):
            if not f and start is None:
                start = i
            elif f and start is not None:
                holes.append((start, i - start))
                start = None
        if start is not None:
            holes.append((start, end - start))
        return bytes(buf), holes


def main():
    ap = argparse.ArgumentParser(description="按方向拆分游戏 socket 流")
    ap.add_argument("pcap")
    ap.add_argument("-o", "--out", default="streams", help="输出目录")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()

    if not os.path.exists(args.pcap):
        print("找不到文件:", args.pcap)
        return 1

    conns = defaultdict(lambda: {"c2s": Stream(), "s2c": Stream(),
                                 "t0": None, "t1": None, "pkts": 0})
    total = 0
    for ts, lt, frame in iter_packets(args.pcap):
        info = parse_frame(lt, frame)
        if not info:
            continue
        sip, sport, dip, dport, seq, payload = info
        if dip == args.host and dport == args.port:
            key, direction = sport, "c2s"
        elif sip == args.host and sport == args.port:
            key, direction = dport, "s2c"
        else:
            continue
        c = conns[key]
        c["pkts"] += 1
        if c["t0"] is None:
            c["t0"] = ts
        c["t1"] = ts
        c[direction].add(seq, payload)
        total += 1

    if not conns:
        print(f"没找到与 {args.host}:{args.port} 的流量。\n"
              f"确认过滤器用的是 ip.addr == {args.host} && tcp.port == {args.port}；"
              f"若走了备用端口，加 --port 443 再试。")
        return 1

    print(f"命中 {total} 个包，{len(conns)} 条连接\n" + "=" * 66)
    os.makedirs(args.out, exist_ok=True)
    best = None
    for cport in sorted(conns, key=lambda k: -(conns[k]["t1"] - conns[k]["t0"])):
        c = conns[cport]
        dur = c["t1"] - c["t0"]
        d = os.path.join(args.out, str(cport))
        os.makedirs(d, exist_ok=True)
        meta = {"client_port": cport, "server": f"{args.host}:{args.port}",
                "duration_sec": round(dur, 1), "packets": c["pkts"]}
        print(f"\n[客户端端口 {cport}]  时长 {dur:7.1f}s  包 {c['pkts']}")
        ok = True
        for direction in ("c2s", "s2c"):
            data, holes = c[direction].assemble()
            path = os.path.join(d, direction + ".bin")
            with open(path, "wb") as f:
                f.write(data)
            lost = sum(n for _, n in holes)
            meta[direction] = {"bytes": len(data), "holes": len(holes),
                               "lost_bytes": lost,
                               "retransmits": c[direction].retrans}
            flag = "✅" if not holes else f"⚠️ {len(holes)} 处空洞，缺 {lost} 字节"
            print(f"   {direction}: {len(data):>9,} 字节  {flag}")
            if holes:
                ok = False
                for off, n in holes[:3]:
                    print(f"        偏移 {off} 缺 {n} 字节")
        meta["usable_for_decrypt"] = ok
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        if ok and (best is None or dur > best[1]):
            best = (cport, dur)

    print("\n" + "=" * 66)
    if best:
        p = os.path.join(args.out, str(best[0]))
        print(f"推荐用时长最久且无空洞的连接：{p}\n")
        print("解密上行（客户端发的请求，每日任务参数在这里）：")
        print(f"  python tools/redwar_rc4.py {p}/c2s.bin \\")
        print(f"         --uid <uid> --sid <sid> --level <level> --first-login <firstLogin> --write")
        print("\n再提参数：")
        print(f"  python tools/capture_daily.py {p}/c2s.bin.decrypted/frames.jsonl")
    else:
        print("⚠️ 所有连接都有空洞 —— RC4 密钥流逐字节累积，缺字节就解不开。")
        print("   可能是抓包时丢包，或没从连接建立就开始抓。建议重抓。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

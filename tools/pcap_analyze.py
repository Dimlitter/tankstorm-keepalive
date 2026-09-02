# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""零依赖解析 Wireshark 抓包（.pcap / .pcapng），还原坦克风暴 socket 协议。

专门针对游戏服务器 tankstorm-proxy.sincetimes.com (193.112.238.18) 的 8001 端口，
把 TCP 流按方向重组，帮我定位：
  1) 登录握手包（连接后客户端发的第一批字节）
  2) 心跳包（挂机时客户端每隔固定秒数重复发的那个小包）

用法（项目根目录）：
  python tools/pcap_analyze.py 抓包.pcapng
  python tools/pcap_analyze.py 抓包.pcapng --host 193.112.238.18 --port 8001

输出：
  - 两个方向的字节流总量、连接时间线
  - 客户端→服务器 前若干包的 hex+ASCII（含帧长度猜测）
  - 疑似心跳：重复出现、间隔规律的客户端小包
  - 落盘 socket_capture.json：结构化的握手/心跳候选，供保活客户端直接使用
"""

import argparse
import json
import os
import socket
import struct
import sys
from collections import Counter, defaultdict

DEFAULT_HOST = "193.112.238.18"
DEFAULT_PORT = 8001


# ---------------- pcap / pcapng 解析 ----------------

def _iter_pcap(data):
    """经典 pcap：yield (ts_float, linktype, frame_bytes)。"""
    magic = data[:4]
    if magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        endian, nano = ">", magic == b"\xa1\xb2\x3c\x4d"
    elif magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        endian, nano = "<", magic == b"\x4d\x3c\xb2\xa1"
    else:
        raise ValueError("不是经典 pcap")
    linktype = struct.unpack(endian + "I", data[20:24])[0]
    off = 24
    n = len(data)
    while off + 16 <= n:
        ts_sec, ts_frac, incl, orig = struct.unpack(endian + "IIII", data[off:off + 16])
        off += 16
        frame = data[off:off + incl]
        off += incl
        ts = ts_sec + ts_frac / (1e9 if nano else 1e6)
        yield ts, linktype, frame


def _iter_pcapng(data):
    """pcapng：yield (ts_float, linktype, frame_bytes)。只处理 SHB/IDB/EPB/SPB。"""
    off = 0
    n = len(data)
    endian = "<"
    linktypes = {}
    if_index = 0
    tsresol = {}
    while off + 8 <= n:
        block_type = data[off:off + 4]
        # Section Header Block 确定字节序
        if block_type == b"\x0a\x0d\x0d\x0a":
            bom = data[off + 8:off + 12]
            endian = "<" if bom == b"\x4d\x3c\x2b\x1a" else ">"
        btype = struct.unpack(endian + "I", data[off:off + 4])[0]
        blen = struct.unpack(endian + "I", data[off + 4:off + 8])[0]
        if blen < 12 or off + blen > n:
            break
        body = data[off + 8:off + blen - 4]
        if btype == 0x00000001:  # Interface Description Block
            lt = struct.unpack(endian + "H", body[0:2])[0]
            linktypes[if_index] = lt
            # 解析 if_tsresol(option code=9)；默认 1e-6。Wireshark/Npcap 常用纳秒(1e-9)，
            # 不解析会导致时间戳与心跳间隔差 1000 倍。
            resol = 1e-6
            ooff = 8
            while ooff + 4 <= len(body):
                ocode, olen = struct.unpack(endian + "HH", body[ooff:ooff + 4])
                if ocode == 0:
                    break
                if ocode == 9 and olen >= 1:
                    tr = body[ooff + 4]
                    resol = (1.0 / (2 ** (tr & 0x7f))) if (tr & 0x80) else (10.0 ** -tr)
                ooff += 4 + olen + ((4 - olen % 4) % 4)
            tsresol[if_index] = resol
            if_index += 1
        elif btype == 0x00000006:  # Enhanced Packet Block
            iface = struct.unpack(endian + "I", body[0:4])[0]
            ts_high, ts_low = struct.unpack(endian + "II", body[4:12])
            caplen = struct.unpack(endian + "I", body[12:16])[0]
            frame = body[20:20 + caplen]
            ts_raw = (ts_high << 32) | ts_low
            ts = ts_raw * tsresol.get(iface, 1e-6)
            yield ts, linktypes.get(iface, 1), frame
        elif btype == 0x00000003:  # Simple Packet Block
            caplen = struct.unpack(endian + "I", body[0:4])[0]
            frame = body[4:4 + caplen]
            yield 0.0, linktypes.get(0, 1), frame
        off += blen


def iter_packets(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] == b"\x0a\x0d\x0d\x0a":
        yield from _iter_pcapng(data)
    else:
        yield from _iter_pcap(data)


# ---------------- 链路层 / IP / TCP 解包 ----------------

def parse_frame(linktype, frame):
    """返回 (src_ip, src_port, dst_ip, dst_port, seq, payload) 或 None。"""
    if linktype == 1:            # Ethernet
        if len(frame) < 14:
            return None
        eth_type = struct.unpack(">H", frame[12:14])[0]
        ip_off = 14
        if eth_type == 0x8100:   # 802.1Q VLAN
            eth_type = struct.unpack(">H", frame[16:18])[0]
            ip_off = 18
        if eth_type != 0x0800:
            return None
    elif linktype == 101:        # raw IP
        ip_off = 0
    elif linktype == 113:        # Linux cooked
        ip_off = 16
    else:
        # 尝试按裸 IPv4 处理
        ip_off = 0
        if not frame or frame[0] >> 4 != 4:
            return None

    if len(frame) < ip_off + 20:
        return None
    b0 = frame[ip_off]
    if b0 >> 4 != 4:
        return None
    ihl = (b0 & 0x0F) * 4
    proto = frame[ip_off + 9]
    if proto != 6:               # 只要 TCP
        return None
    src_ip = socket.inet_ntoa(frame[ip_off + 12:ip_off + 16])
    dst_ip = socket.inet_ntoa(frame[ip_off + 16:ip_off + 20])
    tcp_off = ip_off + ihl
    if len(frame) < tcp_off + 20:
        return None
    src_port, dst_port, seq = struct.unpack(">HHI", frame[tcp_off:tcp_off + 8])
    data_off = (frame[tcp_off + 12] >> 4) * 4
    payload = frame[tcp_off + data_off:]
    return src_ip, src_port, dst_ip, dst_port, seq, payload


def hexdump(b, limit=256):
    out = []
    b = b[:limit]
    for i in range(0, len(b), 16):
        chunk = b[i:i + 16]
        hexs = " ".join(f"{c:02x}" for c in chunk)
        asci = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
        out.append(f"  {i:04x}  {hexs:<47}  {asci}")
    if len(b) == limit:
        out.append("  …(截断)")
    return "\n".join(out)


def guess_framing(stream):
    """猜测帧结构：2字节大端长度前缀？4字节？还是 \\x00 分隔(XMLSocket)？"""
    notes = []
    if b"\x00" in stream[:400] and stream.count(b"\x00") > 2:
        parts = stream.split(b"\x00")
        if sum(1 for p in parts if p) >= 2:
            notes.append(f"可能是 \\x00 分隔(XMLSocket 风格)，切出 {len(parts)} 段")
    for name, size, fmt in (("2字节大端", 2, ">H"), ("4字节大端", 4, ">I")):
        if len(stream) >= size:
            ln = struct.unpack(fmt, stream[:size])[0]
            if 0 < ln < len(stream) + 4 and ln < 100000:
                notes.append(f"若为[{name}]长度前缀：首帧声明 {ln} 字节，"
                             f"实际后续 {len(stream) - size} 字节")
    return notes or ["未识别出明显帧结构（可能是定长/二进制自定义协议）"]


def main():
    ap = argparse.ArgumentParser(description="解析坦克风暴 socket 抓包")
    ap.add_argument("pcap", help=".pcap / .pcapng 文件")
    ap.add_argument("--host", default=DEFAULT_HOST, help="游戏服务器 IP")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="游戏服务器端口")
    ap.add_argument("-o", "--output", default="socket_capture.json")
    args = ap.parse_args()

    if not os.path.exists(args.pcap):
        print("找不到文件:", args.pcap)
        return 1

    # 收集与游戏服务器相关的 TCP 包
    c2s_packets = []   # (ts, seq, payload)  客户端->服务器
    s2c_packets = []   # (ts, seq, payload)  服务器->客户端
    total = 0
    for ts, linktype, frame in iter_packets(args.pcap):
        info = parse_frame(linktype, frame)
        if not info:
            continue
        src_ip, src_port, dst_ip, dst_port, seq, payload = info
        if dst_ip == args.host and dst_port == args.port:
            total += 1
            if payload:
                c2s_packets.append((ts, seq, payload))
        elif src_ip == args.host and src_port == args.port:
            total += 1
            if payload:
                s2c_packets.append((ts, seq, payload))

    print(f"=== 目标 {args.host}:{args.port} ===")
    print(f"相关 TCP 包: {total}（客户端→服务器 有效载荷包 {len(c2s_packets)}，"
          f"服务器→客户端 {len(s2c_packets)}）")
    if total == 0:
        print("\n没抓到与该服务器的通信。请确认：")
        print(f"  - Wireshark 过滤器用了 ip.addr == {args.host} && tcp.port == {args.port}")
        print("  - 抓包时游戏确实在运行（socket 已连上）")
        print("  - 若游戏走了备用端口 443，请加 --port 443 重试")
        return 1

    # 按 seq 重组两个方向的字节流
    def reassemble(packets):
        if not packets:
            return b""
        base = min(seq for _, seq, _ in packets)
        buf = bytearray()
        for _, seq, payload in sorted(packets, key=lambda x: x[1]):
            pos = seq - base
            if pos < 0:
                continue
            if pos > len(buf):
                buf.extend(b"\x00" * (pos - len(buf)))
            buf[pos:pos + len(payload)] = payload
        return bytes(buf)

    c2s = reassemble(c2s_packets)
    s2c = reassemble(s2c_packets)

    print("\n=== 客户端→服务器 首 256 字节（登录握手候选）===")
    print(hexdump(c2s, 256))
    print("  帧结构猜测:", "; ".join(guess_framing(c2s)))

    print("\n=== 服务器→客户端 首 256 字节 ===")
    print(hexdump(s2c, 256))

    # 心跳检测：客户端小包里，内容重复且时间间隔规律的
    print("\n=== 心跳检测（客户端重复小包）===")
    by_payload = defaultdict(list)   # payload -> [ts,...]
    for ts, seq, payload in c2s_packets:
        if len(payload) <= 64:       # 心跳一般很小
            by_payload[payload].append(ts)
    heartbeat = None
    candidates = sorted(by_payload.items(), key=lambda kv: -len(kv[1]))
    for payload, tss in candidates[:6]:
        if len(tss) < 2:
            continue
        tss = sorted(tss)
        gaps = [round(tss[i + 1] - tss[i], 1) for i in range(len(tss) - 1)]
        common = Counter(gaps).most_common(1)[0] if gaps else (0, 0)
        print(f"\n  重复 {len(tss)} 次，长度 {len(payload)} 字节，"
              f"间隔(秒) {gaps[:12]}")
        print(hexdump(payload, 48))
        # 出现≥3次且间隔较稳定 → 判为心跳
        if len(tss) >= 3 and common[1] >= 2 and common[0] > 0:
            if heartbeat is None:
                heartbeat = {"hex": payload.hex(), "len": len(payload),
                             "interval_sec": common[0], "count": len(tss)}
                print(f"  ★ 判定为心跳：约每 {common[0]} 秒一次")

    result = {
        "server": {"host_ip": args.host, "port": args.port,
                   "dns": "tankstorm-proxy.sincetimes.com"},
        "login_handshake_hex": c2s[:512].hex(),
        "server_first_reply_hex": s2c[:512].hex(),
        "heartbeat": heartbeat,
        "framing_note": guess_framing(c2s),
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n结构化结果已写出 {args.output}")
    if heartbeat:
        print(f"找到心跳：{heartbeat['len']} 字节 / 约 {heartbeat['interval_sec']} 秒一次。"
              "把这个 json 发我，我据此写保活客户端。")
    else:
        print("未自动判定出心跳。把 socket_capture.json 和 .pcapng 一起发我，"
              "我手动分析登录握手与心跳。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""坦克风暴 RC4 解密 —— 规则全部来自 RedWar_2026073102.swf 的字节码实测。

    python tools/redwar_rc4.py logs/streams/<会话>/s2c.bin --sid <sid> --uid <uid>
    python tools/redwar_rc4.py logs/streams/<会话>/s2c.bin --sid <sid> --uid <uid> --write

反汇编结论（去掉 secureSWF 的不透明谓词后）
-------------------------------------------
Transport.Send(msg)：
    op = _-5sY._-7CF(msg);  ba = new ByteArray(BIG_ENDIAN);  msg.writeExternal(ba)
    if (op != 0x040E && op != 0x041C && op != 0x041D && op != 0x0455)
        _-4I4.encrypt(ba)                       ← 白名单之外才加密
    header.length = ba.length + 6;  header.type = op
    socket.writeHeader(header); socket.writeBytes(ba); socket.flush()

Transport._-29U()（socket 数据处理，接收）：
    socket.readBytes(ba, 0, _-55i)               ← 按包头长度读 body
    if (op != 0x0215 && op != 0x0228 && op != 0x0229 && op != 0x0230 && op != 0x0283) {
        decode(ba);                              ← decode() 里 _-1yo._-2Sm(ba)
        ba.position = 0
    }
    msg.readExternal(ba)                         ← protobuf 反序列化

Transport._-3rU(登录响应)：
    _-0fk = msg.level;  _-0MJ = msg.sid
    _-0tS = new SecNum(String(msg.firstLogin)=='true' ? 0 : 1)
    _-2q0(int(_-0fk * 100 + _-0tS.Get()))        ← 密钥第三段

Transport._-2q0(mid)：
    if (_-0MJ != null) {
        _-5nc = BASE._-71r + mid + _-0MJ         ← 接收密钥
        _-08k = _-0MJ + BASE._-71r + mid         ← 发送密钥
    } else { _-5nc = _-08k = BASE._-71r }
    _-1yo = new RC4(utf8(_-5nc), true)           ← 接收，S[k]=255-k
    _-4I4 = new RC4(utf8(_-08k), false)          ← 发送，S[k]=k

RC4.init(key, reversed)：
    for k in 0..255: S[k] = reversed ? 255-k : k
    j = 0;  keylen = key.length - 2              ← 无条件，不是假分支
    for k in 0..255: j = (j + S[k] + key[k % keylen]) & 255; swap(S[k], S[j])
    i = 11;  j = 11                              ← 无条件，不是假分支

BASE._-71r：编译期默认 '780511549720865'，运行时被 RedWar.Data 覆盖成 uid。

三个要点
--------
1. 包头永远明文，只有 body 过 RC4。
2. 豁免 opcode 的 body **完全不碰 RC4 实例**，不消耗密钥流。
3. 每个方向一个 RC4 实例，登录后建立、之后再不重置 —— 密钥流在**该方向所有
   非豁免 body** 之间连续累积。所以要解第 N 条，必须按顺序喂入它之前的每一条
   非豁免 body，一条不能少；但包头和豁免消息必须**排除在外**。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from tankstorm import crypto                 # RC4、密钥推导、豁免名单的单一来源

try:
    from tankstorm import schema as SCH        # opcode/字段名对照，可选
except Exception:
    SCH = None

# 豁免 opcode 与 RC4 实现都来自 tankstorm/crypto.py，这里只做 int 化的视图
EXEMPT = {d: {int(o, 16) for o in ops} for d, ops in crypto.EXEMPT.items()}
DEFAULT_PREFIX = crypto.DEFAULT_PREFIX
RC4 = crypto.RC4
make_key = crypto.make_key


# ---------------------------------------------------------------- RC4

# ---------------------------------------------------------------- 分帧 / protobuf

def frames(buf: bytes, start: int = 0):
    """[u16be len][u16be op][u32be seq][body]，len 覆盖 op+seq+body。"""
    off, n = start, len(buf)
    while off + 8 <= n:
        ln = int.from_bytes(buf[off:off + 2], "big")
        if ln < 6 or off + 2 + ln > n:
            return
        yield (off, int.from_bytes(buf[off + 2:off + 4], "big"),
               int.from_bytes(buf[off + 4:off + 8], "big"), off + 8, off + 2 + ln)
        off += 2 + ln


def find_start(buf: bytes, limit: int = 2048) -> int:
    """跳过 TGW 网关头之类的非帧前缀。"""
    best = (0, -1)
    for s in range(min(limit, max(1, len(buf) - 8))):
        cnt = sum(1 for _ in frames(buf, s))
        if cnt > best[0]:
            best = (cnt, s)
    return best[1] if best[1] >= 0 else 0


def _varint(b, i):
    shift = val = 0
    n = len(b)
    while i < n:
        c = b[i]
        i += 1
        val |= (c & 0x7F) << shift
        if not c & 0x80:
            return val, i
        shift += 7
        if shift > 63:
            return None, i
    return None, i


def pb_ok(body: bytes):
    """返回 (整体合法, 字段数, 最大 bytes 字段长度)。"""
    n = len(body)
    if n == 0:
        return True, 0, 0
    i, nf, big = 0, 0, 0
    while i < n:
        key, i = _varint(body, i)
        if key is None:
            return False, nf, big
        wt, fn = key & 7, key >> 3
        if fn == 0 or wt in (3, 4, 6, 7):
            return False, nf, big
        if wt == 0:
            v, i = _varint(body, i)
            if v is None:
                return False, nf, big
        elif wt == 1:
            i += 8
        elif wt == 5:
            i += 4
        else:
            ln, i = _varint(body, i)
            if ln is None or ln > n - i:
                return False, nf, big
            big = max(big, ln)
            i += ln
        nf += 1
        if i > n:
            return False, nf, big
    return i == n, nf, big


def strength(body: bytes) -> int:
    """0=不像明文, 1=弱, 2=强。防止随机数据靠"一个巨型 bytes 字段"蒙混。"""
    ok, nf, big = pb_ok(body)
    if not ok or nf == 0:
        return 0
    if nf == 1 and big >= 0.9 * len(body):
        return 1
    if nf >= 3:
        return 2
    try:
        t = body.decode("utf-8")
        if any("一" <= c <= "鿿" for c in t):
            return 2
    except UnicodeDecodeError:
        pass
    return 2 if nf >= 2 else 1


def entropy(b) -> float:
    if not b:
        return 0.0
    n = len(b)
    return -sum((c / n) * math.log2(c / n) for c in Counter(b).values())


# ---------------------------------------------------------------- 主流程

def collect(buf, direction, start):
    """切帧，分出豁免/非豁免。非豁免 body 按序构成密钥流的输入。"""
    ex = EXEMPT[direction]
    enc, plain = [], []
    for off, op, seq, bo, be in frames(buf, start):
        # 上行的登录行 a,{uid},{secret} 是 Transport 之外的裸写，被分帧器解析成
        # op=612c('a,')。它不过 Send()，不消耗密钥流，必须排除。
        raw_write = direction == "c2s" and not (0x0400 <= op <= 0x04FF)
        (plain if (raw_write or op in ex) else enc).append((off, op, seq, bo, be))
    return enc, plain


def try_key(buf, enc, direction, prefix, mid, sid, skip, probe):
    """返回 (强命中数, 参与数)。skip = 跳过前几条非豁免 body。"""
    try:
        rc4 = RC4(make_key(direction, prefix, mid, sid), direction == "s2c")
    except ValueError:
        return 0, 0
    good = tot = 0
    for off, op, seq, bo, be in enc[skip:]:
        p = rc4.crypt(buf[bo:be])
        if be - bo:
            tot += 1
            if strength(p) == 2:
                good += 1
            if tot >= probe:
                break
    return good, tot


def main():
    ap = argparse.ArgumentParser(description="坦克风暴 RC4 解密（规则来自 SWF 字节码）")
    ap.add_argument("stream", help="logs/streams/<会话>/s2c.bin 或 c2s.bin")
    ap.add_argument("--dir", choices=["s2c", "c2s"],
                    help="方向，默认按文件名推断")
    ap.add_argument("--sid", help="登录响应里的 sid（没有则从同目录 meta.json 读）")
    ap.add_argument("--uid", help="uid —— 运行时 BASE._-71r 会被覆盖成它")
    ap.add_argument("--prefix", action="append",
                    help="额外的 _-71r 候选值，可重复")
    ap.add_argument("--mid", type=int,
                    help="密钥中段 level*100+flag，已知就直接给，省去穷举")
    ap.add_argument("--level", help="FlashVars 里的 level，给了就不用穷举")
    ap.add_argument("--first-login", default="false",
                    help="FlashVars 里的 firstLogin，配合 --level 使用")
    ap.add_argument("--max-level", type=int, default=200, help="穷举 level 上限")
    ap.add_argument("--max-skip", type=int, default=3,
                    help="允许前几条非豁免 body 不算进密钥流（抓漏了的话调大）")
    ap.add_argument("--probe", type=int, default=12, help="每个假设试解多少条")
    ap.add_argument("--write", action="store_true", help="把解密结果写到 <流>.decrypted/")
    args = ap.parse_args()

    buf = open(args.stream, "rb").read()
    direction = args.dir or ("c2s" if "c2s" in os.path.basename(args.stream) else "s2c")
    sid = args.sid
    meta_p = os.path.join(os.path.dirname(args.stream), "meta.json")
    if not sid and os.path.exists(meta_p):
        sid = str(json.load(open(meta_p, encoding="utf-8")).get("sid") or "")
    if not sid:
        ap.error("需要 sid（--sid，或同目录放 meta.json）")

    start = find_start(buf)
    enc, plain = collect(buf, direction, start)
    print(f"{args.stream}  方向 {direction}  {len(buf)} 字节，第一帧偏移 {start}")
    print(f"  切出 {len(enc) + len(plain)} 帧：非豁免（加密）{len(enc)} 条，"
          f"豁免（明文）{len(plain)} 条")
    print(f"  豁免 opcode: {' '.join(f'{o:04x}' for o in sorted(EXEMPT[direction]))}")
    if plain:
        c = Counter(f"{op:04x}" for _, op, _, _, _ in plain)
        print(f"  实际出现的豁免消息: {dict(c)}")
    if not enc:
        print("  没有非豁免消息 —— 这条流里没有加密内容")
        return 0
    nonempty = [e for e in enc if e[4] > e[3]]
    print(f"  其中非空 body {len(nonempty)} 条，第一条 opcode "
          f"{nonempty[0][1]:04x} 长度 {nonempty[0][4] - nonempty[0][3]}"
          f"，熵 {entropy(buf[nonempty[0][3]:nonempty[0][4]]):.2f}")

    prefixes = [p for p in ([args.uid] if args.uid else []) + [DEFAULT_PREFIX]
                + (args.prefix or []) if p]
    if args.mid is not None:
        mids = [args.mid]
    elif args.level:
        mids = [crypto.middle(args.level, args.first_login)]
    else:
        mids = [lv * 100 + f for lv in range(1, args.max_level + 1) for f in (0, 1)]
    print(f"\n  搜索空间: _-71r {len(prefixes)} 个 × 中段 {len(mids)} 个 × "
          f"skip 0..{args.max_skip}")

    best = None
    for prefix in prefixes:
        for mid in mids:
            for skip in range(args.max_skip + 1):
                good, tot = try_key(buf, enc, direction, prefix, mid, sid,
                                    skip, args.probe)
                if tot and (best is None or good / tot > best[0]):
                    best = (good / tot, good, tot, prefix, mid, skip)
                    if good == tot and tot >= 3:
                        break
            if best and best[0] == 1.0 and best[2] >= 3:
                break
        if best and best[0] == 1.0 and best[2] >= 3:
            break

    if not best or best[0] < 0.9:
        print(f"\n  ✗ 最好只到 {best[1]}/{best[2]}（{best[0]:.0%}）" if best else "\n  ✗ 无命中")
        print("    排查：sid 对不对？uid 对不对？流是不是从 connect 第一个字节开始录的？"
              "\n    中间漏包会让密钥流永久错位 —— 检查录制期间有没有失步告警。")
        return 1

    rate, good, tot, prefix, mid, skip = best
    key = make_key(direction, prefix, mid, sid)
    print(f"\n  ★ 命中 {good}/{tot}（{rate:.0%}）")
    print(f"    _-71r = {prefix!r}   中段 = {mid}（level={mid // 100}, "
          f"firstLogin={'是' if mid % 100 == 0 else '否'}）   sid = {sid!r}")
    print(f"    密钥 = {key.decode('utf-8', 'replace')!r}  "
          f"S表{'倒序' if direction == 's2c' else '正序'}  KSA用len-2  i=j=11")
    if skip:
        print(f"    ⚠ 跳过了前 {skip} 条非豁免 body 才对上 —— 录制可能漏了开头")

    rc4 = RC4(key, direction == "s2c")
    outdir = args.stream + ".decrypted"
    if args.write:
        os.makedirs(outdir, exist_ok=True)
    jl = open(os.path.join(outdir, "frames.jsonl"), "w", encoding="utf-8") \
        if args.write else None
    ex = EXEMPT[direction]
    ok = bad = 0
    for i, (off, op, seq, bo, be) in enumerate(frames(buf, start)):
        raw = buf[bo:be]
        if op in ex:
            body, encd = raw, False
        else:
            body, encd = rc4.crypt(raw), True
        good_pb = strength(body) > 0 or not len(body)
        ok += good_pb
        bad += not good_pb
        if jl:
            ops = f"{op:04x}"
            rec = {"i": i, "op": ops, "seq": seq, "off": off,
                   "len": len(body), "enc": encd, "pb": good_pb}
            if SCH:
                rec["msg"] = SCH.name_of(ops)
                d = SCH.decode(body, ops)
                if d is not None:
                    rec["data"] = d
            if "data" not in rec:
                rec["hex"] = body[:200].hex()
            jl.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if jl:
        jl.close()
        print(f"    已写出 {outdir}/frames.jsonl")
    print(f"    全流校验: {ok} 条通过 protobuf 检查，{bad} 条未通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""AVM2 反汇编 —— 把方法体字节码变成可读指令，并把常量池下标解析成实际内容。

对外只有两个函数（dump_class.py / xref.py / extract_proto.py 依赖它们）：

    load(swf_path)      -> ABC          解析 SWF 里的 ABC，带磁盘缓存
    disasm(abc, code)   -> [(addr, text, comment)]

text 形如 "pushstring 65364"（助记符 + 原始操作数），comment 是解析后的内容
（字符串给出 'xxx'，multiname 给出 package:Name）。调用方靠 text.startswith(...)
和 comment 里的子串做匹配，所以这两个字段的格式不能随意改。
"""

import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from abcparse import ABC, extract_abc, _u30, _s32   # noqa: E402

# 操作数类型：
#   -      无操作数
#   u30    普通变长整数
#   str    字符串常量池下标
#   int/uint/dbl  对应常量池
#   mn     multiname 下标
#   ns     namespace 下标
#   cls    类下标
#   meth   方法下标
#   s8/u8  单字节
#   s24    有符号 24 位跳转偏移
#   mn+u30 multiname 加参数个数
OPS = {
    0x01: ("bkpt", ()), 0x02: ("nop", ()), 0x03: ("throw", ()),
    0x04: ("getsuper", ("mn",)), 0x05: ("setsuper", ("mn",)),
    0x06: ("dxns", ("str",)), 0x07: ("dxnslate", ()),
    0x08: ("kill", ("u30",)), 0x09: ("label", ()),
    0x0C: ("ifnlt", ("s24",)), 0x0D: ("ifnle", ("s24",)),
    0x0E: ("ifngt", ("s24",)), 0x0F: ("ifnge", ("s24",)),
    0x10: ("jump", ("s24",)), 0x11: ("iftrue", ("s24",)),
    0x12: ("iffalse", ("s24",)), 0x13: ("ifeq", ("s24",)),
    0x14: ("ifne", ("s24",)), 0x15: ("iflt", ("s24",)),
    0x16: ("ifle", ("s24",)), 0x17: ("ifgt", ("s24",)),
    0x18: ("ifge", ("s24",)), 0x19: ("ifstricteq", ("s24",)),
    0x1A: ("ifstrictne", ("s24",)),
    0x1B: ("lookupswitch", ("switch",)),
    0x1C: ("pushwith", ()), 0x1D: ("popscope", ()),
    0x1E: ("nextname", ()), 0x1F: ("hasnext", ()),
    0x20: ("pushnull", ()), 0x21: ("pushundefined", ()),
    0x23: ("nextvalue", ()), 0x24: ("pushbyte", ("s8",)),
    0x25: ("pushshort", ("u30",)), 0x26: ("pushtrue", ()),
    0x27: ("pushfalse", ()), 0x28: ("pushnan", ()),
    0x29: ("pop", ()), 0x2A: ("dup", ()), 0x2B: ("swap", ()),
    0x2C: ("pushstring", ("str",)), 0x2D: ("pushint", ("int",)),
    0x2E: ("pushuint", ("uint",)), 0x2F: ("pushdouble", ("dbl",)),
    0x30: ("pushscope", ()), 0x31: ("pushnamespace", ("ns",)),
    0x32: ("hasnext2", ("u30", "u30")),
    0x40: ("newfunction", ("meth",)), 0x41: ("call", ("u30",)),
    0x42: ("construct", ("u30",)),
    0x43: ("callmethod", ("u30", "u30")),
    0x44: ("callstatic", ("meth", "u30")),
    0x45: ("callsuper", ("mn", "u30")),
    0x46: ("callproperty", ("mn", "u30")),
    0x47: ("returnvoid", ()), 0x48: ("returnvalue", ()),
    0x49: ("constructsuper", ("u30",)),
    0x4A: ("constructprop", ("mn", "u30")),
    0x4C: ("callproplex", ("mn", "u30")),
    0x4E: ("callsupervoid", ("mn", "u30")),
    0x4F: ("callpropvoid", ("mn", "u30")),
    0x53: ("applytype", ("u30",)), 0x55: ("newobject", ("u30",)),
    0x56: ("newarray", ("u30",)), 0x57: ("newactivation", ()),
    0x58: ("newclass", ("cls",)), 0x59: ("getdescendants", ("mn",)),
    0x5A: ("newcatch", ("u30",)),
    0x5D: ("findpropstrict", ("mn",)), 0x5E: ("findproperty", ("mn",)),
    0x5F: ("finddef", ("mn",)), 0x60: ("getlex", ("mn",)),
    0x61: ("setproperty", ("mn",)), 0x62: ("getlocal", ("u30",)),
    0x63: ("setlocal", ("u30",)), 0x64: ("getglobalscope", ()),
    0x65: ("getscopeobject", ("u8",)), 0x66: ("getproperty", ("mn",)),
    0x68: ("initproperty", ("mn",)), 0x6A: ("deleteproperty", ("mn",)),
    0x6C: ("getslot", ("u30",)), 0x6D: ("setslot", ("u30",)),
    0x6E: ("getglobalslot", ("u30",)), 0x6F: ("setglobalslot", ("u30",)),
    0x70: ("convert_s", ()), 0x71: ("esc_xelem", ()), 0x72: ("esc_xattr", ()),
    0x73: ("convert_i", ()), 0x74: ("convert_u", ()), 0x75: ("convert_d", ()),
    0x76: ("convert_b", ()), 0x77: ("convert_o", ()), 0x78: ("checkfilter", ()),
    0x80: ("coerce", ("mn",)), 0x82: ("coerce_a", ()), 0x85: ("coerce_s", ()),
    0x86: ("astype", ("mn",)), 0x87: ("astypelate", ()),
    0x90: ("negate", ()), 0x91: ("increment", ()), 0x92: ("inclocal", ("u30",)),
    0x93: ("decrement", ()), 0x94: ("declocal", ("u30",)),
    0x95: ("typeof", ()), 0x96: ("not", ()), 0x97: ("bitnot", ()),
    0xA0: ("add", ()), 0xA1: ("subtract", ()), 0xA2: ("multiply", ()),
    0xA3: ("divide", ()), 0xA4: ("modulo", ()), 0xA5: ("lshift", ()),
    0xA6: ("rshift", ()), 0xA7: ("urshift", ()), 0xA8: ("bitand", ()),
    0xA9: ("bitor", ()), 0xAA: ("bitxor", ()), 0xAB: ("equals", ()),
    0xAC: ("strictequals", ()), 0xAD: ("lessthan", ()), 0xAE: ("lessequals", ()),
    0xAF: ("greaterthan", ()), 0xB0: ("greaterequals", ()),
    0xB1: ("instanceof", ()), 0xB2: ("istype", ("mn",)), 0xB3: ("istypelate", ()),
    0xB4: ("in", ()),
    0xC0: ("increment_i", ()), 0xC1: ("decrement_i", ()),
    0xC2: ("inclocal_i", ("u30",)), 0xC3: ("declocal_i", ("u30",)),
    0xC4: ("negate_i", ()), 0xC5: ("add_i", ()), 0xC6: ("subtract_i", ()),
    0xC7: ("multiply_i", ()),
    0xD0: ("getlocal0", ()), 0xD1: ("getlocal1", ()),
    0xD2: ("getlocal2", ()), 0xD3: ("getlocal3", ()),
    0xD4: ("setlocal0", ()), 0xD5: ("setlocal1", ()),
    0xD6: ("setlocal2", ()), 0xD7: ("setlocal3", ()),
    0xEF: ("debug", ("u8", "str", "u8", "u30")),
    0xF0: ("debugline", ("u30",)), 0xF1: ("debugfile", ("str",)),
    0xF2: ("bkptline", ("u30",)),
}


def _s24(b, p):
    v = b[p] | (b[p + 1] << 8) | (b[p + 2] << 16)
    if v & 0x800000:
        v -= 1 << 24
    return v, p + 3


def _short(s, n=60):
    s = s.replace("\n", "\\n").replace("\r", "\\r")
    return s if len(s) <= n else s[:n] + "…"


def disasm(abc, code: bytes):
    """反汇编一段方法体。返回 [(地址, 指令文本, 注释)]。

    遇到未知字节码时停止——混淆器有时会在方法尾部塞垃圾，硬解会产生假指令。
    """
    out = []
    p, n = 0, len(code)
    while p < n:
        addr = p
        op = code[p]
        p += 1
        ent = OPS.get(op)
        if ent is None:
            out.append((addr, f".byte 0x{op:02x}", "未知操作码，停止反汇编"))
            break
        name, sig = ent
        args, notes = [], []
        try:
            for idx, kind in enumerate(sig):
                if kind == "s8":
                    v = code[p] - 256 if code[p] > 127 else code[p]
                    p += 1
                    args.append(str(v))
                elif kind == "u8":
                    v = code[p]
                    p += 1
                    args.append(str(v))
                elif kind == "s24":
                    v, p = _s24(code, p)
                    args.append(str(v))
                    notes.append(f"-> {p + v}")
                elif kind == "switch":
                    base, p = _s24(code, p)
                    cnt, p = _u30(code, p)
                    targets = []
                    for _ in range(cnt + 1):
                        t, p = _s24(code, p)
                        targets.append(addr + t)
                    args.append(str(base))
                    notes.append(f"{cnt + 1} 个分支 -> {targets[:6]}"
                                 + ("…" if len(targets) > 6 else ""))
                elif kind == "u30" and idx > 0 and sig[idx - 1] == "mn":
                    # 形如 callproperty <mn> <argc>：mn 之后紧跟的 u30 是参数个数。
                    # 把它并进方法名注释成 "名字(N 参)" —— extract_proto.py 靠这个格式
                    # 识别 protobuf 的 write_TYPE_* / _-055 注册调用。
                    v, p = _u30(code, p)
                    args.append(str(v))
                    if notes:
                        base = notes[-1].split(":")[-1]
                        notes[-1] = f"{base}({v} 参)"
                else:
                    v, p = _u30(code, p)
                    args.append(str(v))
                    if kind == "str":
                        s = abc.strings[v] if v < len(abc.strings) else "?"
                        notes.append(f"'{_short(s)}'")
                    elif kind == "mn":
                        notes.append(abc.qname(v))
                    elif kind == "int":
                        notes.append(str(abc.ints[v]) if v < len(abc.ints) else "?")
                    elif kind == "uint":
                        notes.append(str(abc.uints[v]) if v < len(abc.uints) else "?")
                    elif kind == "dbl":
                        notes.append(str(abc.doubles[v]) if v < len(abc.doubles) else "?")
                    elif kind == "ns":
                        k, ni = abc.namespaces[v] if v < len(abc.namespaces) else (0, 0)
                        notes.append(f"ns:{abc.strings[ni] if ni < len(abc.strings) else ''}")
                    elif kind == "cls":
                        if v < len(abc.instances):
                            notes.append(abc.qname(abc.instances[v]["name"]))
                    elif kind == "meth":
                        notes.append(f"method#{v}")
        except IndexError:
            out.append((addr, f"{name} <操作数截断>", ""))
            break
        out.append((addr, (name + " " + " ".join(args)).strip(), "  ".join(notes)))
    return out


# ---------------------------------------------------------------- 载入

_CACHE = {}


def load(swf_path: str, use_disk_cache: bool = True):
    """解析 SWF 里最大的那个 ABC 块（就是主程序）。

    13MB 的 ABC 解析一次要几秒，所以带进程内 + 磁盘两级缓存。
    SWF 换了（大小或 mtime 变化）缓存自动失效。
    """
    key = os.path.abspath(swf_path)
    if key in _CACHE:
        return _CACHE[key]

    st = os.stat(swf_path)
    stamp = (st.st_size, int(st.st_mtime))
    cache_file = swf_path + ".abccache"
    if use_disk_cache and os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                saved_stamp, abc = pickle.load(f)
            if saved_stamp == stamp:
                _CACHE[key] = abc
                return abc
        except Exception:
            pass                                  # 缓存坏了就重解

    blocks = extract_abc(swf_path)
    if not blocks:
        raise ValueError(f"{swf_path} 里没有 DoABC 标签")
    abc = ABC(max(blocks, key=len))               # 主程序 = 最大的那块
    if use_disk_cache:
        try:
            with open(cache_file, "wb") as f:
                pickle.dump((stamp, abc), f, protocol=4)
        except Exception:
            pass
    _CACHE[key] = abc
    return abc


if __name__ == "__main__":
    abc = load(sys.argv[1])
    print(f"类 {len(abc.instances):,}  方法 {len(abc.methods):,}  "
          f"方法体 {len(abc.bodies):,}  字符串 {len(abc.strings):,}")
    if len(sys.argv) > 2:                          # 反汇编指定方法号
        mi = int(sys.argv[2])
        for a, t, c in disasm(abc, abc.bodies[mi]):
            print(f"  {a:5d}  {t:<32} {c}")

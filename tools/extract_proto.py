#!/usr/bin/env python3
# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""从 RedWar SWF 还原完整协议：opcode 表 + 带字段号和类型的 .proto。

    python3 extract_proto.py <swf> <输出目录>

三步
----
1. opcode 表：注册方法里有五六百次 注册函数(opcode, 消息类) 调用，
   opcode 是 base+offset 的立即数（2026073102 是 _-1JW::_-67s 里 563 次 _-055）。
2. 真实消息名：类名被 secureSWF 改成了 _-4mF 这种，但 protobuf-as3 生成的
   报错模板 'Bad data format: <消息名>.<字段名> cannot be set twice.' 是字符串
   字面量，混淆器动不了。按类扫这些字符串就能把混淆名映射回真名。
3. 字段号和类型：protobuf-as3 的 writeExternal 里是
   write_TYPE_XXX(output, <tag>, value)，tag 是立即数，同样动不了。
   把 pushbyte/pushshort/pushint 和紧跟的 write_TYPE_* 调用配对即可。

secureSWF 每次编译都重新混淆，注册函数、注册表属性、WriteUtils 里被混淆的
那一半方法，名字每一版都不一样，所以这些都按结构现找（find_registry /
classify_writers），不写死。
"""
import collections
import json
import os
import re
import sys

from disasm import load, disasm, _s24, _u30

BAD = re.compile(r"Bad data format: ([A-Za-z_]\w*)\.(\w+) cannot be set twice\.")
NUMPUSH = ("pushbyte", "pushshort", "pushint", "pushuint")
CALL = re.compile(r"^(.+)\((\d+) 参\)$")
CALL_OPS = ("callpropvoid", "callproperty", "callproplex")


def _short(c):
    """去掉命名空间：'*:_-XI'、'pkg:_-4mF' -> 名字本身。"""
    return (c or "").split(":")[-1]


def _calls(lines):
    """方法体里的调用，[(函数名, 参数个数)]。"""
    out = []
    for _, t, c in lines:
        m = CALL.match(c or "")
        if m and t.split()[0] in CALL_OPS:
            out.append((_short(m.group(1)), int(m.group(2))))
    return out


def find_registry(abc):
    """找 opcode 注册函数和注册表属性的混淆名。

    注册方法里同一个 2 参函数要调五六百次，全库没有第二个方法接近这个量
    （第二名不到 300）。注册表属性是该方法里出现最多的 getproperty，
    每次注册都先取它。返回 (注册函数名, 注册表属性名)。
    """
    best = (0, None, None)
    for mi, code in abc.bodies.items():
        cnt = collections.Counter(fn for fn, n in _calls(disasm(abc, code)) if n == 2)
        if cnt:
            fn, n = cnt.most_common(1)[0]
            if n > best[0]:
                best = (n, mi, fn)
    _, mi, fn = best
    if mi is None:
        return None, None
    props = collections.Counter(_short(c) for _, t, c in disasm(abc, abc.bodies[mi])
                                if t.startswith("getproperty"))
    return fn, (props.most_common(1)[0][0] if props else None)


def find_named_opcodes(abc):
    """常量类里的 C2S_<消息名> / S2C_<消息名> = opcode（2026102102 起才有）。

    静态构造里是 pushshort <opcode>; initproperty C2S_RceHeartbeat 这样一对一对的。
    返回 {常量名: opcode}，没有这个类就是空字典。
    """
    out = {}
    for idx, inst in enumerate(abc.instances):
        cls = abc.classes[idx]
        if not any(str(n).startswith(("C2S_", "S2C_")) for n, k, mi in cls["traits"]):
            continue
        last = None
        for a, t, c in disasm(abc, abc.bodies.get(cls["cinit"], b"")):
            v = num_of(t)
            if v is not None:
                last = v
            elif t.startswith(("initproperty", "setproperty")) \
                    and _short(c).startswith(("C2S_", "S2C_")) and last is not None:
                out[_short(c)] = last
    return out


def find_exempt(abc, named):
    """Transport 静态构造里的两个加密豁免数组，返回 {"c2s": [opcode], "s2c": [opcode]}。

    2026102102 起豁免名单写成 [C2S_RceHeartbeat, C2S_RceInit, ...] 这样的常量数组，
    Send / 收包处用 indexOf 查。更早的版本是直接比较立即数，这里认不出，返回 None。
    """
    for idx, inst in enumerate(abc.instances):
        if not abc.qname(inst["name"]).endswith(":Transport"):
            continue
        out, pending = {}, []
        for a, t, c in disasm(abc, abc.bodies.get(abc.classes[idx]["cinit"], b"")):
            if t.startswith("getproperty") and _short(c) in named:
                pending.append(_short(c))
            elif t.startswith("newarray"):
                n = int(t.split()[1])
                items, pending = pending[-n:] if n else [], []
                if n and len(items) == n:
                    side = {i[:3] for i in items}
                    if len(side) == 1:
                        out[side.pop().lower()] = [named[i] for i in items]
        return out if set(out) == {"c2s", "s2c"} else None
    return None


def classify_writers(abc):
    """认出 protobuf-as3 WriteUtils 里被混淆的方法，返回 (writeTag 名, {方法名: 类型})。

    WriteUtils 就是带 write_TYPE_* 静态方法的那个类。被混淆的那一半靠方法体里
    调用的未混淆函数认（混淆器塞的假分支会掺进移位/按位运算，只能看调用）：
      writeTag  三参，只调一次 write_TYPE_UINT32
      double/float/string/bytes/message  分别调 writeDouble/writeFloat/
                                         writeUTFBytes/writeBytes/writeToBuffer
      bool      两参，只调 writeByte        enum  两参，只调 write_TYPE_INT32
      group     三参，只调两个开块/收块的混淆函数
    三参里还有一个 64 位 varint 辅助函数（调 writeByte），它不是字段写入，跳过。
    """
    tag, types = None, {}
    for idx, inst in enumerate(abc.instances):
        traits = abc.classes[idx]["traits"]
        if not any(str(n).startswith("write_TYPE_") for n, k, mi in traits):
            continue
        for n, k, mi in traits:
            if mi is None or mi not in abc.bodies or not str(n).startswith("_-"):
                continue
            called = [fn for fn, _ in _calls(disasm(abc, abc.bodies[mi]))]
            nparams = len(abc.methods[mi]["params"])
            known = [fn for fn in called if not fn.startswith("_-")]
            for lib, ty in (("writeDouble", "double"), ("writeFloat", "float"),
                            ("writeUTFBytes", "string"), ("writeBytes", "bytes"),
                            ("writeToBuffer", "message")):
                if lib in called:
                    types[n] = ty
                    break
            else:
                if nparams == 2 and called == ["writeByte"]:
                    types[n] = "bool"
                elif nparams == 2 and called == ["write_TYPE_INT32"]:
                    types[n] = "enum"
                elif nparams == 3 and called == ["write_TYPE_UINT32"]:
                    tag = n
                elif nparams == 3 and called and not known:
                    types[n] = "group"
    return tag, types


def _switch_table(code, lines):
    """取 readExternal 里 lookupswitch 的跳转表：(default 地址, [按序号的目标地址], 全部边界)。

    AVM2 的 lookupswitch 是 default 偏移 + case_count + (case_count+1) 个目标。
    没有 lookupswitch 返回 None。
    """
    for addr, txt, _ in lines:
        if not txt.startswith("lookupswitch"):
            continue
        p = addr + 1
        dflt, p = _s24(code, p)
        cnt, p = _u30(code, p)
        out = []
        for _ in range(cnt + 1):
            t, p = _s24(code, p)
            out.append(addr + t)
        return addr + dflt, out, sorted(set(out))
    return None


# ---------- switch 比较链的模拟执行 ----------
#
# as3 编译器把 switch (tag.number) 编成一串比较：依次拿每个 case 的值和字段号做
# ifstrictne，命中就压入该 case 的序号，再跳去 lookupswitch。所以 lookupswitch 的
# 序号是 case 在源码里的先后，不是字段号。2026102102 起字段号被打乱，源码里 case 仍按
# 声明顺序排（RseSuperStormOpt 依次是 5/4/6/2/3/1），按位置配对就全错了。
# secureSWF 又往比较链里掺了不透明谓词：方法开头把两个局部变量赋成常量真/假，
# 再据此造出永不执行的假分支，顺着指令读会读错，只能真的跑一遍。

class _Stop(Exception):
    """碰到模拟不了的指令或取值。"""


_OBJ = object()      # this、参数这类对象：转布尔为真，不能参与运算

_JUMPS = {
    "ifstrictne": lambda a, b: not _same(a, b), "ifne": lambda a, b: not _same(a, b),
    "ifstricteq": lambda a, b: _same(a, b), "ifeq": lambda a, b: _same(a, b),
    "iflt": lambda a, b: _num(a) < _num(b), "ifle": lambda a, b: _num(a) <= _num(b),
    "ifgt": lambda a, b: _num(a) > _num(b), "ifge": lambda a, b: _num(a) >= _num(b),
    "ifnlt": lambda a, b: not _num(a) < _num(b), "ifnle": lambda a, b: not _num(a) <= _num(b),
    "ifngt": lambda a, b: not _num(a) > _num(b), "ifnge": lambda a, b: not _num(a) >= _num(b),
}
_ARITH = {
    "add": lambda a, b: a + b, "add_i": lambda a, b: a + b,
    "subtract": lambda a, b: a - b, "subtract_i": lambda a, b: a - b,
    "multiply": lambda a, b: a * b, "multiply_i": lambda a, b: a * b,
    "bitand": lambda a, b: a & b, "bitor": lambda a, b: a | b,
    "bitxor": lambda a, b: a ^ b, "lshift": lambda a, b: a << (b & 31),
    "rshift": lambda a, b: a >> (b & 31), "urshift": lambda a, b: (a & 0xFFFFFFFF) >> (b & 31),
}


def _same(a, b):
    if isinstance(a, bool) != isinstance(b, bool):   # 严格相等：true 不等于 1
        return False
    return a == b


def _num(v):
    if isinstance(v, bool) or v is _OBJ or v is None:
        raise _Stop
    return v


def _truth(v):
    if v is _OBJ:
        return True
    if v is None:
        return False
    return bool(v)


def _local_no(txt):
    """'getlocal2' / 'setlocal 10' / 'kill 9' -> 局部变量号。"""
    op = txt.split()
    return int(op[1]) if len(op) > 1 else int(op[0][-1])


def _run(lines, at, start, local, stack=(), limit=4000):
    """从 start 地址起模拟执行，跑到 lookupswitch 返回栈顶（分支序号）。

    只认比较链和方法开头会用到的指令，碰到别的就停：跑方法开头时停下是正常的
    （要的只是 local 里带出来的不透明谓词取值），返回 None。local 就地修改。
    """
    stack, i = list(stack), at.get(start)
    try:
        for _ in range(limit):
            if i is None or i >= len(lines):
                return None
            addr, txt, c = lines[i]
            op = txt.split()[0]
            i += 1
            if op in ("pushbyte", "pushshort"):
                stack.append(int(txt.split()[1]))
            elif op in ("pushint", "pushuint"):
                stack.append(int(c))
            elif op in ("pushtrue", "pushfalse"):
                stack.append(op == "pushtrue")
            elif op in ("pushnull", "pushundefined"):
                stack.append(None)
            elif op == "dup":
                stack.append(stack[-1])
            elif op == "pop" or op == "pushscope":
                stack.pop()
            elif op == "swap":
                stack[-1], stack[-2] = stack[-2], stack[-1]
            elif op.startswith("getlocal"):
                stack.append(local.get(_local_no(txt), _OBJ))
            elif op.startswith("setlocal"):
                local[_local_no(txt)] = stack.pop()
            elif op == "kill":
                local[_local_no(txt)] = None
            elif op in ("inclocal", "inclocal_i", "declocal", "declocal_i"):
                n = _local_no(txt)
                local[n] = _num(local.get(n)) + (1 if op.startswith("inc") else -1)
            elif op == "convert_b":
                stack.append(_truth(stack.pop()))
            elif op == "not":
                stack.append(not _truth(stack.pop()))
            elif op in ("convert_i", "convert_u", "coerce_i", "coerce_u"):
                stack.append(int(_num(stack.pop())))
            elif op in ("coerce", "coerce_a", "label", "nop"):
                pass
            elif op in ("increment", "increment_i", "decrement", "decrement_i"):
                stack.append(_num(stack.pop()) + (1 if op.startswith("inc") else -1))
            elif op in ("negate", "negate_i"):
                stack.append(-_num(stack.pop()))
            elif op in _ARITH:
                b, a = _num(stack.pop()), _num(stack.pop())
                stack.append(_ARITH[op](a, b))
            elif op in ("equals", "strictequals"):
                b, a = stack.pop(), stack.pop()
                stack.append(_same(a, b))
            elif op == "jump":
                i = at.get(int(c.split("->")[1]))
            elif op in ("iftrue", "iffalse"):
                if _truth(stack.pop()) == (op == "iftrue"):
                    i = at.get(int(c.split("->")[1]))
            elif op in _JUMPS:
                b, a = stack.pop(), stack.pop()
                if _JUMPS[op](a, b):
                    i = at.get(int(c.split("->")[1]))
            elif op == "lookupswitch":
                return _num(stack.pop())
            else:
                return None
    except (_Stop, IndexError, ValueError):
        return None
    return None


def _case_index(lines, tags):
    """switch 比较链：{字段号: lookupswitch 序号}。跑不通或两个字段撞到同一序号返回 None。"""
    at = {a: i for i, (a, _, _) in enumerate(lines)}
    # 从取出字段号的下一条起跑，字段号放在栈顶（中间可能被插了 jump，不能假定紧跟 setlocal）
    start = next((lines[i + 1][0] for i, (a, t, c) in enumerate(lines[:-1])
                  if t.startswith("getproperty") and _short(c) == "number"), None)
    if start is None:
        return None
    base = {0: _OBJ}
    _run(lines, at, 0, base)              # 方法开头：带出不透明谓词的取值
    out = {}
    for tag in tags:
        k = _run(lines, at, start, dict(base), stack=[tag])
        if k is None:
            return None
        out[tag] = k
    return out if len(set(out.values())) == len(out) else None


def _out(outdir, name):
    """产物一律 UTF-8 + LF，在 Windows 上生成也和仓库里的逐字节一致。"""
    return open(os.path.join(outdir, name), "w", encoding="utf-8", newline="\n")


def num_of(t):
    p = t.split()
    if p[0] in NUMPUSH and len(p) > 1:
        try:
            return int(p[1])
        except ValueError:
            return None
    return None


def main():
    swf, outdir = sys.argv[1], sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    abc = load(swf)

    # 类索引：qname -> (instance, class), 以及方法归属
    by_q, meth_of = {}, {}
    for idx, inst in enumerate(abc.instances):
        q = abc.qname(inst["name"])
        cls = abc.classes[idx]
        by_q[q] = (inst, cls)
        mids = [inst["iinit"], cls["cinit"]]
        mids += [mi for _, k, mi in inst["traits"] if mi is not None]
        mids += [mi for _, k, mi in cls["traits"] if mi is not None]
        for mi in mids:
            meth_of.setdefault(mi, q)

    # ---------- 1. opcode 表 ----------
    reg_fn, reg_prop = find_registry(abc)
    reg_call = f"{reg_fn}(2 参)"
    reg = {}
    for mi, code in abc.bodies.items():
        L = disasm(abc, code)
        if not any(_short(c) == reg_call for _, _, c in L):
            continue
        nums, klass = [], None
        for a, t, c in L:
            if _short(c) == reg_call:
                if klass and nums:
                    op = sum(nums)
                    if 0x0100 <= op <= 0x0FFF:
                        reg.setdefault(op, klass)
                nums, klass = [], None
                continue
            v = num_of(t)
            if v is not None:
                nums.append(v)
            elif t.startswith("getlex") and "protocol:" in c:
                klass = c
            elif _short(c) == reg_prop:  # 每次注册以取注册表开头，重置窗口
                nums, klass = [], None
    print(f"1) opcode 表: {len(reg)} 条（注册函数 {reg_fn}，注册表 {reg_prop}）")

    # ---------- 2. 混淆类名 -> 真实消息名 ----------
    real = {}
    for q, (inst, cls) in by_q.items():
        if "protocol:" not in q and ":Message" not in q:
            pass
        mids = [inst["iinit"]] + [mi for _, k, mi in inst["traits"] if mi is not None]
        names = set()
        for mi in mids:
            code = abc.bodies.get(mi)
            if not code:
                continue
            for a, t, c in disasm(abc, code):
                if t.startswith("pushstring"):
                    s = abc.strings[int(t.split()[1])]
                    m = BAD.match(s)
                    if m:
                        names.add(m.group(1))
        if len(names) == 1:
            real[q] = names.pop()
        elif names:                      # 内嵌类共用方法体时可能出现多个
            real[q] = sorted(names)[0]
    print(f"2) 还原真实消息名: {len(real)} 个类")

    # ---------- 3. writeToBuffer 里的字段号和类型 ----------
    # protobuf-as3 的 WriteUtils 有一半方法被混淆了，由 classify_writers 现认。
    # 2026073102 里 WriteUtils 是 _-3EJ：
    #   _-9g=writeTag  _-1lP=double  _-6pT=float  _-30c=bool(writeByte)
    #   _-yF=string(writeUTFBytes)  _-6YX=bytes(length+writeBytes)  _-jt=enum
    #   _-1CD=message  _-AL=group/packed（都调 _-2S5 开块）
    write_tag, obf_types = classify_writers(abc)
    print(f"   WriteUtils：writeTag={write_tag}，" + "、".join(
        f"{n}={ty}" for n, ty in sorted(obf_types.items(), key=lambda kv: kv[1])))
    TYPES = {
        **obf_types,
        "write_TYPE_INT64": "int64", "write_TYPE_UINT64": "uint64",
        "write_TYPE_INT32": "int32", "write_TYPE_FIXED64": "fixed64",
        "write_TYPE_FIXED32": "fixed32", "write_TYPE_UINT32": "uint32",
        "write_TYPE_SFIXED32": "sfixed32", "write_TYPE_SFIXED64": "sfixed64",
        "write_TYPE_SINT32": "sint32", "write_TYPE_SINT64": "sint64",
    }
    schema, slots, decl = {}, {}, {}
    for q, (inst, cls) in by_q.items():
        we = next((mi for n, k, mi in inst["traits"]
                   if n in ("writeToBuffer", "writeExternal") and mi is not None), None)
        if we is None or we not in abc.bodies:
            continue
        flds, last_num, cur_tag = [], None, None
        slot, want_slot = {}, False
        for a, t, c in disasm(abc, abc.bodies[we]):
            v = num_of(t)
            if v is not None:
                last_num = v
                continue
            # writeTag 之后紧跟的第一个 getproperty 就是这个字段的存储槽，
            # 形如 write(output, this._type)。repeated 字段没有报错模板，
            # 只能靠这个槽名兜底。
            if want_slot and t.startswith("getproperty"):
                slot[cur_tag] = _short(c)        # 新版带 "*:" 前缀
                want_slot = False
                continue
            m = CALL.match(c or "")
            if not m:
                continue
            fn = _short(m.group(1))
            if fn == write_tag:                  # writeTag(output, wiretype, tag)
                cur_tag, last_num = last_num, None
                want_slot = True
            elif cur_tag is not None and fn in TYPES:
                flds.append((cur_tag, TYPES[fn]))
                cur_tag, want_slot = None, False
        if flds:
            schema[q] = sorted(set(flds))
            slots[q] = slot
            # writeToBuffer 按 .proto 里的声明顺序写字段。字段号可以被打乱，
            # 这个先后不会变，新旧版本的字段靠它对应（见 tools/wiremap.py）
            decl[q] = []
            for tag, ty in flds:
                if all(tag != t for t, _ in decl[q]):
                    decl[q].append((tag, ty))

    # 字段名：readExternal 里每个字段对应 lookupswitch 的一个分支，
    # 分支内的报错模板就是该字段的真名。
    #
    # 早先这里是把模板名按出现顺序和 tag 顺序**位置对齐**的，那是错的：
    # repeated 字段不会生成 "cannot be set twice" 模板（它本来就允许设置多次），
    # 于是每有一个 repeated 字段，后面所有名字就整体前移一位。
    # RseHeroVisit 的 3~18 号字段因此全部错位两格 —— freeVisitCnt 被叫成
    # hasCreditVisit，finishVisitTime 被叫成 getType。
    #
    # 现在按分支配对：模拟执行 switch 前面的比较链，求出每个字段号落到
    # lookupswitch 的第几个分支（见 _case_index），再取该分支里的模板。
    # 后来又栽过一次"按位置配对"：2026102102 起字段号被打乱，case 的先后
    # 不再等于字段号的大小。跑不通就整条放弃，宁可留 fieldN 也不要产出错位的名字。
    # repeated 字段的分支没有模板，留空后由 writeToBuffer 的存储槽名兜底。
    #
    # 同一个分支里 new 出来的消息类，就是 message 字段装的嵌套类型。
    fieldnames, nested, skipped = {}, {}, 0
    for q, (inst, cls) in by_q.items():
        if q not in schema:
            continue
        tags = sorted(t for t, _ in schema[q])
        msg_tags = {t for t, ty in schema[q] if ty == "message"}
        names, kinds = {}, {}
        rx = next((mi for n, k, mi in inst["traits"]
                   if n == "readExternal" and mi is not None), None)
        if rx and rx in abc.bodies:
            code = abc.bodies[rx]
            lines = disasm(abc, code)
            table = _switch_table(code, lines)
            order = _case_index(lines, tags) if table else None
            if order:
                dflt, targets, bounds = table
                for tag, k in order.items():
                    if not 0 <= k < len(targets) or targets[k] == dflt:
                        continue
                    start = targets[k]
                    end = next((b for b in bounds if b > start), 1 << 30)
                    for a, t, c in lines:
                        if not start <= a < end:
                            continue
                        # new X() 编成 findpropstrict X; constructprop X，前一条带完整类名
                        if tag in msg_tags and tag not in kinds \
                                and t.startswith(("findpropstrict", "getlex")) and c in schema:
                            kinds[tag] = c
                        if t.startswith("pushstring") and tag not in names:
                            m = BAD.match(abc.strings[int(t.split()[1])])
                            if m:
                                names[tag] = m.group(2)
            elif table:
                skipped += 1
        nested[q] = kinds
        # repeated 字段没有模板，退回 writeToBuffer 里的存储槽名。
        # 槽名要么是可读的真名（freeVisitCnt），要么还是混淆名，后者当没有。
        for tag in tags:
            if tag in names:
                continue
            s = slots.get(q, {}).get(tag, "")
            if s and not s.startswith("_-"):
                names[tag] = s[1:] if s.startswith("_") else s
        fieldnames[q] = names
    ok = sum(1 for q in schema if len(fieldnames.get(q, {})) == len(schema[q]))
    print(f"3) 提到字段号的类 {len(schema)} 个，字段名完全还原 {ok} 个"
          f"（{skipped} 个 switch 跑不通，已放弃猜名）")

    # ---------- 输出 ----------
    opmap = {}
    for op, q in sorted(reg.items()):
        opmap[f"{op:04x}"] = {"class": q, "name": real.get(q, "")}
    with _out(outdir, "opcodes.json") as f:
        json.dump(opmap, f, ensure_ascii=False, indent=1)

    lines = ['syntax = "proto2";', "", "// 由 extract_proto.py 从 RedWar SWF 还原",
             "// 字段号/类型来自 writeExternal 的立即数，",
             "// 字段名来自 readExternal 中该字段 case 分支里的报错模板", ""]
    named = 0
    for q, flds in sorted(schema.items(), key=lambda kv: real.get(kv[0], kv[0])):
        name = real.get(q)
        if not name:
            continue
        named += 1
        fnames = fieldnames.get(q, {})
        op = next((f"{o:04x}" for o, k in reg.items() if k == q), None)
        lines.append(f"// {q}" + (f"   opcode 0x{op}" if op else ""))
        lines.append(f"message {name} {{")
        for tag, ty in sorted(set(flds)):
            fn = fnames.get(tag) or f"field{tag}"
            lines.append(f"  optional {ty.replace('TYPE_', '').lower():<10} {fn} = {tag};")
        lines.append("}")
        lines.append("")
    with _out(outdir, "redwar.proto") as f:
        f.write("\n".join(lines))
    # 给工具用的紧凑 schema：opcode -> {name, fields:{tag:[名字, 类型]}}
    compact = {}
    for op, q in sorted(reg.items()):
        flds = schema.get(q, [])
        names = fieldnames.get(q, {})
        fmap = {}
        for tag, ty in flds:
            fmap[str(tag)] = [names.get(tag) or f"field{tag}", ty]
        compact[f"{op:04x}"] = {"name": real.get(q) or q.split(":")[-1],
                                "cls": q.split(":")[-1], "fields": fmap}
    with _out(outdir, "schema.json") as f:
        json.dump(compact, f, ensure_ascii=False, indent=0)
    # 给 tools/build_wiremap.py 用的完整结构：每个消息类按声明顺序列出
    # [字段号, 类型, 名字或 null, 嵌套类或 null]，嵌套消息也在内
    consts = find_named_opcodes(abc)
    exempt = find_exempt(abc, consts)
    structure = {
        "opcodes": {f"{op:04x}": q for op, q in sorted(reg.items())},
        "named": {k: f"{v:04x}" for k, v in sorted(consts.items())},
        "exempt": exempt and {d: [f"{o:04x}" for o in ops] for d, ops in exempt.items()},
        "classes": {q: {"name": real.get(q),
                        "fields": [[tag, ty, fieldnames.get(q, {}).get(tag),
                                    nested.get(q, {}).get(tag)] for tag, ty in decl[q]]}
                    for q in sorted(schema)},
    }
    with _out(outdir, "structure.json") as f:
        json.dump(structure, f, ensure_ascii=False, indent=0)
    unknown = sum(1 for q in schema for t, ty in schema[q]
                  if ty == "message" and t not in nested.get(q, {}))
    print(f"写出 {outdir}/opcodes.json、{outdir}/redwar.proto（{named} 个具名消息）、"
          f"{outdir}/schema.json 和 {outdir}/structure.json"
          f"（{unknown} 个 message 字段没认出嵌套类型）")

    # 抽样自检
    print("\n抽样（opcode -> 消息名）:")
    for op in sorted(reg)[:5] + [o for o in (0x0268, 0x027c, 0x0215, 0x0283) if o in reg]:
        print(f"  {op:04x} {reg[op]:<58} {real.get(reg[op], '?')}")


if __name__ == "__main__":
    import _console
    _console.utf8()
    main()

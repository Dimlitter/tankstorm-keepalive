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
1. opcode 表：com.sincetimes.redwar.game.comnunicate.protocol:_-1JW::_-67s 里
   有 563 次 _-055(opcode, 消息类) 注册调用，opcode 是 base+offset 的立即数。
2. 真实消息名：类名被 secureSWF 改成了 _-4mF 这种，但 protobuf-as3 生成的
   报错模板 'Bad data format: <消息名>.<字段名> cannot be set twice.' 是字符串
   字面量，混淆器动不了。按类扫这些字符串就能把混淆名映射回真名。
3. 字段号和类型：protobuf-as3 的 writeExternal 里是
   write_TYPE_XXX(output, <tag>, value)，tag 是立即数，同样动不了。
   把 pushbyte/pushshort/pushint 和紧跟的 write_TYPE_* 调用配对即可。
"""
import json
import os
import re
import sys

from disasm import load, disasm, _s24, _u30

BAD = re.compile(r"Bad data format: ([A-Za-z_]\w*)\.(\w+) cannot be set twice\.")
NUMPUSH = ("pushbyte", "pushshort", "pushint", "pushuint")


def _switch_cases(abc, code, lines):
    """取 readExternal 里 lookupswitch 的**非 default** 分支偏移，按 case 序排列。

    AVM2 的 lookupswitch 是 default 偏移 + case_count + (case_count+1) 个目标。
    编译器会把 switch 的取值归一化到 0 起始，所以 case 序号和字段号之间差多少
    取决于该消息最小的字段号 —— 不能假定差 1（RceAdmiralVisit 的字段从 2 开始）。
    字段号有空洞时，那些 case 直接指向 default。
    因此这里只把"真正的分支"按序取出，交给调用方和升序字段号逐个配对。
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
        return [t for t in out if t != addr + dflt], sorted(set(out))
    return [], []


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
    reg = {}
    for mi, code in abc.bodies.items():
        L = disasm(abc, code)
        if not any(c == "_-055(2 参)" for _, _, c in L):
            continue
        nums, klass = [], None
        for a, t, c in L:
            if c == "_-055(2 参)":
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
            elif "_-5sY" in c:          # 每次注册以取注册表开头，重置窗口
                nums, klass = [], None
    print(f"1) opcode 表: {len(reg)} 条")

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
    # protobuf-as3 的 WriteUtils(_-3EJ) 有一半方法被混淆了，靠方法体实测认出来：
    #   _-9g=writeTag  _-1lP=double  _-6pT=float  _-30c=bool(writeByte)
    #   _-yF=string(writeUTFBytes)  _-6YX=bytes(length+writeBytes)  _-jt=enum
    #   _-1CD=message  _-AL=group/packed（都调 _-2S5 开块）
    TYPES = {
        "_-1lP": "double", "_-6pT": "float", "_-30c": "bool", "_-yF": "string",
        "_-6YX": "bytes", "_-jt": "enum", "_-1CD": "message", "_-AL": "group",
        "write_TYPE_INT64": "int64", "write_TYPE_UINT64": "uint64",
        "write_TYPE_INT32": "int32", "write_TYPE_FIXED64": "fixed64",
        "write_TYPE_FIXED32": "fixed32", "write_TYPE_UINT32": "uint32",
        "write_TYPE_SFIXED32": "sfixed32", "write_TYPE_SFIXED64": "sfixed64",
        "write_TYPE_SINT32": "sint32", "write_TYPE_SINT64": "sint64",
    }
    CALL = re.compile(r"^([A-Za-z_][\w-]*|_-[\w-]+)\(")
    schema, slots = {}, {}
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
                slot[cur_tag] = c
                want_slot = False
                continue
            m = CALL.match(c or "")
            if not m:
                continue
            fn = m.group(1)
            if fn == "_-9g":                     # writeTag(output, wiretype, tag)
                cur_tag, last_num = last_num, None
                want_slot = True
            elif cur_tag is not None and fn in TYPES:
                flds.append((cur_tag, TYPES[fn]))
                cur_tag, want_slot = None, False
        if flds:
            schema[q] = sorted(set(flds))
            slots[q] = slot

    # 字段名：readExternal 里每个字段对应 lookupswitch 的一个分支，
    # 分支内的报错模板就是该字段的真名。
    #
    # 早先这里是把模板名按出现顺序和 tag 顺序**位置对齐**的，那是错的：
    # repeated 字段不会生成 "cannot be set twice" 模板（它本来就允许设置多次），
    # 于是每有一个 repeated 字段，后面所有名字就整体前移一位。
    # RseHeroVisit 的 3~18 号字段因此全部错位两格 —— freeVisitCnt 被叫成
    # hasCreditVisit，finishVisitTime 被叫成 getType。
    #
    # 现在按分支配对：分支数必须和字段数一一相等才采用，对不上就整条放弃，
    # 宁可留 fieldN 也不要再产出错位的名字。repeated 字段的分支没有模板，
    # 留空后由 writeToBuffer 的存储槽名兜底。
    fieldnames, skipped = {}, 0
    for q, (inst, cls) in by_q.items():
        if q not in schema:
            continue
        tags = sorted(t for t, _ in schema[q])
        names = {}
        rx = next((mi for n, k, mi in inst["traits"]
                   if n == "readExternal" and mi is not None), None)
        if rx and rx in abc.bodies:
            code = abc.bodies[rx]
            lines = disasm(abc, code)
            cases, bounds = _switch_cases(abc, code, lines)
            if len(cases) == len(tags):
                for tag, start in zip(tags, cases):
                    end = next((b for b in bounds if b > start), 1 << 30)
                    for a, t, c in lines:
                        if not start <= a < end:
                            continue
                        if t.startswith("pushstring"):
                            m = BAD.match(abc.strings[int(t.split()[1])])
                            if m:
                                names[tag] = m.group(2)
                                break
            elif cases:
                skipped += 1
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
          f"（{skipped} 个分支数对不上，已放弃猜名）")

    # ---------- 输出 ----------
    opmap = {}
    for op, q in sorted(reg.items()):
        opmap[f"{op:04x}"] = {"class": q, "name": real.get(q, "")}
    json.dump(opmap, open(os.path.join(outdir, "opcodes.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

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
    open(os.path.join(outdir, "redwar.proto"), "w", encoding="utf-8").write(
        "\n".join(lines))
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
    json.dump(compact, open(os.path.join(outdir, "schema.json"), "w",
                            encoding="utf-8"), ensure_ascii=False, indent=0)
    print(f"写出 {outdir}/opcodes.json、{outdir}/redwar.proto（{named} 个具名消息）"
          f"和 {outdir}/schema.json")

    # 抽样自检
    print("\n抽样（opcode -> 消息名）:")
    for op in sorted(reg)[:5] + [o for o in (0x0268, 0x027c, 0x0215, 0x0283) if o in reg]:
        print(f"  {op:04x} {reg[op]:<58} {real.get(reg[op], '?')}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
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
from collections import defaultdict

from disasm import load, disasm

BAD = re.compile(r"Bad data format: ([A-Za-z_]\w*)\.(\w+) cannot be set twice\.")
NUMPUSH = ("pushbyte", "pushshort", "pushint", "pushuint")


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
    real, fields_seen = {}, defaultdict(list)
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
                        fields_seen[q].append(m.group(2))
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
    schema = {}
    for q, (inst, cls) in by_q.items():
        we = next((mi for n, k, mi in inst["traits"]
                   if n in ("writeToBuffer", "writeExternal") and mi is not None), None)
        if we is None or we not in abc.bodies:
            continue
        flds, last_num, cur_tag = [], None, None
        for a, t, c in disasm(abc, abc.bodies[we]):
            v = num_of(t)
            if v is not None:
                last_num = v
                continue
            m = CALL.match(c or "")
            if not m:
                continue
            fn = m.group(1)
            if fn == "_-9g":                     # writeTag(output, wiretype, tag)
                cur_tag, last_num = last_num, None
            elif cur_tag is not None and fn in TYPES:
                flds.append((cur_tag, TYPES[fn]))
                cur_tag = None
        if flds:
            schema[q] = sorted(set(flds))

    # 字段名：readExternal 的报错模板按 tag 顺序出现；数量对不上就退回 setter 名
    fieldnames = {}
    for q, (inst, cls) in by_q.items():
        if q not in schema:
            continue
        bad = []
        rx = next((mi for n, k, mi in inst["traits"]
                   if n == "readExternal" and mi is not None), None)
        if rx and rx in abc.bodies:
            for a, t, c in disasm(abc, abc.bodies[rx]):
                if t.startswith("pushstring"):
                    m = BAD.match(abc.strings[int(t.split()[1])])
                    if m:
                        bad.append(m.group(2))
        setters = [n for n, k, mi in inst["traits"] if k == "setter"]
        n_f = len(schema[q])
        fieldnames[q] = bad if len(bad) == n_f else (setters if len(setters) == n_f else bad)
    ok = sum(1 for q in schema if len(fieldnames.get(q, [])) == len(schema[q]))
    print(f"3) 提到字段号的类 {len(schema)} 个，字段名完全对齐 {ok} 个")

    # ---------- 输出 ----------
    opmap = {}
    for op, q in sorted(reg.items()):
        opmap[f"{op:04x}"] = {"class": q, "name": real.get(q, "")}
    json.dump(opmap, open(os.path.join(outdir, "opcodes.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    lines = ['syntax = "proto2";', "", "// 由 extract_proto.py 从 RedWar SWF 还原",
             "// 字段号/类型来自 writeExternal 的立即数，字段名来自 protobuf-as3 的报错模板", ""]
    named = 0
    for q, flds in sorted(schema.items(), key=lambda kv: real.get(kv[0], kv[0])):
        name = real.get(q)
        if not name:
            continue
        named += 1
        fnames = fieldnames.get(q) or fields_seen.get(q, [])
        op = next((f"{o:04x}" for o, k in reg.items() if k == q), None)
        lines.append(f"// {q}" + (f"   opcode 0x{op}" if op else ""))
        lines.append(f"message {name} {{")
        for i, (tag, ty) in enumerate(sorted(set(flds))):
            fn = fnames[i] if i < len(fnames) else f"field{tag}"
            lines.append(f"  optional {ty.replace('TYPE_', '').lower():<10} {fn} = {tag};")
        lines.append("}")
        lines.append("")
    open(os.path.join(outdir, "redwar.proto"), "w", encoding="utf-8").write(
        "\n".join(lines))
    # 给工具用的紧凑 schema：opcode -> {name, fields:{tag:[名字, 类型]}}
    compact = {}
    for op, q in sorted(reg.items()):
        flds = schema.get(q, [])
        names = fieldnames.get(q, [])
        fmap = {}
        for i, (tag, ty) in enumerate(flds):
            fmap[str(tag)] = [names[i] if i < len(names) else f"field{tag}", ty]
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

#!/usr/bin/env python3
# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""生成新游戏版本到本项目编号的映射（tankstorm/wiremap.json）。

    python tools/extract_proto.py <新的.swf> out
    python tools/build_wiremap.py docs/structure.json out/structure.json <版本号>

2026102102 起，游戏每次更新都可能把 opcode 和字段号整体打乱。本项目的代码、
schema.json、任务表一律按 docs/structure.json 那一版（2026101403）的编号写，
连接时由 tankstorm/wiremap.py 按这里生成的表在收发两头翻译。

怎么对应
--------
* 消息：先按真名（报错模板、没被混淆的类名、新版常量类 C2S_/S2C_ 里的名字），
  再顺着已对上的消息里 message 字段的嵌套类型往下认，最后对剩下的按
  "字段类型 + 名字 + 嵌套类型"的结构签名认，签名不唯一的放弃。
* 字段：writeToBuffer 按 .proto 的声明顺序写字段，字段号打乱后这个先后不变，
  所以按位置对。同一位置名字或类型对不上，说明这条假设在这个类上不成立，直接报错。
* 加密豁免：本项目的豁免名单（tankstorm/crypto.py）映射过去，必须和新版 SWF
  里那两个豁免数组一模一样，否则报错。这一条把心跳、握手、广播几条关键消息全核了一遍。

认不出来的消息不进表：收到时记成 8xxx 的伪 opcode（不和本项目编号撞车），
要发时直接拒发。宁可少认，不能认错 —— 错一个 opcode 就可能把请求发成购买。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from tankstorm import crypto                 # noqa: E402  本项目的豁免名单（按本项目编号）

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tankstorm",
                   "wiremap.json")


class MapError(Exception):
    pass


def _local(q):
    return q.split(":")[-1]


def names_of(st):
    """类 -> 消息名。报错模板给的真名优先，其次是没被混淆的类名，再次是常量类。"""
    names = {q: c["name"] for q, c in st["classes"].items() if c["name"]}
    for op, q in st["opcodes"].items():
        if q not in names and not _local(q).startswith("_-"):
            names[q] = _local(q)
    for const, op in (st.get("named") or {}).items():
        q = st["opcodes"].get(op)
        if q is None:
            raise MapError(f"常量 {const}={op} 指向的 opcode 没注册")
        if names.setdefault(q, const[4:]) != const[4:]:
            raise MapError(f"常量 {const}={op} 和该类的真名 {names[q]} 对不上")
    return names


def _unique(names):
    """名字 -> 类，只收唯一的。"""
    seen, dup = {}, set()
    for q, n in names.items():
        if n in seen:
            dup.add(n)
        seen[n] = q
    return {n: q for n, q in seen.items() if n not in dup}


def _fields(st, q):
    return (st["classes"].get(q) or {"fields": []})["fields"]


def pair_classes(canon, wire):
    """返回 {本项目的类: 新版的类}。"""
    cn, wn = _unique(names_of(canon)), _unique(names_of(wire))
    pairs = {cn[n]: wn[n] for n in cn if n in wn}

    def link(cq, wq, why):
        if cq in pairs:
            if pairs[cq] != wq:
                raise MapError(f"{why}：{_local(cq)} 已经对上 {_local(pairs[cq])}，"
                               f"这里又要对 {_local(wq)}")
            return False
        if wq in pairs.values():
            raise MapError(f"{why}：新版 {_local(wq)} 被两个类认领")
        pairs[cq] = wq
        return True

    def propagate():
        todo = list(pairs)
        while todo:
            cq = todo.pop()
            wq = pairs[cq]
            for fc, fw in zip(_fields(canon, cq), _fields(wire, wq)):
                if fc[1] == fw[1] == "message" and fc[3] and fw[3]:
                    if link(fc[3], fw[3], f"{_local(cq)} 的嵌套字段"):
                        todo.append(fc[3])

    def signature(st, q, nested_key):
        return tuple((ty, name, nested_key(nq) if nq else None)
                     for tag, ty, name, nq in _fields(st, q))

    propagate()
    # 剩下没名字的 opcode 类按结构签名认：嵌套类型用"新版那一侧的类"做身份
    while True:
        rev = {w: c for c, w in pairs.items()}
        left_c = [q for q in canon["opcodes"].values() if q not in pairs and _fields(canon, q)]
        left_w = [q for q in wire["opcodes"].values() if q not in rev and _fields(wire, q)]
        by_sig = {}
        for q in left_c:
            by_sig.setdefault(signature(canon, q, lambda n: pairs.get(n, ("?", n))), [[], []])[0].append(q)
        for q in left_w:
            by_sig.setdefault(signature(wire, q, lambda n: n), [[], []])[1].append(q)
        new = [(c[0], w[0]) for c, w in by_sig.values() if len(c) == 1 and len(w) == 1]
        if not new:
            break
        for cq, wq in new:
            link(cq, wq, "结构签名")
        propagate()
    return pairs


def build(canon, wire):
    """生成一个版本的映射表；任何一处对不上都抛 MapError。"""
    pairs = pair_classes(canon, wire)
    wire_exempt = wire.get("exempt")
    if not wire_exempt:
        raise MapError("新版 SWF 里认不出加密豁免数组（Transport 静态构造），没法核对映射")

    c_op = {q: op for op, q in canon["opcodes"].items()}
    w_op = {}
    for op, q in wire["opcodes"].items():
        if q in w_op:
            raise MapError(f"新版 {_local(q)} 注册在两个 opcode 上：{w_op[q]} 和 {op}")
        w_op[q] = op

    # 加密豁免的最后一块：各剩一条且都是空消息（心跳回包就是这样）时配上
    for d in ("c2s", "s2c"):
        if not set(crypto.EXEMPT[d]) <= set(canon["opcodes"]):
            raise MapError(f"本项目的 {d} 豁免名单里有 opcode 不在 {d} 结构表里")
        left_c = [op for op in crypto.EXEMPT[d] if canon["opcodes"][op] not in pairs]
        mapped = {w_op.get(pairs[canon["opcodes"][op]]) for op in crypto.EXEMPT[d]
                  if canon["opcodes"][op] in pairs}
        left_w = [op for op in wire_exempt[d] if op not in mapped]
        if len(left_c) == len(left_w) == 1:
            cq, wq = canon["opcodes"][left_c[0]], wire["opcodes"][left_w[0]]
            if not _fields(canon, cq) and not _fields(wire, wq):
                pairs[cq] = wq

    ops = {}
    for op, q in canon["opcodes"].items():
        if q not in pairs:
            continue
        if pairs[q] not in w_op:
            raise MapError(f"{op} {_local(q)} 对上的新版类 {_local(pairs[q])} 没注册 opcode")
        ops[op] = w_op[pairs[q]]
    for d in ("c2s", "s2c"):
        got = {ops.get(op) for op in crypto.EXEMPT[d]}
        if got != set(wire_exempt[d]):
            raise MapError(f"{d} 加密豁免对不上：本项目的 {sorted(crypto.EXEMPT[d])} "
                           f"映射成 {sorted(map(str, got))}，新版 SWF 是 {sorted(wire_exempt[d])}")

    # 字段：按声明位置对，名字、类型、嵌套类型逐个核。空消息也进表（字段表为空），
    # 嵌套在别的消息里时才翻译得了
    problems, extra = [], 0
    order = sorted(pairs, key=lambda q: c_op.get(q, "~" + q))
    index = {cq: i for i, cq in enumerate(order)}
    for cq in order:
        wq = pairs[cq]
        fc, fw = _fields(canon, cq), _fields(wire, wq)
        if len(fw) < len(fc):
            problems.append(f"{_local(cq)}：新版字段比本项目少（{len(fw)} < {len(fc)}）")
            continue
        extra += len(fw) - len(fc)
        for a, b in zip(fc, fw):
            ctag, cty, cname, cnest = a
            wtag, wty, wname, wnest = b
            if cty != wty or (cname and wname and cname != wname) \
                    or (cnest and wnest and pairs.get(cnest) != wnest):
                problems.append(f"{_local(cq)}：本项目 {a} 对新版 {b}")
    if problems:
        raise MapError("按声明位置对字段时有冲突：\n  " + "\n  ".join(problems[:20]))

    table = []
    for cq in order:
        rows = []
        for (ctag, cty, _, cnest), (wtag, *_rest) in zip(_fields(canon, cq), _fields(wire, pairs[cq])):
            rows.append([ctag, wtag, index.get(cnest, -1) if cty == "message" else None])
        table.append(rows)
    msgs = {op: index[q] for op, q in canon["opcodes"].items() if q in index}
    stats = {"opcode": len(canon["opcodes"]), "对上": len(ops), "类": len(order),
             "新版多出的字段": extra,
             "嵌套类型不明": sum(1 for rows in table for r in rows if r[2] == -1)}
    return {"ops": dict(sorted(ops.items())), "msgs": dict(sorted(msgs.items())),
            "classes": table}, stats


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("canon", help="本项目编号那一版的 structure.json（docs/structure.json）")
    ap.add_argument("wire", help="新版 SWF 用 extract_proto.py 导出的 structure.json")
    ap.add_argument("version", help="游戏版本号，即 FlashVars 里的 version")
    ap.add_argument("--out", default=OUT, help="写进哪个 wiremap.json（默认 tankstorm/wiremap.json）")
    args = ap.parse_args()

    canon = json.load(open(args.canon, encoding="utf-8"))
    wire = json.load(open(args.wire, encoding="utf-8"))
    try:
        entry, stats = build(canon, wire)
    except MapError as exc:
        print(f"生成失败：{exc}")
        return 1
    try:
        with open(args.out, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    data[args.version] = entry
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    print(f"已写入 {args.out} 的 {args.version}：" +
          "，".join(f"{k} {v}" for k, v in stats.items()))
    return 0


if __name__ == "__main__":
    import _console
    _console.utf8()
    sys.exit(main())

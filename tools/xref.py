#!/usr/bin/env python3
"""全库交叉引用：找出哪些方法体引用了某个名字（multiname 或字符串常量）。

    python3 xref.py <swf> _-2q0
    python3 xref.py <swf> --string 780511549720865
"""
import sys
from disasm import load, disasm

SWF = sys.argv[1]
abc = load(SWF)

# 方法索引 -> 所属类和成员名，用于把命中点定位到人能看懂的位置
owner = {}
for idx, inst in enumerate(abc.instances):
    qn = abc.qname(inst['name'])
    owner[inst['iinit']] = f'{qn}::iinit'
    for name, kind, mi in inst['traits']:
        if mi is not None and kind in ('method', 'getter', 'setter'):
            owner[mi] = f'{qn}::{name}'
    cls = abc.classes[idx]
    owner[cls['cinit']] = f'{qn}::cinit[static]'
    for name, kind, mi in cls['traits']:
        if mi is not None and kind in ('method', 'getter', 'setter'):
            owner[mi] = f'{qn}::{name}[static]'


def where(mi):
    return owner.get(mi, f'<游离方法 #{mi}>')


if sys.argv[2] == '--string':
    target = sys.argv[3]
    idxs = {i for i, s in enumerate(abc.strings) if s == target}
    print(f'字符串 {target!r} 在常量池的索引: {sorted(idxs)}')
    for mi, code in abc.bodies.items():
        for addr, txt, cm in disasm(abc, code):
            if txt.startswith('pushstring') and int(txt.split()[1]) in idxs:
                print(f'  {where(mi)}  方法#{mi} 偏移 {addr}')
else:
    target = sys.argv[2]
    hits = 0
    for mi, code in abc.bodies.items():
        lines = disasm(abc, code)
        for n, (addr, txt, cm) in enumerate(lines):
            if cm and target in cm:
                hits += 1
                print(f'\n>>> {where(mi)}  方法#{mi} 偏移 {addr}: {txt}  {cm}')
                for a2, t2, c2 in lines[max(0, n - 6):n + 4]:
                    mark = '  *' if a2 == addr else '   '
                    print(f'   {mark}{a2:5d}  {t2:<26} {c2}')
    print(f'\n共 {hits} 处引用')

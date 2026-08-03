#!/usr/bin/env python3
"""按类名（子串匹配）反汇编：列出静态/实例成员，输出方法体。

    python3 dump_class.py <swf> Transport
    python3 dump_class.py <swf> _-0ly --only encrypt,decrypt
"""
import sys
from disasm import load, disasm

SWF = sys.argv[1]
PAT = sys.argv[2]
ONLY = set(sys.argv[4].split(',')) if len(sys.argv) > 4 and sys.argv[3] == '--only' else None

abc = load(SWF)


def show(mi, title):
    code = abc.bodies.get(mi)
    print(f'\n--- {title}  (method#{mi}, {"无方法体" if code is None else str(len(code)) + "B"}) ---')
    if code is None:
        return
    for addr, txt, cm in disasm(abc, code):
        print(f'  {addr:4d}  {txt:<28} {cm}')


for idx, inst in enumerate(abc.instances):
    qn = abc.qname(inst['name'])
    if PAT not in qn:
        continue
    cls = abc.classes[idx]
    print('=' * 70)
    print(f'[{idx}] {qn}   继承自 {abc.qname(inst["super"])}')
    print(f'  接口: {[abc.qname(i) for i in inst["iface"]] or "无"}')
    print('  --- 静态成员 (class traits) ---')
    for name, kind, mi in cls['traits']:
        print(f'    {kind:8} {name}' + (f'   -> method#{mi}' if mi is not None else ''))
    print('  --- 实例成员 (instance traits) ---')
    for name, kind, mi in inst['traits']:
        print(f'    {kind:8} {name}' + (f'   -> method#{mi}' if mi is not None else ''))

    show(cls['cinit'], '[静态构造] cinit')
    for name, kind, mi in cls['traits']:
        if kind in ('method', 'getter', 'setter') and mi is not None:
            if ONLY and not any(o in name for o in ONLY):
                continue
            show(mi, f'[静态] {name}()')
    show(inst['iinit'], '[实例构造] iinit')
    for name, kind, mi in inst['traits']:
        if kind in ('method', 'getter', 'setter') and mi is not None:
            if ONLY and not any(o in name for o in ONLY):
                continue
            show(mi, f'[实例] {name}()')

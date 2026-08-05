#!/usr/bin/env python3
"""ABC（ActionScript Byte Code）解析 —— 按 Adobe《AVM2 Overview》的 abcFile 结构实现。

只做静态结构解析，不执行任何字节码。给 disasm.py / extract_proto.py 提供：

    ABC.strings     常量池字符串（下标 0 是空串占位）
    ABC.instances   [{name, super, iface, traits, iinit, flags}]
    ABC.classes     [{cinit, traits}]        与 instances 一一对应
    ABC.bodies      {method_index: code_bytes}
    ABC.qname(i)    multiname 下标 → 'package:ClassName' 形式的限定名

ABC 里的整数是 7 位一组的变长编码（u30 最多 5 字节）；类名与命名空间分开存在
常量池里，所以取限定名必须把 namespace 和 name 拼起来。
"""

import struct

# ---------------------------------------------------------------- 变长整数

def _u30(b, p):
    """变长无符号整数，最多 5 字节，每字节低 7 位有效。返回 (值, 新位置)。"""
    v = s = 0
    for _ in range(5):
        c = b[p]
        p += 1
        v |= (c & 0x7F) << s
        if not c & 0x80:
            break
        s += 7
    return v & 0xFFFFFFFF, p


def _s32(b, p):
    """有符号 32 位，编码同 u30，直接按补码解释。"""
    v, p = _u30(b, p)
    return (v - (1 << 32)) if v & 0x80000000 else v, p


def _d64(b, p):
    return struct.unpack_from("<d", b, p)[0], p + 8


# ---------------------------------------------------------------- 常量池

# namespace kind
NS_KIND = {
    0x05: "private", 0x08: "ns", 0x16: "package", 0x17: "packageInternal",
    0x18: "protected", 0x19: "explicit", 0x1A: "staticProtected",
}

# multiname kind
MN_QNAME, MN_QNAMEA = 0x07, 0x0D
MN_RTQNAME, MN_RTQNAMEA = 0x0F, 0x10
MN_RTQNAMEL, MN_RTQNAMELA = 0x11, 0x12
MN_MULTINAME, MN_MULTINAMEA = 0x09, 0x0E
MN_MULTINAMEL, MN_MULTINAMELA = 0x1B, 0x1C
MN_TYPENAME = 0x1D


class ABC:
    """解析后的 ABC 文件。"""

    def __init__(self, data: bytes):
        self.raw = data
        p = 0
        self.minor, self.major = struct.unpack_from("<HH", data, p)
        p += 4
        p = self._read_cpool(data, p)
        p = self._read_methods(data, p)
        p = self._read_metadata(data, p)
        p = self._read_classes(data, p)
        p = self._read_scripts(data, p)
        self._read_bodies(data, p)

    # ---------- 常量池 ----------

    def _read_cpool(self, b, p):
        n, p = _u30(b, p)
        self.ints = [0]
        for _ in range(max(0, n - 1)):
            v, p = _s32(b, p)
            self.ints.append(v)

        n, p = _u30(b, p)
        self.uints = [0]
        for _ in range(max(0, n - 1)):
            v, p = _u30(b, p)
            self.uints.append(v)

        n, p = _u30(b, p)
        self.doubles = [float("nan")]
        for _ in range(max(0, n - 1)):
            v, p = _d64(b, p)
            self.doubles.append(v)

        n, p = _u30(b, p)
        self.strings = [""]
        for _ in range(max(0, n - 1)):
            ln, p = _u30(b, p)
            self.strings.append(b[p:p + ln].decode("utf-8", "replace"))
            p += ln

        n, p = _u30(b, p)
        self.namespaces = [(0, 0)]          # (kind, name_index)
        for _ in range(max(0, n - 1)):
            kind = b[p]
            p += 1
            ni, p = _u30(b, p)
            self.namespaces.append((kind, ni))

        n, p = _u30(b, p)
        self.ns_sets = [()]
        for _ in range(max(0, n - 1)):
            cnt, p = _u30(b, p)
            items = []
            for _ in range(cnt):
                v, p = _u30(b, p)
                items.append(v)
            self.ns_sets.append(tuple(items))

        n, p = _u30(b, p)
        self.multinames = [(0, None)]       # (kind, payload)
        for _ in range(max(0, n - 1)):
            kind = b[p]
            p += 1
            if kind in (MN_QNAME, MN_QNAMEA):
                ns, p = _u30(b, p)
                nm, p = _u30(b, p)
                pay = (ns, nm)
            elif kind in (MN_RTQNAME, MN_RTQNAMEA):
                nm, p = _u30(b, p)
                pay = (None, nm)
            elif kind in (MN_RTQNAMEL, MN_RTQNAMELA):
                pay = (None, None)
            elif kind in (MN_MULTINAME, MN_MULTINAMEA):
                nm, p = _u30(b, p)
                nss, p = _u30(b, p)
                pay = (nss, nm)
            elif kind in (MN_MULTINAMEL, MN_MULTINAMELA):
                nss, p = _u30(b, p)
                pay = (nss, None)
            elif kind == MN_TYPENAME:
                nm, p = _u30(b, p)
                cnt, p = _u30(b, p)
                params = []
                for _ in range(cnt):
                    v, p = _u30(b, p)
                    params.append(v)
                pay = (nm, tuple(params))
            else:                            # 未知 kind，无法继续安全解析
                raise ValueError(f"未知 multiname kind 0x{kind:02x} @ {p - 1}")
            self.multinames.append((kind, pay))
        return p

    # ---------- 方法签名 ----------

    def _read_methods(self, b, p):
        n, p = _u30(b, p)
        self.methods = []
        for _ in range(n):
            pc, p = _u30(b, p)
            ret, p = _u30(b, p)
            ptypes = []
            for _ in range(pc):
                v, p = _u30(b, p)
                ptypes.append(v)
            name, p = _u30(b, p)
            flags = b[p]
            p += 1
            if flags & 0x08:                 # HAS_OPTIONAL
                oc, p = _u30(b, p)
                for _ in range(oc):
                    _, p = _u30(b, p)        # val index
                    p += 1                   # val kind
            if flags & 0x80:                 # HAS_PARAM_NAMES
                for _ in range(pc):
                    _, p = _u30(b, p)
            self.methods.append({"name": name, "flags": flags,
                                 "ret": ret, "params": ptypes})
        return p

    def _read_metadata(self, b, p):
        n, p = _u30(b, p)
        for _ in range(n):
            _, p = _u30(b, p)                # name
            cnt, p = _u30(b, p)
            for _ in range(cnt * 2):         # keys + values
                _, p = _u30(b, p)
        return p

    # ---------- traits ----------

    def _read_traits(self, b, p):
        cnt, p = _u30(b, p)
        out = []
        for _ in range(cnt):
            name_i, p = _u30(b, p)
            tag = b[p]
            p += 1
            kind, attrs = tag & 0x0F, tag >> 4
            mi = None
            if kind in (0, 6):               # Slot / Const
                _, p = _u30(b, p)            # slot_id
                _, p = _u30(b, p)            # type_name
                vindex, p = _u30(b, p)
                if vindex:
                    p += 1                   # vkind
                kname = "slot" if kind == 0 else "const"
            elif kind in (1, 2, 3):          # Method / Getter / Setter
                _, p = _u30(b, p)            # disp_id
                mi, p = _u30(b, p)
                kname = {1: "method", 2: "getter", 3: "setter"}[kind]
            elif kind == 4:                  # Class
                _, p = _u30(b, p)            # slot_id
                _, p = _u30(b, p)            # classi
                kname = "class"
            elif kind == 5:                  # Function
                _, p = _u30(b, p)            # slot_id
                mi, p = _u30(b, p)
                kname = "function"
            else:
                kname = f"kind{kind}"
            if attrs & 0x04:                 # ATTR_Metadata
                mc, p = _u30(b, p)
                for _ in range(mc):
                    _, p = _u30(b, p)
            out.append((self.name_of(name_i), kname, mi))
        return out, p

    # ---------- 类 ----------

    def _read_classes(self, b, p):
        n, p = _u30(b, p)
        self.instances = []
        for _ in range(n):
            name, p = _u30(b, p)
            sup, p = _u30(b, p)
            flags = b[p]
            p += 1
            if flags & 0x08:                 # ClassProtectedNs
                _, p = _u30(b, p)
            ic, p = _u30(b, p)
            iface = []
            for _ in range(ic):
                v, p = _u30(b, p)
                iface.append(v)
            iinit, p = _u30(b, p)
            traits, p = self._read_traits(b, p)
            self.instances.append({"name": name, "super": sup, "flags": flags,
                                   "iface": iface, "iinit": iinit,
                                   "traits": traits})
        self.classes = []
        for _ in range(n):
            cinit, p = _u30(b, p)
            traits, p = self._read_traits(b, p)
            self.classes.append({"cinit": cinit, "traits": traits})
        return p

    def _read_scripts(self, b, p):
        n, p = _u30(b, p)
        self.scripts = []
        for _ in range(n):
            init, p = _u30(b, p)
            traits, p = self._read_traits(b, p)
            self.scripts.append({"init": init, "traits": traits})
        return p

    # ---------- 方法体 ----------

    def _read_bodies(self, b, p):
        n, p = _u30(b, p)
        self.bodies = {}
        self.body_info = {}
        for _ in range(n):
            mi, p = _u30(b, p)
            max_stack, p = _u30(b, p)
            local_count, p = _u30(b, p)
            _, p = _u30(b, p)                # init_scope_depth
            _, p = _u30(b, p)                # max_scope_depth
            clen, p = _u30(b, p)
            code = b[p:p + clen]
            p += clen
            ec, p = _u30(b, p)
            for _ in range(ec):              # exception_info
                for _ in range(5):           # from,to,target,exc_type,var_name
                    _, p = _u30(b, p)
            _, p = self._read_traits(b, p)
            self.bodies[mi] = code
            self.body_info[mi] = {"max_stack": max_stack,
                                  "local_count": local_count}
        return p

    # ---------------------------------------------------------- 名字解析

    def name_of(self, mi: int) -> str:
        """multiname 下标 → 裸名字（不含包名）。"""
        if not 0 <= mi < len(self.multinames):
            return f"<mn{mi}>"
        kind, pay = self.multinames[mi]
        if kind == MN_TYPENAME:
            base = self.name_of(pay[0])
            args = ",".join(self.name_of(x) for x in pay[1])
            return f"{base}<{args}>"
        if pay is None:
            return "*"
        ni = pay[1]
        if ni is None:
            return "*"
        return self.strings[ni] if ni < len(self.strings) else f"<s{ni}>"

    def ns_of(self, mi: int) -> str:
        """multiname 下标 → 命名空间字符串（取不到返回空串）。"""
        if not 0 <= mi < len(self.multinames):
            return ""
        kind, pay = self.multinames[mi]
        if kind in (MN_QNAME, MN_QNAMEA) and pay[0]:
            k, ni = self.namespaces[pay[0]] if pay[0] < len(self.namespaces) else (0, 0)
            return self.strings[ni] if ni < len(self.strings) else ""
        if kind in (MN_MULTINAME, MN_MULTINAMEA) and pay[0]:
            nss = self.ns_sets[pay[0]] if pay[0] < len(self.ns_sets) else ()
            for x in nss:                     # 取第一个非空的
                k, ni = self.namespaces[x] if x < len(self.namespaces) else (0, 0)
                s = self.strings[ni] if ni < len(self.strings) else ""
                if s:
                    return s
        return ""

    def qname(self, mi: int) -> str:
        """限定名：'com.foo.Bar:Baz'。没有命名空间时退化为裸名。"""
        if mi == 0:
            return "*"
        ns, nm = self.ns_of(mi), self.name_of(mi)
        return f"{ns}:{nm}" if ns else nm

    def ns_kind(self, mi: int):
        """返回 (kind 数值, kind 名字)，用于识别 PackageInternalNs 之类。"""
        if not 0 <= mi < len(self.multinames):
            return None, ""
        kind, pay = self.multinames[mi]
        if kind in (MN_QNAME, MN_QNAMEA) and pay[0] and pay[0] < len(self.namespaces):
            k, _ = self.namespaces[pay[0]]
            return k, NS_KIND.get(k, f"kind{k}")
        return None, ""


def extract_abc(swf_path: str):
    """从 SWF 里取出所有 DoABC(82) / DoABC1(72) tag 的字节。"""
    import swfparse
    raw = open(swf_path, "rb").read()
    body, sig, ver, declared = swfparse.uncompress(raw)
    blocks = []
    for code, payload, off in swfparse.iter_tags(body):
        if code == 82:                        # DoABC: u32 flags + 以\0结尾的名字
            p = 4
            z = payload.index(b"\x00", p)
            blocks.append(payload[z + 1:])
        elif code == 72:                      # DoABC1: 直接就是 abc
            blocks.append(payload)
    return blocks


if __name__ == "__main__":
    import sys
    for i, blk in enumerate(extract_abc(sys.argv[1])):
        a = ABC(blk)
        print(f"[ABC #{i}] {len(blk):,}B  字符串 {len(a.strings):,}  "
              f"类 {len(a.instances):,}  方法 {len(a.methods):,}  "
              f"方法体 {len(a.bodies):,}")

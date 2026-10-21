# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""协议 schema：opcode 与消息名、字段号与字段名的对照。

schema.json 由 tools/extract_proto.py 从 RedWar_2026101403.swf 还原：
  · opcode 表来自注册方法里 567 次 注册函数(opcode, 消息类) 调用
  · 消息真名来自 protobuf-as3 的报错模板 'Bad data format: <消息>.<字段> ...'
    ——字符串字面量，secureSWF 动不了
  · 字段号来自 writeToBuffer 里 writeTag(output, wiretype, <tag>) 的立即数
  · 字段类型由 WriteUtils 各方法体实测认定
    （写 writeUTFBytes 的是 string，写 writeByte 的是 bool，等等）

这是本项目自己的编号，游戏更新后**不要**拿新版 SWF 重新生成它：2026102102 起
新版会打乱 opcode 和字段号，代码里的字段路径全按这一版写。新版本的线上编号
由 wiremap.py 在收发两头翻译，生成方法见 tools/build_wiremap.py。
"""

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))

# schema.json 跟着包走，是只读数据。打包成单文件后它被解压到 _MEIPASS/tankstorm/，
# 此时 __file__ 也指向那里，所以第一条就能命中；退路是 exe 旁边放一份，
# 方便游戏更新后直接替换而不必等新版本。
_CANDIDATES = [os.path.join(_HERE, "schema.json")]
try:
    from .paths import bundled_dir, user_path
    _CANDIDATES += [os.path.join(bundled_dir(), "tankstorm", "schema.json"),
                    user_path("schema.json")]
except ImportError:                                # 极端情况下别让导入失败拖垮整个包
    pass

SCHEMA = {}
for _PATH in _CANDIDATES:
    try:
        with open(_PATH, encoding="utf-8") as _f:
            SCHEMA = json.load(_f)
        break
    except (OSError, ValueError):
        continue


def name_of(op: str) -> str:
    """opcode（4位小写十六进制）-> 消息名，认不出就原样返回。"""
    e = SCHEMA.get(op)
    return e["name"] if e else op


def describe(op: str) -> str:
    e = SCHEMA.get(op)
    return f"{op} {e['name']}" if e else op


# ---------------------------------------------------------------- protobuf

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


_ZIGZAG = {"sint32", "sint64"}

# schema 类型 -> 应有的 wire type
_WIRE_TYPE = {"int32": 0, "int64": 0, "uint32": 0, "uint64": 0, "sint32": 0, "sint64": 0,
              "bool": 0, "enum": 0, "fixed64": 1, "sfixed64": 1, "double": 1,
              "fixed32": 5, "sfixed32": 5, "float": 5,
              "string": 2, "bytes": 2, "message": 2}


def check_wire_types(body: bytes, op: str):
    """按 schema 核对 body 里每个顶层字段的 wire type，返回 (一致数, 不一致数)。

    用来判断 opcode/字段号的对照还对不对（见 wiremap.verify）。2026-10-21 拿两次实盘的
    真实下行流实测：对照表对的时候一致率是 100%（这个协议的 repeated 标量不打包，
    所以 int 字段一律是 varint）；拿错的对照表去解，一致率只有 18%~57%。
    schema 里没有的字段、类型未知的字段不计；拆不开的算一个不一致。
    """
    fields = (SCHEMA.get(op or "", {}) or {}).get("fields", {})
    good = bad = 0
    i, n = 0, len(body)
    while i < n:
        key, i = _varint(body, i)
        if key is None:
            return good, bad + 1
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(body, i)
            if v is None:
                return good, bad + 1
        elif wt == 1:
            i += 8
        elif wt == 5:
            i += 4
        elif wt == 2:
            ln, i = _varint(body, i)
            if ln is None:
                return good, bad + 1
            i += ln
        else:
            return good, bad + 1
        if i > n:
            return good, bad + 1
        meta = fields.get(str(fn))
        want = _WIRE_TYPE.get(meta[1]) if meta else None
        if want is None:
            continue
        if wt == want:
            good += 1
        else:
            bad += 1
    return good, bad


def is_protobuf(body: bytes) -> bool:
    """body 能否整体解析成合法 protobuf（字段号>0，wire type 只能 0/1/2/5，长度刚好吃完）。

    录制器用它给实时解密做自检：密钥对了，解出来的 body 应当几乎全部通过。
    """
    n = len(body)
    i = 0
    while i < n:
        key, i = _varint(body, i)
        if key is None:
            return False
        wt, fn = key & 7, key >> 3
        if fn == 0 or wt in (3, 4, 6, 7):
            return False
        if wt == 0:
            v, i = _varint(body, i)
            if v is None:
                return False
        elif wt == 1:
            i += 8
        elif wt == 5:
            i += 4
        else:
            ln, i = _varint(body, i)
            if ln is None or ln > n - i:
                return False
            i += ln
        if i > n:
            return False
    return i == n


def decode(body: bytes, op: str = None, depth: int = 0):
    """解析 protobuf，按 schema 给字段命名。返回 dict，解析失败返回 None。

    同一字段出现多次（repeated）会收成 list。
    """
    fields = (SCHEMA.get(op or "", {}) or {}).get("fields", {})
    out, i, n = {}, 0, len(body)
    while i < n:
        key, i = _varint(body, i)
        if key is None:
            return None
        wt, fn = key & 7, key >> 3
        if fn == 0 or wt in (3, 4, 6, 7):
            return None
        meta = fields.get(str(fn))
        fname, ftype = (meta[0], meta[1]) if meta else (f"field{fn}", None)
        if wt == 0:
            v, i = _varint(body, i)
            if v is None:
                return None
            if ftype in _ZIGZAG:
                v = (v >> 1) ^ -(v & 1)
            elif ftype == "bool":
                v = bool(v)
        elif wt == 1:
            if i + 8 > n:
                return None
            v, i = int.from_bytes(body[i:i + 8], "little"), i + 8
        elif wt == 5:
            if i + 4 > n:
                return None
            v, i = int.from_bytes(body[i:i + 4], "little"), i + 4
        else:
            ln, i = _varint(body, i)
            if ln is None or ln > n - i:
                return None
            raw, i = body[i:i + ln], i + ln
            if ftype == "string":
                v = raw.decode("utf-8", "replace")
            else:
                v = None
                if ftype in ("message", "group", None) and depth < 6:
                    v = decode(raw, None, depth + 1)
                if v is None:              # 不是嵌套消息就试当文本，再不行才给 hex
                    try:
                        t = raw.decode("utf-8")
                        v = t if t.isprintable() else raw.hex()
                    except UnicodeDecodeError:
                        v = raw.hex()
        if fname in out:
            if not isinstance(out[fname], list):
                out[fname] = [out[fname]]
            out[fname].append(v)
        else:
            out[fname] = v
    return out

"""协议 schema：opcode 与消息名、字段号与字段名的对照。

schema.json 由 tools/extract_proto.py 从 RedWar SWF 还原：
  · opcode 表来自 _-1JW::_-67s 里 563 次 _-055(opcode, 消息类) 注册调用
  · 消息真名来自 protobuf-as3 的报错模板 'Bad data format: <消息>.<字段> ...'
    ——字符串字面量，secureSWF 动不了
  · 字段号来自 writeToBuffer 里 writeTag(output, wiretype, <tag>) 的立即数
  · 字段类型由 WriteUtils(_-3EJ) 各方法体实测认定
    （_-yF=string 写 writeUTFBytes，_-30c=bool 写 writeByte，等等）

游戏更新后重新生成：
    python tools/extract_proto.py <新的.swf> out && cp out/schema.json tankstorm/
"""

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_HERE, "schema.json")

try:
    with open(_PATH, encoding="utf-8") as _f:
        SCHEMA = json.load(_f)
except (OSError, ValueError):
    SCHEMA = {}


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

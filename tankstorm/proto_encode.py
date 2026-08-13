"""轻量 protobuf 编码器 —— 用于构造 C→S 请求消息的 body。

本模块只实现 protobuf 编码（Python dict → bytes），解码由 schema.py 负责。
游戏协议里 C→S 消息的字段类型只用到了 varint(int32/bool)、string、bytes，
没有 fixed64/fixed32/嵌套 message，所以这里只实现最常见的几种。

用法示例（构造 RceSuperStormOpt type=2 拒绝包）::

    body = encode_message({
        1: ("int32", 2),           # type = 2（拒绝）
        2: ("string", atkUid),     # 进攻方 uid
        4: ("string", atkName),    # 进攻方名字
        6: ("string", deftName),   # 防守方名字（我）
        7: ("string", deftUid),    # 防守方 uid（我）
    })

字段号和类型严格按 docs/redwar.proto 里的 RceSuperStormOpt 定义。
"""


def encode_varint(value: int) -> bytes:
    """编码一个无符号 varint。"""
    if value < 0:
        # protobuf 对负数用 10 字节补码 varint，但游戏协议里 int32 都是非负的
        value = value & 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def encode_tag(field_number: int, wire_type: int) -> bytes:
    """编码字段的 key = (field_number << 3) | wire_type。"""
    return encode_varint((field_number << 3) | wire_type)


def encode_int32(field_number: int, value: int) -> bytes:
    """编码 int32/int64/bool 字段（wire_type = 0）。"""
    return encode_tag(field_number, 0) + encode_varint(value)


def encode_string(field_number: int, value: str) -> bytes:
    """编码 string 字段（wire_type = 2）。"""
    raw = value.encode("utf-8")
    return encode_tag(field_number, 2) + encode_varint(len(raw)) + raw


def encode_bytes(field_number: int, value: bytes) -> bytes:
    """编码 bytes 字段（wire_type = 2）。"""
    return encode_tag(field_number, 2) + encode_varint(len(value)) + value


def encode_message(fields: dict, omit_zero: bool = True) -> bytes:
    """按字段号升序编码一组字段。

    fields: {字段号: (类型名, 值), ...}
    类型名支持: "int32", "bool", "string", "bytes"

    omit_zero=True（默认，保持老调用方的行为）
        值为 None / 空串 / 0（非 bool）的字段跳过不写。
    omit_zero=False
        除 None 外一律写出，**0 也要写**。

    为什么每日任务必须用 omit_zero=False
    ------------------------------------
    proto2 里"写了 0"和"根本没写"是两回事：前者 has_x() 为真，后者为假。
    8/10 抓包实测，真实客户端是**显式写 0** 的：
        RceHeroVisit  {free:1, type:0}
        RceHeroOpen   {type:0}
        RceDailySignIn{nType:0, nActivetype:0}
        RceWPCExplore {sceneID:10001, expType:0, exploreCnt:0, credit:0, ...}
    而按老行为，{3:("int32",1), 4:("int32",0)} 会编成只有 free 的包，type 整个丢掉；
    {3:("int32",0)} 更是编出一个**空 body** —— 空 body 连加密都不走
    （帧的加密判定是 real_frame and bool(body)），等于发了个空壳请求。
    2026-08-12 实盘就是这样：开面板的前置发出去了，动作却石沉大海。

    任务表里列出的字段 = "这次要赋值的字段"，所以一个都不能省。
    """
    parts = []
    for fn in sorted(fields):
        ftype, val = fields[fn]
        if val is None:
            continue
        if ftype in ("int32", "int64"):
            if omit_zero and val == 0:
                continue
            parts.append(encode_int32(fn, int(val)))
        elif ftype == "bool":
            parts.append(encode_int32(fn, 1 if val else 0))
        elif ftype == "string":
            if omit_zero and not val:
                continue
            parts.append(encode_string(fn, str(val)))
        elif ftype == "bytes":
            if omit_zero and not val:
                continue
            parts.append(encode_bytes(fn, val))
        else:
            raise ValueError(f"不支持的字段类型: {ftype}")
    return b"".join(parts)


# ---------------------------------------------------------------- 便捷函数


def build_rce_super_storm_opt(
    type_: int,
    atk_uid: str = "",
    n_atk_region: int = 0,
    atk_name: str = "",
    n_atk_lv: int = 0,
    deft_name: str = "",
    deft_uid: str = "",
    n_result: int = 0,
) -> bytes:
    """构造 RceSuperStormOpt (opcode 0x04ab) 的 protobuf body。

    字段定义（docs/redwar.proto + schema.json）::

        message RceSuperStormOpt {
          optional int32  type       = 1;   // 1=发起强攻, 2=拒绝强攻
          optional string atkUid     = 2;
          optional int32  nAtkRegion = 3;
          optional string atkName    = 4;
          optional int32  nAtkLv     = 5;
          optional string deftName   = 6;   // 防守方名字
          optional string deftUid    = 7;   // 防守方 uid
          optional int32  nResult    = 8;
        }
    """
    fields = {
        1: ("int32", type_),
        2: ("string", atk_uid),
        3: ("int32", n_atk_region),
        4: ("string", atk_name),
        5: ("int32", n_atk_lv),
        6: ("string", deft_name),
        7: ("string", deft_uid),
        8: ("int32", n_result),
    }
    return encode_message(fields)

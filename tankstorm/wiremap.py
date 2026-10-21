# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""游戏协议编号的翻译层：代码里一律用本项目的编号，连接时按游戏版本换成线上编号。

2026102102 起，游戏更新会把 opcode 和字段号整体打乱：RceArenaInfo 从 0468 挪到
0483、type 从字段 1 挪到字段 6；旧的心跳 040e 成了 RceClearData，旧的 RceLoad 041d
成了 RcePurchase。本项目的任务表、schema.json、各模块里的字段路径都按 2026101403
那一版的编号写（docs/structure.json），连接时：

    activate(ctx)          按 FlashVars 的 version 选映射（session.open_link 在连 TCP 之前调用）。
                           没见过的版本先沿用最近一个已适配版本的对照表试连：游戏更新
                           未必动协议，只改活动内容的更新照样能用
    ACTIVE.to_wire()       发出去之前：本项目编号 -> 线上编号（sender、握手、心跳）
    ACTIVE.from_wire()     收到之后：线上编号 -> 本项目编号（录制器解密之后、解码之前）
    verify(rec)            收完登录推送、发任何请求之前核对对照表：解出来的字段类型
                           要和 schema 一致，服务端时间要对得上。核对不过就断开，
                           这个版本在本进程里记为不可用（reject），保活重连时连 TCP 都不连

试连的代价是握手那两帧（RceInit、RceLoad）按旧对照表发出去：协议真被打乱时，
它们会落到别的消息上。它们只带固定密钥串和 "build"/"undefined" 几个字符串，
而且是明文发的，被当成加密消息解出来只会是乱码。

加密豁免按消息本身判断（本项目编号查 crypto.EXEMPT），收发两头都先把 opcode
换回本项目编号再查，不用为每个版本另备一份名单。

映射表 wiremap.json 由 tools/build_wiremap.py 从新版 SWF 生成。认不出的消息不在表里：
收到时记成 8xxx 的伪 opcode，不会和本项目编号撞车；要发时直接拒发。
"""

import contextlib
import json
import os
import struct
import time

from .log import get_logger
from .paths import user_path

log = get_logger()

_HERE = os.path.dirname(os.path.abspath(__file__))


class UnsupportedVersion(ConnectionError):
    """这个游戏版本登录后核对没通过。是 OSError 的子类，连接流程按连接失败处理。"""


class Unmapped(ValueError):
    """要发的消息或字段在当前游戏版本里没有对应，拒发。"""


# ---------------------------------------------------------------- protobuf 拆装

def _varint(b, i):
    shift = val = 0
    while i < len(b):
        c = b[i]
        i += 1
        val |= (c & 0x7F) << shift
        if not c & 0x80:
            return val, i
        shift += 7
        if shift > 63:
            break
    raise ValueError("varint 截断")


def _put_varint(v):
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _split(body):
    """拆成 [(字段号, wire type, 值的原始字节)]，拆不开抛 ValueError。"""
    out, i, n = [], 0, len(body)
    while i < n:
        key, i = _varint(body, i)
        tag, wt = key >> 3, key & 7
        if tag == 0:
            raise ValueError("字段号为 0")
        if wt == 0:
            _, j = _varint(body, i)
        elif wt == 1:
            j = i + 8
        elif wt == 5:
            j = i + 4
        elif wt == 2:
            ln, i = _varint(body, i)
            j = i + ln
        else:
            raise ValueError(f"wire type {wt}")
        if j > n:
            raise ValueError("长度越界")
        out.append((tag, wt, body[i:j]))
        i = j
    return out


# ---------------------------------------------------------------- 映射

class WireMap:
    """一个游戏版本的编号对照。entry 为 None 表示和本项目同一套编号，不用翻译。"""

    def __init__(self, version: str, entry: dict = None, assumed_from: str = None):
        self.version = version
        self.identity = entry is None
        # 没见过的版本借用的是哪个版本的对照表；None 表示这个版本本身就适配过
        self.assumed_from = assumed_from
        if self.identity:
            return
        self._op_out = entry["ops"]
        self._op_in = {w: c for c, w in self._op_out.items()}
        self._msg = entry["msgs"]
        # 每个类两张表：字段号 -> (对面的字段号, 嵌套类序号, 声明位置)
        # 嵌套类序号：None 不是消息；-1 是消息但认不出类型
        self._out = [{c: (w, n, i) for i, (c, w, n) in enumerate(rows)}
                     for rows in entry["classes"]]
        self._in = [{w: (c, n, i) for i, (c, w, n) in enumerate(rows)}
                    for rows in entry["classes"]]

    def op_out(self, op: str) -> str:
        if self.identity:
            return op
        try:
            return self._op_out[op]
        except KeyError:
            raise Unmapped(f"opcode {op} 在游戏版本 {self.version} 里没有对应的消息") from None

    def op_in(self, op: str) -> str:
        if self.identity:
            return op
        return self._op_in.get(op) or f"{0x8000 | int(op, 16):04x}"

    def _renum(self, body, cls, out):
        rows = (self._out if out else self._in)[cls]
        parts = []
        for tag, wt, val in _split(body):
            hit = rows.get(tag)
            if hit is None:
                if out:
                    raise Unmapped(f"字段 {tag} 在游戏版本 {self.version} 里没有对应")
                continue                                  # 新版多出来的字段，本项目用不到
            new, nested, pos = hit
            if wt == 2 and nested is not None:
                if nested < 0:
                    if out:
                        raise Unmapped(f"字段 {tag} 的嵌套类型认不出来")
                    continue
                val = self._renum(val, nested, out)
            parts.append((pos, _put_varint(new << 3 | wt)
                          + (_put_varint(len(val)) + val if wt == 2 else val)))
        if out:
            parts.sort(key=lambda p: p[0])            # 按声明顺序写，和真客户端逐字节一致
        return b"".join(p for _, p in parts)

    def to_wire(self, op: str, body: bytes):
        """本项目编号的 (opcode, body) -> 线上编号。对不上抛 Unmapped。"""
        wop = self.op_out(op)
        if self.identity or not body:
            return wop, body
        cls = self._msg.get(op)
        if cls is None:
            raise Unmapped(f"opcode {op} 在游戏版本 {self.version} 里没有字段表")
        try:
            return wop, self._renum(body, cls, True)
        except ValueError as exc:
            if isinstance(exc, Unmapped):
                raise
            raise Unmapped(f"opcode {op} 的 body 不是合法 protobuf：{exc}") from None

    def from_wire(self, wop: str, body: bytes):
        """线上编号的 (opcode, body) -> 本项目编号。body 拆不开抛 ValueError。"""
        op = self.op_in(wop)
        if self.identity or not body:
            return op, body
        cls = self._msg.get(op)
        if cls is None:
            return op, body                               # 认不出的消息原样留着，反正没人读
        return op, self._renum(body, cls, False)

    def frame_to_wire(self, frame: bytes) -> bytes:
        """protocol.json 里录下来的整帧（[长度][opcode][seq][body]）换成线上编号。

        不是这种帧的（TGW 头、a,uid,secret 登录行）原样返回：登录行虽然也有长度前缀，
        它的"opcode"是 'a,' 两个字符，不在表里。
        """
        if self.identity or len(frame) < 8 \
                or struct.unpack(">H", frame[:2])[0] != len(frame) - 2 \
                or frame[2:4].hex() not in self._op_out:
            return frame
        wop, body = self.to_wire(frame[2:4].hex(), frame[8:])
        return struct.pack(">H", len(body) + 6) + bytes.fromhex(wop) + frame[4:8] + body


IDENTITY = WireMap("")

# 当前连接用的映射。没连过（工具脚本、单元测试）就是不翻译
ACTIVE = IDENTITY

_DATA = None


def _load() -> dict:
    """随包的 wiremap.json，再叠上程序目录里用户自己放的那份（游戏更新后不用等发版）。"""
    global _DATA
    if _DATA is None:
        with open(os.path.join(_HERE, "wiremap.json"), encoding="utf-8") as f:
            data = json.load(f)
        mine = user_path("wiremap.json")
        if os.path.exists(mine):
            try:
                with open(mine, encoding="utf-8") as f:
                    extra = json.load(f)
                data.update({k: v for k, v in extra.items() if not k.startswith("_")})
                log.info("已叠加程序目录里的 %s（%s）", mine,
                         "、".join(k for k in extra if not k.startswith("_")))
            except (OSError, ValueError) as exc:
                log.warning("程序目录里的 wiremap.json 读不了，忽略：%s", exc)
        _DATA = data
    return _DATA


_REJECTED = {}      # 本进程里登录后核对没通过的版本 -> 原因


def for_version(version: str) -> WireMap:
    """这个游戏版本用哪张对照表。没见过的版本借用最近一个已适配版本的。"""
    if version in _REJECTED:
        raise UnsupportedVersion(
            f"游戏版本 {version} 先前登录后核对没通过（{_REJECTED[version]}），不再连接。"
            f"协议编号多半又被打乱了，重新生成对照表的方法见 README「游戏更新后」")
    data = _load()
    canon = data.get("_canon", "")
    if version and version == canon:
        return WireMap(version)
    entry = data.get(version) if version and not version.startswith("_") else None
    if entry is not None:
        return WireMap(version, entry)
    latest = max([k for k in data if not k.startswith("_")] + [canon])
    return WireMap(version, None if latest == canon else data[latest],
                   assumed_from=latest or "本项目编号")


def reject(version: str, why: str) -> None:
    """记下这个版本核对没通过，本进程之后不再连它。"""
    _REJECTED[version] = why


def activate(ctx: dict) -> WireMap:
    """按 FlashVars 里的游戏版本选好映射，之后的收发都按它翻译。"""
    global ACTIVE
    ACTIVE = for_version(str(ctx.get("version") or ""))
    if ACTIVE.assumed_from:
        log.warning("游戏版本 %s 没有现成的对照表，先按 %s 的编号试连；"
                    "登录后核对解出来的内容，对不上就断开，不发任何请求",
                    ACTIVE.version or "（未知）", ACTIVE.assumed_from)
    else:
        log.info("游戏版本 %s：%s", ACTIVE.version,
                 "与本项目同一套编号" if ACTIVE.identity else "按 wiremap.json 翻译 opcode 与字段号")
    return ACTIVE


# 登录后核对的门槛。编号对的时候一致率是 100%、服务端时间差一两秒；
# 拿错的对照表去解，一致率只有 18%~57%，RseAuthState 根本认不出来。
VERIFY_MIN_MESSAGES = 10        # 至少解出这么多条下行消息
VERIFY_MIN_FIELDS = 100         # 至少核对这么多个字段
VERIFY_MIN_AGREE = 0.99         # 字段 wire type 与 schema 一致的比例
VERIFY_MAX_SKEW = 3600          # RseAuthState.nCurTime 与本机时间最多差多少秒


def verify(rec, now: float = None):
    """收完登录推送后核对当前对照表，返回 (能不能发请求, 说明)。

    只看本次连接收到的东西：rec.wire_check 在每次连接时清零，
    RseAuthState 也只认序号在 rec.connect_seq 之后的那条。
    """
    m = ACTIVE
    if rec is None or not getattr(rec, "decrypt_ok", False):
        if m.assumed_from:
            return False, "实时解密没开或没通过自检，没法核对借用的对照表"
        return True, "实时解密没开或没通过自检，跳过核对（这个版本已适配）"
    msgs, good, bad = rec.wire_check
    if msgs < VERIFY_MIN_MESSAGES or good + bad < VERIFY_MIN_FIELDS:
        return False, f"登录后只解出 {msgs} 条消息、{good + bad} 个字段，不够判断"
    rate = good / (good + bad)
    if rate < VERIFY_MIN_AGREE:
        return False, f"字段类型与 schema 只有 {rate:.0%} 一致，编号对不上"
    seq, auth = rec.latest.get("RseAuthState") or (0, None)
    t = (auth or {}).get("nCurTime") if seq > rec.connect_seq else None
    now = time.time() if now is None else now
    if not isinstance(t, int) or abs(t - now) > VERIFY_MAX_SKEW:
        return False, f"没收到能对上本机时间的 RseAuthState（nCurTime={t}）"
    return True, (f"{msgs} 条消息、{good + bad} 个字段类型一致率 {rate:.1%}，"
                  f"服务端时间相差 {t - now:+.0f} 秒")


@contextlib.contextmanager
def using(version: str):
    """临时换成某个版本的映射，退出时恢复。给测试和离线工具用。"""
    global ACTIVE
    prev = ACTIVE
    ACTIVE = for_version(version)
    try:
        yield ACTIVE
    finally:
        ACTIVE = prev

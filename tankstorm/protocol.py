# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""坦克风暴 socket 协议编解码（数据驱动）。

游戏协议未加密（config.xml: encrypt=false），但具体的登录握手/心跳字节需要从
Wireshark 抓包里得到。为了让代码与"抓包得到的协议细节"解耦，协议细节放在项目根目录
的 protocol.json 里，本模块只负责按它拼包/拆包。

protocol.json 结构（抓包分析后由我填好，示例见 protocol.example.json）：

{
  "framing": "length4be",          // 帧结构：length2be|length4be|null|raw
  "encoding": "utf-8",
  "heartbeat": {                    // 心跳（挂机时定时发的小包）
     "interval_sec": 30,
     "raw_hex": "0002000270696e67"  // 已含帧头，原样重放
  },
  "login": {                        // 登录握手：按 parts 顺序拼接后再套帧
     "parts": [
        {"hex": "0001"},            // 字面字节
        {"field": "openid"},        // 取 ctx['openid'] 的 ASCII
        {"hex": "00"},
        {"field": "openkey"},
        {"field": "uid", "as": "int4be"}   // 也可按整型编码
     ]
  },
  "online_prompt": {                // 可选：服务器"是否在线"探测包 → 需回应
     "match_hex_prefix": "00ff",
     "reply": {"parts": [{"hex":"00ff01"}]}
  }
}
"""

import json
import os
import struct

from .log import get_logger

log = get_logger()

from . import paths                               # noqa: E402

BASE_DIR = paths.app_dir()
# 协议表是只读数据：exe 旁边有用户自己那份就用它（换游戏版本时可以直接替换），
# 没有就用打包进去的出厂默认值。
PROTOCOL_FILE = paths.data_file("protocol.json")


class ProtocolNotConfigured(Exception):
    pass


def load_spec(path: str = PROTOCOL_FILE) -> dict:
    if not os.path.exists(path):
        raise ProtocolNotConfigured(
            f"缺少 {path} —— socket 协议还没配置。请先按 README 用 Wireshark 抓包，"
            "跑 tools/pcap_analyze.py，把结果发我生成 protocol.json。")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------- 拼包 ----------------

def _render_part(part: dict, ctx: dict, encoding: str) -> bytes:
    if "hex" in part:
        return bytes.fromhex(part["hex"])
    if "text" in part:
        # 字面文本，可含 {field} 占位符（如 "a,{uid},{secret}"）
        return part["text"].format(**{k: ctx.get(k, "") for k in ctx}).encode(encoding)
    field = part.get("field")
    val = ctx.get(field, "")
    as_type = part.get("as", "ascii")
    if as_type == "ascii":
        return str(val).encode(encoding)
    if as_type == "int1":
        return struct.pack("B", int(val) & 0xFF)
    if as_type == "int2be":
        return struct.pack(">H", int(val) & 0xFFFF)
    if as_type == "int4be":
        return struct.pack(">I", int(val) & 0xFFFFFFFF)
    if as_type == "int8be":
        return struct.pack(">Q", int(val) & 0xFFFFFFFFFFFFFFFF)
    raise ValueError(f"未知字段编码 as={as_type}")


def render_message(parts: list, ctx: dict, encoding: str = "utf-8") -> bytes:
    return b"".join(_render_part(p, ctx, encoding) for p in parts)


def apply_framing(body: bytes, framing: str) -> bytes:
    if framing == "raw":
        return body
    if framing == "null":
        return body + b"\x00"
    if framing == "length2be":
        return struct.pack(">H", len(body)) + body
    if framing == "length4be":
        return struct.pack(">I", len(body)) + body
    if framing == "length2be_incl":     # 长度含自身
        return struct.pack(">H", len(body) + 2) + body
    if framing == "length4be_incl":
        return struct.pack(">I", len(body) + 4) + body
    raise ValueError(f"未知 framing={framing}")


def _build_step(step: dict, ctx: dict, default_framing: str, encoding: str) -> bytes:
    """构造登录序列中的一步 → 字节。"""
    if "raw_hex" in step:
        return bytes.fromhex(step["raw_hex"])
    body = render_message(step["parts"], ctx, encoding)
    return apply_framing(body, step.get("framing", default_framing))


def build_login_sequence(spec: dict, ctx: dict) -> list:
    """返回登录要按序发送的步骤列表：[(bytes, delay_after_sec), ...]。

    支持两种写法：
      - login_sequence: [ {step}, {step}, {"delay":0.2}, ... ]  多包握手（本游戏用这个）
      - login:          单个 {step}                              兼容旧的单包写法
    """
    encoding = spec.get("encoding", "utf-8")
    framing = spec.get("framing", "raw")
    seq = spec.get("login_sequence")
    if seq:
        out = []
        for step in seq:
            if "delay" in step and len(step) == 1:
                # 纯延迟步：附加到上一步的 delay 上
                if out:
                    b, d = out[-1]
                    out[-1] = (b, d + float(step["delay"]))
                continue
            data = _build_step(step, ctx, framing, encoding)
            out.append((data, float(step.get("delay", 0.05))))
        return out
    login = spec.get("login")
    if login:
        return [(_build_step(login, ctx, framing, encoding), 0.05)]
    raise ProtocolNotConfigured("protocol.json 缺少 login_sequence 或 login 段")


def build_login(spec: dict, ctx: dict) -> bytes:
    """兼容旧接口：把整个登录序列拼成一坨字节（不含步间延迟）。"""
    return b"".join(data for data, _ in build_login_sequence(spec, ctx))


def build_heartbeat(spec: dict, ctx: dict) -> bytes:
    hb = spec.get("heartbeat")
    if not hb:
        raise ProtocolNotConfigured("protocol.json 缺少 heartbeat 段")
    if "raw_hex" in hb:
        return bytes.fromhex(hb["raw_hex"])
    body = render_message(hb["parts"], ctx, spec.get("encoding", "utf-8"))
    return apply_framing(body, spec.get("framing", "raw"))


def heartbeat_interval(spec: dict) -> float:
    return float((spec.get("heartbeat") or {}).get("interval_sec", 30))


def maybe_online_reply(spec: dict, data: bytes, ctx: dict) -> bytes | None:
    """若服务器发来'是否在线'探测包，返回应答字节；否则 None。"""
    op = spec.get("online_prompt")
    if not op:
        return None
    prefix = op.get("match_hex_prefix")
    if prefix and data.startswith(bytes.fromhex(prefix)):
        reply = op.get("reply")
        if reply and "raw_hex" in reply:
            return bytes.fromhex(reply["raw_hex"])
        if reply:
            body = render_message(reply["parts"], ctx, spec.get("encoding", "utf-8"))
            return apply_framing(body, spec.get("framing", "raw"))
    return None

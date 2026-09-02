# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""构造加密帧并通过 socket 发送 —— 用于主动发出 C→S 协议消息。

保活进程原来只发三种豁免包（心跳 040e / 认证 041c / build 041d），全在 RC4
发送豁免名单里，body 不用加密，可以静态重放。

但"拒绝超级强攻"(RceSuperStormOpt, opcode 04ab) **不在豁免名单里**，body 必须
经过 C→S 方向的 RC4 实例加密。加密后密钥流被消耗，后续所有 C→S 非豁免消息
必须共享同一条密钥流 —— 所以绝不能另建 RC4 实例，必须用 Recorder 里那个。

帧结构（抓包确认）::

    [2字节大端长度 N][2字节 opcode][4字节 seq=0][body]
    整帧 = 2 + N，N = 2(opcode) + 4(seq) + len(body) = 6 + len(body)

客户端所有包的 seq 恒为 0。
"""

import struct
import time
import random
from . import crypto
from .log import get_logger
from .proto_encode import build_rce_super_storm_opt

log = get_logger()

# RceSuperStormOpt 的 opcode（C→S）
OPCODE_RCE_SUPER_STORM_OPT = "04ab"


def build_frame(opcode_hex: str, body: bytes, seq: int = 0) -> bytes:
    """把 opcode + seq + body 组装成完整帧（含 2 字节长度头）。

    返回可直接 sendall 的字节。
    """
    opcode_bytes = bytes.fromhex(opcode_hex)
    seq_bytes = struct.pack(">I", seq)
    payload = opcode_bytes + seq_bytes + body
    length = struct.pack(">H", len(payload))
    return length + payload


def encrypt_body(opcode_hex: str, body: bytes, rc4_c2s) -> bytes:
    """根据豁免名单决定是否加密 body。

    非豁免 → 用 C→S 的 RC4 实例加密（消耗密钥流）。
    豁免 → 原样返回。

    注意：RC4 是有状态的流密码，每次 crypt() 都会推进内部 i/j 指针。
    所以这个函数每调用一次（对非豁免包），密钥流就不可逆地前进了 len(body) 步。
    """
    if opcode_hex in crypto.EXEMPT["c2s"]:
        return body
    if rc4_c2s is None:
        raise RuntimeError(
            f"opcode {opcode_hex} 不在发送豁免名单，必须加密，"
            "但 RC4 C→S 实例不可用（实时解密未启用或密钥自检失败）"
        )
    return rc4_c2s.crypt(body)


def send_frame(sock, opcode_hex: str, body: bytes, rc4_c2s=None) -> None:
    """加密 body → 组帧 → sendall。

    对豁免 opcode（040e/041c/041d/0455）不加密；其余必须提供 rc4_c2s。
    """
    enc_body = encrypt_body(opcode_hex, body, rc4_c2s)
    frame = build_frame(opcode_hex, enc_body)
    sock.sendall(frame)
    log.debug("已发送帧 %s（body %d 字节，帧 %d 字节）",
              opcode_hex, len(body), len(frame))


def send_reject_super_storm(sock, rc4_c2s, rse_data: dict) -> bool:
    """收到 RseSuperStormOpt (027c) 后，构造 RceSuperStormOpt (04ab) type=2 拒绝包并发送。

    参数 rse_data 是 schema.decode() 解出的 RseSuperStormOpt 字段字典，形如::

        {
            "type": 1,
            "nResult": 0,
            "deftUid": "12345",
            "deftName": "我的名字",
            "atkUid": "67890",
            "atkName": "攻击者名字",
        }

    AS3 原始代码（_-3SJ 方法）的行为：
        _loc1_ = new RceSuperStormOpt()
        _loc1_.type = 2
        _loc1_.atkUid  = this.msg.atkUid
        _loc1_.atkName = this.msg.atkName
        _loc1_.deftName = this.msg.deftName   # AS3 里写的 _-02e，实为 deftName
        _loc1_.deftUid = this.msg.deftUid
        Transport.Send(_loc1_)

    注意 RseSuperStormOpt 和 RceSuperStormOpt 的字段号不同：
      Rse: deftUid=3, deftName=4, atkUid=5, atkName=6
      Rce: atkUid=2, atkName=4, deftName=6, deftUid=7
    但字段**名**是一样的，所以按名取、按 Rce 的字段号填就行。

    返回 True 表示成功发送，False 表示条件不满足未发送。
    """
    if not rse_data:
        log.warning("拒绝超级强攻失败：解码数据为空")
        return False

    atk_uid = str(rse_data.get("atkUid") or "")
    atk_name = str(rse_data.get("atkName") or "")
    deft_uid = str(rse_data.get("deftUid") or "")
    deft_name = str(rse_data.get("deftName") or "")

    if not atk_uid:
        log.warning("拒绝超级强攻失败：缺少 atkUid")
        return False

    body = build_rce_super_storm_opt(
        type_=2,
        atk_uid=atk_uid,
        atk_name=atk_name,
        deft_name=deft_name,
        deft_uid=deft_uid,
    )

    log.info("构造拒绝超级强攻包：type=2 atkUid=%s atkName=%s → deftUid=%s deftName=%s "
             "(body %d 字节)", atk_uid, atk_name, deft_uid, deft_name, len(body))

    try:
        time.sleep(random.uniform(5, 15))
        send_frame(sock, OPCODE_RCE_SUPER_STORM_OPT, body, rc4_c2s)
        log.info("✅ 已发送拒绝超级强攻（RceSuperStormOpt type=2, opcode %s）",
                 OPCODE_RCE_SUPER_STORM_OPT)
        return True
    except Exception as exc:
        log.error("发送拒绝超级强攻失败: %s", exc)
        return False

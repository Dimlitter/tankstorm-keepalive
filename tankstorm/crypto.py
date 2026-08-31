# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""RC4 与密钥推导 —— 严格照 RedWar SWF 的字节码实现。

细节和推导过程见 docs/加密与协议还原.md。要点：

  · 包头永远明文，只有 body 过 RC4
  · 豁免 opcode 的 body 完全不碰 RC4 实例，**不消耗密钥流**
  · 每个方向一个 RC4 实例，密钥流在该方向所有非豁免 body 之间连续累积

密钥三要素全部来自网页的 FlashVars（Transport._-3rU 是被 RedWar.Data 调的，
不是 socket 消息处理器），所以保活进程在 connect 之前就能把密钥算出来：

    mid  = int(level) * 100 + (0 if firstLogin 为真 else 1)
    接收 = uid ‖ mid ‖ sid      S 表倒序 S[k] = 255 - k
    发送 = sid ‖ uid ‖ mid      S 表正序 S[k] = k

其中 uid 位置上原本是 BASE._-71r，编译期默认 '780511549720865'，
运行时被 RedWar.Data 覆盖成 uid。
"""

DEFAULT_PREFIX = "780511549720865"

# 豁免 RC4 的 opcode（RedWar_2026073102.swf 实测）
#   接收 Transport._-29U 里的比较链：533/552/553/560/643
#   发送 Transport.Send  里的比较链：1038/1052/1053/1109
EXEMPT = {
    "s2c": {"0215", "0228", "0229", "0230", "0283"},
    "c2s": {"040e", "041c", "041d", "0455"},
}


class RC4:
    """_-15y:_-0ly 的等价实现。reversed_sbox 对应构造函数第二个参数。"""

    __slots__ = ("S", "i", "j")

    def __init__(self, key: bytes, reversed_sbox: bool):
        S = list(range(255, -1, -1)) if reversed_sbox else list(range(256))
        klen = len(key) - 2            # 无条件，不是混淆器的假分支
        if klen < 1:
            raise ValueError("密钥太短：KSA 取模用 len-2")
        j = 0
        for k in range(256):
            j = (j + S[k] + key[k % klen]) & 0xFF
            S[k], S[j] = S[j], S[k]
        self.S = S
        self.i = self.j = 11           # 无条件，KSA 结束后 i=j=11

    def crypt(self, data: bytes) -> bytes:
        S, i, j = self.S, self.i, self.j
        out = bytearray(len(data))
        for k in range(len(data)):
            i = (i + 1) & 0xFF
            j = (j + S[i]) & 0xFF
            S[i], S[j] = S[j], S[i]
            out[k] = data[k] ^ S[(S[i] + S[j]) & 0xFF]
        self.i, self.j = i, j
        return bytes(out)


def middle(level, first_login) -> int:
    """密钥中段：int(level)*100 + (firstLogin ? 0 : 1)。"""
    try:
        lv = int(str(level).strip() or 0)
    except ValueError:
        lv = 0
    s = str(first_login).strip().lower()
    return lv * 100 + (0 if s in ("true", "1", "yes") else 1)


def make_key(direction: str, prefix: str, mid, sid: str) -> bytes:
    """AS3 里 String + int 是十进制拼接，这里保持一致。"""
    if direction == "s2c":
        return f"{prefix}{mid}{sid}".encode("utf-8")
    return f"{sid}{prefix}{mid}".encode("utf-8")


def from_ctx(ctx: dict):
    """用 FlashVars 里的 uid/sid/level/firstLogin 造出双向 RC4。

    返回 ({'s2c': RC4, 'c2s': RC4}, 说明文字)；材料不全时返回 (None, 原因)。
    """
    uid = str(ctx.get("uid") or "").strip()
    sid = str(ctx.get("sid") or "").strip()
    if not uid or not sid:
        return None, "缺 uid 或 sid"
    if ctx.get("level") in (None, ""):
        return None, "缺 level（FlashVars 里应该有）"
    mid = middle(ctx.get("level"), ctx.get("firstLogin"))
    try:
        return ({"s2c": RC4(make_key("s2c", uid, mid, sid), True),
                 "c2s": RC4(make_key("c2s", uid, mid, sid), False)},
                f"uid={uid} 中段={mid}（level={ctx.get('level')}, "
                f"firstLogin={ctx.get('firstLogin')}） sid={sid[:6]}…")
    except ValueError as exc:
        return None, str(exc)

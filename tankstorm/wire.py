# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""已登录连接上的请求原语：发请求、等回包、边等边续心跳、按路径读响应。

每日任务、国战、争霸战、公会战、功勋商城都走同一套：

    since = wire.send(sock, rec, opcode, fields)      # 记下发送前的消息序号
    data = wire.await_response(sock, rec, "RseXxx", since, timeout, want=...)

since 是**消息到达序号**（rec.seq_mark()），只认序号比它大的回包。
不用时间戳：time.time() 在 Windows 上粒度约 15.6ms，服务器回得快时
"发送时刻"和"到达时刻"会落在同一个 tick，收到了也判成超时。

心跳
----
任务一跑就是好几分钟，心跳周期只有 10 秒。心跳由任务侧在等待时顺手发
（nap / await_response 里调 beat()），而不是开后台线程：心跳走明文豁免、
不碰 RC4，但两个线程同时 sendall 会让帧字节交错，那比掉线难查得多。

心跳器用 `with wire.heartbeat(beater):` 装上，退出时恢复**原来那个**。
国战曾经无条件把全局心跳器赋成 None，把每日任务装好的那个废掉，
2026-08-29 实盘跑了两分半"共发心跳 0 次"。上下文管理器从结构上杜绝这类错。
"""

import contextlib
import re
import time

from . import sender
from .log import get_logger
from .proto_encode import encode_message

log = get_logger()

_BEAT = None


def beat() -> None:
    """该发心跳就发一次；没装心跳器就是空操作。"""
    if _BEAT is None:
        return
    try:
        _BEAT()
    except Exception as exc:                  # 心跳发不出去不该弄挂任务
        log.debug("心跳发送失败（忽略）: %s", exc)


@contextlib.contextmanager
def heartbeat(fn):
    """在这段代码执行期间使用心跳器 fn。fn 为 None 时什么都不动。

    退出时恢复进入前的那个心跳器，不是恢复成 None —— 嵌套调用时
    （每日任务里跑国战）外层装好的心跳器不能被内层摘掉。
    """
    global _BEAT
    if fn is None:
        yield
        return
    prev = _BEAT
    _BEAT = fn
    try:
        yield
    finally:
        _BEAT = prev


def nap(seconds: float) -> None:
    """边等边发心跳。任务之间动辄等好几秒，累计起来早超过心跳周期了。"""
    end = time.time() + seconds
    while True:
        beat()
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(left, 1.0))


class Beater:
    """到点才发心跳，没到点就是空操作。由任务侧在等待时调用。"""

    def __init__(self, sock, hb: bytes, interval: float):
        self.sock = sock
        self.hb = hb
        self.interval = interval
        self.last = time.time()      # 刚登录完不必立刻补一发
        self.count = 0

    def __call__(self) -> None:
        now = time.time()
        if now - self.last < self.interval:
            return
        self.sock.sendall(self.hb)
        self.last = now
        self.count += 1
        log.debug("任务执行中心跳 #%d", self.count)


# ---------------------------------------------------------------- 发与收

def send(sock, rec, opcode: str, fields: dict) -> int:
    """编码并发出一条请求，返回发送前的消息序号（之后只认序号更大的回包）。

    omit_zero=False：字段表里列的字段一个都不能省，0 也要显式写出来。
    proto2 里"写了 0"和"没写"是两回事，真实客户端是显式写 0 的。
    """
    before = rec.seq_mark() if rec else 0
    sender.send_frame(sock, opcode, encode_message(fields, omit_zero=False),
                      rec.rc4_c2s)
    return before


def latest(rec, rse_msg: str):
    """某条服务器消息最近一次的解码结果（字段字典）；没有就返回 None。"""
    got = rec.latest.get(rse_msg) if rec else None
    if not got or not isinstance(got[1], dict):
        return None
    return got[1]


def pick_recent(rec, rse_msg, since_seq, want=None):
    """从这条消息的近期历史里挑一条序号更新、且满足 want 的。

    只看 rec.latest 是不够的：服务器会连着推同名但内容不同的两条
    （RseWPCBaseOpen 先 type:0 带 leftFreeCnt，再 type:3 只有擂台信息），
    latest 会被后一条冲掉，闸门就以为"没有这个字段"。
    """
    hist = getattr(rec, "recent", {}).get(rse_msg)
    if hist:
        for seq, data in reversed(hist):
            if seq > since_seq and (want is None or want(data)):
                return data
        return None
    got = rec.latest.get(rse_msg)                 # 老录制器没有 recent
    if got and got[0] > since_seq and (want is None or want(got[1])):
        return got[1]
    return None


def pump(sock, timeout: float = 0.5) -> bool:
    """从 socket 收一批数据（录制器的旁路会顺手解码入库）。连接断开返回 False。"""
    try:
        sock.settimeout(timeout)
        if not sock.recv(8192):
            return False
    except Exception:
        pass
    return True


def await_response(sock, rec, rse_msg, since_seq, timeout, want=None,
                   relaxed=None):
    """发完请求后等对应的服务器响应。

    want 可选，用来在同名的几条里挑出真正有用的那条。

    relaxed 是**退而求其次**的判据：等不到严格匹配时用它再捞一次。
    闸门要用到 —— 有些面板回包的 type 不是对请求的回显而是"回包种类"
    （RceWPCBaseOpen 发 type:1，回包却是 type:0 的面板 + type:3 的冠军信息），
    拿它当区分字段会把唯一带次数的那条否掉。2026-08-13 实盘就这么把配件探索
    明明还有 3 次免费的一轮跳过了。所以严格判据只用来"优先挑"，挑不到就放宽。
    """
    if rec is None:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        hit = pick_recent(rec, rse_msg, since_seq, want)
        if hit is not None:
            return hit
        beat()           # 等回包是耗时大头，心跳得在这儿续上
        if not pump(sock):
            break
    if relaxed is not None:
        hit = pick_recent(rec, rse_msg, since_seq, relaxed)
        if hit is not None:
            log.debug("等不到严格匹配的 %s，退回用宽松判据挑到一条", rse_msg)
            return hit
    return None


# ---------------------------------------------------------------- 读响应

def is_int(v) -> bool:
    """是不是真正的整数。bool 是 int 的子类，必须先挡掉。"""
    return isinstance(v, int) and not isinstance(v, bool)


def as_list(v):
    """protobuf 的 repeated 字段只出现一次时解码成标量，多次才是 list。"""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


_SELECT = re.compile(r"^([A-Za-z_]\w*)\[(\w+)=(-?\d+)\]$")


def read_path(data, path):
    """按路径从响应里取值，支持嵌套、"从每个元素里挑一个字段"和"按标记选元素"。

    形如：
        "freeVisitCnt"              顶层字段
        "sVisitData.field4"         嵌套一层（参谋的 [低级剩余, 高级剩余]）
        "field14[].field1"          列表里逐个取 field1（军备的 [{剩余,冷却}, …]）
        "field4[field1=1].field2"   列表里挑 field1==1 的那个元素，再取 field2

    最后一种是给技能书用的：RseBookCollection.field4 是
    [{field1:档位, field2:剩余, field3:下次可用}, …]，免费档是 field1==1。
    **按标记挑而不是按下标挑** —— 下标会随服务端调整档位顺序而错位，
    而错位在这个项目里已经犯过一次（schema 字段名按位置对齐，错了 198 个消息）。

    "xxx[]" 那一段直接返回列表，由调用方逐个取下一段（见 daily 的 read_counts）。
    取不到返回 None。
    """
    cur = data
    for part in path.split("."):
        sel = _SELECT.match(part)
        if sel:
            name, key, val = sel.group(1), sel.group(2), int(sel.group(3))
            if not isinstance(cur, dict):
                return None
            items = cur.get(name)
            if items is None:
                return None
            cur = next((e for e in as_list(items)
                        if isinstance(e, dict) and e.get(key) == val), None)
            if cur is None:
                return None
            continue
        pluck = part.endswith("[]")
        if pluck:
            part = part[:-2]
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
        if pluck:
            return as_list(cur)            # 下一段负责从每个元素里挑
    return cur

# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""服务器消息录制 + 异常事件实时告警 + 加密载荷取证。

用途：像"超级强攻令"这种事件很随机（对方用令后你要在 5 分钟内输验证码，否则基地被强攻），
没法蹲着抓包。但保活守护进程本来就 24 小时连在游戏 socket 上，让它顺便：

  1. 把**双向**消息按协议帧切好，落盘到 logs/frames-YYYY-MM-DD.jsonl；
  2. 一旦发现"没见过的消息类型"或命中关键词（验证码/强攻/进攻…），
     立刻通过 PushPlus 推到你微信 —— 你就能在 5 分钟窗口内自己打开游戏处理；
  3. 自动识别**载荷加密**的消息（高熵 + 非 protobuf），完整存成独立 .bin，
     供 tools/redwar_rc4.py 离线解密 —— 这类消息永不采样、永不截断。

注意：本模块只做"观察 + 通知你"，不会替你回应验证码。验证码是游戏用来确认真人在场的
机制，自动过验证码属于绕过人机验证，不在本项目范围内；及时叫你本人去处理才是正路。

帧格式（抓包确认）：[2字节大端长度 N][2字节 opcode][4字节 seq][body]，整帧 = 2 + N。

关于加密（2026-08-03，已由 RedWar_2026073102.swf 字节码定案）
------------------------------------------------------------
**绝大多数消息都是 RC4 加密的**，只有少数 opcode 走白名单豁免：
  接收豁免 0215 0228 0229 0230 0283   （Transport._-29U 里按 opcode 判断）
  发送豁免 040e 041c 041d 0455        （Transport.Send 里按 opcode 判断）
protocol.json 的心跳/认证/build 三个包恰好都在发送豁免名单里 —— 这就是静态
重放能工作的原因；0283 世界广播在接收豁免名单里 —— 这就是关键词能读到中文
昵称的原因。两者都推不出"协议没加密"。

密钥流范围（关键）：
  · 包头永远明文，只有 body 过 RC4
  · 豁免消息的 body 完全不碰 RC4 实例，**不消耗密钥流**
  · 每个方向一个 RC4 实例，登录响应到达时建立，之后再不重置
  → 密钥流在「该方向所有非豁免 body」之间连续累积。要解第 N 条，必须按顺序
    喂入它之前的每一条非豁免 body，一条不能少；包头和豁免消息必须排除在外。

所以：**中途漏一个非豁免 body，之后全部永久解不开。** 本模块因此把原始双向
字节流全量落盘到 logs/streams/（默认开启），分帧失步会立刻标红 —— 那意味着
这条流从失步点起已经报废，只能重连重录。

密钥（Transport._-2q0）：
  接收 = BASE._-71r ‖ str(level*100+firstLoginFlag) ‖ sid   S表倒序 S[k]=255-k
  发送 = sid ‖ BASE._-71r ‖ str(level*100+firstLoginFlag)   S表正序 S[k]=k
  两者都是 KSA 取模用 len-2、KSA 后 i=j=11（都不是假分支，字节码里无条件执行）。
  BASE._-71r 编译期默认 '780511549720865'，运行时被 RedWar.Data 覆盖成 uid。
解密用 tools/redwar_rc4.py。
"""

import hashlib
import json
import math
import os
import struct
import time
from collections import Counter
from datetime import date

from . import crypto
from .log import LOG_DIR, get_logger
from .stream_recorder import StreamRecorder

try:
    from . import schema as SCH        # opcode -> 消息名（schema.json 还原自 SWF）
except Exception:                      # schema.json 缺失也不影响录制
    SCH = None

log = get_logger()

# 正常游戏 27 分钟抓包里出现过的 S>C opcode（基线）。不在此列表 = 新事件，值得告警。
KNOWN_OPCODES = {
    "0201", "0208", "020c", "0211", "0213", "0215", "0216", "0217", "021b",
    "021c", "021e", "0221", "0222", "0223", "0225", "0228", "022c", "022e",
    "0231", "0232", "0234", "0236", "0240", "0241", "0243", "0247", "024e",
    "024f", "0255", "0257", "025a", "0271", "0275", "0277", "027b", "027f",
    "0283", "0287", "028e", "0296", "029a", "029d", "029e", "02a0", "02a1",
    "02a2", "02a9", "02aa", "02b7", "02bc", "02c4", "02cb", "02cc", "02d8",
    "02e6", "0303", "0307", "0312", "031a", "0326", "0329", "0331",
}

# 已学习到的 opcode 落盘于此：良性新类型只提醒一次，重启后不再重复打扰
LEARNED_FILE = "known-opcodes.json"

# 已确认的重要事件消息：**每次都告警**，不受"只报一次""登录静默""已学习"影响。
# 2026-08-02 19:13 实测被超级强攻时捕获到这两条（此前两天日志中从未出现）。
# 它们的载荷是加密的，关键词和图片检测对其无效，只能靠 opcode 本身识别。
# 消息名由 SWF 还原（见 schema.json）：027c 就是超级强攻本体，字段里带攻防双方
# 的 uid 和昵称；0268 是国家事件通告，实测两者同时出现。
EVENT_SIGNATURES = {
    "027c": "RseSuperStormOpt —— 超级强攻通知（字段含 atkUid/atkName/deftUid/deftName）",
    # 0268 (RseCountryOpt) 曾经也在这里，理由是 2026-08-02 抓到超级强攻时它和
    # 027c 一起出现。2026-08-29 做国战功能时发现那是**巧合**：0268 是国战的通用
    # 回包，开面板、召唤、扫荡、攻击全走它，正常打国战时每秒都在刷。
    # 留着它有两个害处，后一个更严重：
    #   · --keepalive 模式下 on_alert 接着 PushPlus，打一轮国战就是一轮轰炸
    #   · 真的超级强攻告警会被淹没在噪声里 —— 而那正是本项目最要紧的功能
    # 超级强攻有 027c 这个专属信号，字段里直接带攻防双方，不需要 0268 来佐证。
}

# 高频/大体积消息：只计数不存 body，避免日志爆炸（0283 是玩家列表，单条可达数 KB）
BULK_OPCODES = {"0283", "0215", "0213", "0217", "0234", "0287"}

# 世界广播类消息：正文里塞满**其他玩家的自定义昵称**，关键词必然误报。
# 实测 0283 里出现过昵称叫「在线包强攻，强攻令2270」「强攻宝贝」的玩家，
# 广播一刷就触发"强攻"告警，但跟本人毫无关系。
# 因此这类消息只有在**提到我自己**（uid/昵称）时才允许关键词告警。
BROADCAST_OPCODES = {"0283"}

# 命中即告警的关键词（UTF-8 出现在 protobuf 字符串字段里）
ALERT_KEYWORDS = [
    "验证码", "超级强攻", "强攻令", "被进攻", "进攻你",
    "五分钟内", "5分钟内", "确认在线", "是否在线",
]

# 图片魔数：服务器往 socket 推图片是非常反常的行为，验证码极可能以图片下发
IMAGE_MAGICS = [
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"\xff\xd8\xff", "JPEG"),
    (b"GIF87a", "GIF"), (b"GIF89a", "GIF"),
]

# 豁免 RC4 的 opcode。来自 RedWar_2026073102.swf 反汇编：
#   接收 Transport._-29U 偏移 761~1055 的比较链（533/552/553/560/643）
#   发送 Transport.Send  偏移 271~504 的比较链（1038/1052/1053/1109）
# 这两个名单之外的 body 全部过 RC4，且连续消耗同一条密钥流。
EXEMPT_OPCODES = crypto.EXEMPT      # 单一来源，见 tankstorm/crypto.py

ENC_DIR = "enc"          # logs/enc/ 存重点加密载荷原始字节
ENTROPY_MIN_LEN = 128    # 熵只用来交叉验证白名单，不再作为判定依据


# ---------------------------------------------------------------- 小工具

def _entropy(b: bytes) -> float:
    """香农熵（比特/字节）。密文接近 8，protobuf 文本一般 4~6。"""
    if not b:
        return 0.0
    n = len(b)
    return -sum((c / n) * math.log2(c / n) for c in Counter(b).values())


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


def _pb_ok(body: bytes) -> bool:
    """body 能否整体解析成合法 protobuf（字段号>0，wire type 只能 0/1/2/5，长度刚好吃完）。"""
    n = len(body)
    if n == 0:
        return True
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


def _readable_text(body: bytes) -> str:
    """从 protobuf body 里粗略抽出可读文本（中文/ASCII），用于关键词匹配与人工辨认。"""
    try:
        txt = body.decode("utf-8", "ignore")
    except Exception:
        return ""
    out = []
    for ch in txt:
        if ch.isprintable() and (ch.isascii() or "一" <= ch <= "鿿"):
            out.append(ch)
        else:
            out.append(" ")
    return " ".join("".join(out).split())


class FrameReader:
    """把 TCP 字节流按 [2字节长度] 切成协议帧。TCP 会粘包/拆包，必须缓冲重组。"""

    MAX_FRAME = 1 << 20      # 1MB，超过必然是失步而不是真的大包

    def __init__(self, on_desync=None):
        self.buf = bytearray()
        self.on_desync = on_desync
        self._preamble = True      # 连接开头可能有非帧结构的网关头
        self.pos = 0               # buf[0] 在本方向字节流里的绝对偏移

    def _eat_preamble(self) -> bool:
        """TGW 网关头（tgw_l7_forward\\r\\nHost: …\\r\\n\\r\\n）不是长度前缀帧，
        它会让分帧器从第一个字节就顶死（0x7467 被当成长度 29799 一直等）。
        真帧的头两字节是大端长度，小于 0x2000 时首字节在 0x00~0x1f，不可能是可打印
        ASCII —— 所以"首字节可打印"是安全的判据。"""
        if not self._preamble or len(self.buf) < 4:
            return False
        if not all(32 <= c < 127 for c in self.buf[:4]):
            self._preamble = False
            return False
        end = self.buf.find(b"\r\n\r\n", 0, 256)
        if end < 0:
            return len(self.buf) < 256      # 还没收全，继续等
        del self.buf[:end + 4]
        self.pos += end + 4
        self._preamble = False
        log.debug("跳过 %d 字节网关头前缀", end + 4)
        return False

    def feed(self, data: bytes) -> list:
        """喂入新收到的字节，返回 [(opcode_hex, seq, body, body在流中的绝对偏移), ...]。"""
        self.buf.extend(data)
        if self._eat_preamble():
            return []
        frames = []
        while len(self.buf) >= 2:
            ln = struct.unpack(">H", self.buf[:2])[0]
            if ln < 6 or ln > self.MAX_FRAME:   # 至少要放下 opcode(2)+seq(4)
                dropped = len(self.buf)
                log.warning("帧解析失步（长度=%d），丢弃 %d 字节缓冲", ln, dropped)
                if self.on_desync:
                    self.on_desync(ln, bytes(self.buf[:64]), dropped)
                self.buf.clear()
                self.pos += dropped
                break
            if len(self.buf) < 2 + ln:          # 还没收全，等下一批
                break
            chunk = bytes(self.buf[2:2 + ln])
            del self.buf[:2 + ln]
            frames.append((chunk[:2].hex(),
                           struct.unpack(">I", chunk[2:6])[0],
                           chunk[6:],
                           self.pos + 8))       # body 起始处的绝对偏移
            self.pos += 2 + ln
        return frames

    def pending(self) -> int:
        return len(self.buf)


class Recorder:
    """录制双向消息，并对异常事件回调告警。"""

    def __init__(self, config: dict, on_alert=None, on_super_storm=None):
        conf = (config.get("录制", {}) or {})
        self.enabled = conf.get("启用", True)
        self.alert_unknown = conf.get("未知消息告警", True)
        self.alert_keywords = conf.get("关键词告警", True)
        self.keep_body_max = int(conf.get("单条最大记录字节", 16384))
        self.record_out = conf.get("录制上行", True)      # 客户端发出的包也记
        self.dump_enc = conf.get("导出加密载荷", True)     # 加密 body 存独立 .bin
        self.auto_reject = conf.get("自动拒绝超级强攻", False)
        self.on_alert = on_alert
        self.on_super_storm = on_super_storm   # 收到 027c 时的回调
        # 每种服务器消息的最新一条解码结果 {消息名: (到达序号, 字段字典)}。
        # 每日任务靠它读"剩余免费次数"再决定发不发，避免免费用完后扣券/扣勋章。
        #
        # 第一项是**单调递增的序号**，不是时间戳。曾经用 time.time()，
        # 结果在 Windows 上翻了车：time.time() 的粒度约 15.6ms，
        # 服务器回得快时，"发请求的时刻"和"响应到达的时刻"会落在同一个 tick 上，
        # 于是 `到达时刻 > 发送时刻` 不成立，明明收到了却判成超时。
        # 序号是精确的，不受时钟粒度影响。
        self.latest = {}
        self._latest_seq = 0
        # 同名消息的近几条历史 {消息名: [(序号, 字段字典), ...]}。
        # 服务器常常连着推同名但 type 不同的两条（实测 RseWPCBaseOpen 先来
        # type:0 带 leftFreeCnt，紧跟着 type:3 只有擂台信息），只留最后一条
        # 就会把真正有用的那条冲掉。闸门要按内容挑，所以得留一小段历史。
        self.recent = {}
        self.reader = FrameReader(on_desync=self._note_desync)          # s2c
        self.reader_out = FrameReader(on_desync=self._note_desync)      # c2s
        self.counts = {}
        self.counts_out = {}
        self.enc_ops = {}          # opcode -> 见到的加密载荷条数
        self._alerted = set()      # 同一 opcode 只在首次出现时告警，避免刷屏
        self._last_alert_ts = 0.0
        self._path = None
        self._fh = None
        # 判断"广播消息是否与我有关"的标识：uid，以及可在配置里补充的游戏昵称
        self.identity = {str(x).strip() for x in conf.get("我的标识", []) if str(x).strip()}
        # 自己的 uid，由 enable_crypto() 从 FlashVars 上下文里填。
        # 有些请求（争霸战挑战的 uidself）要显式带自己的 uid，而服务端从不回显它。
        self.uid = ""

        # 刚连上时服务器会一口气推几十条消息（登录爆发期），其中不少 opcode 只在
        # 登录时出现一次。这段时间只"学"不"报"，否则每次连接都被这批消息刷屏。
        # 超级强攻这类事件发生在稳定运行期，不会落在这个窗口里。
        self.warmup_sec = float(conf.get("登录静默秒", 20))
        self._session_start = 0.0
        self._session_id = time.strftime("%Y%m%d-%H%M%S")
        self._enc_index = {"c2s": 0, "s2c": 0}
        self._warned = set()
        self.learned = self._load_learned()

        # 实时解密。密钥三要素（uid/sid/level/firstLogin）全在 FlashVars 里，
        # connect 之前就齐了，所以可以边收边解。
        # 状态机：off（没启用）→ probing（验证密钥）→ ok / failed
        self.live_decrypt = conf.get("实时解密", True)
        self._rc4 = {}
        self._crypto = "off"
        self._probe = [0, 0]        # [合法数, 已验数]
        self._rc4_out_rec = None    # 录制上行专用的第二条 c2s 密钥流

        # 原始字节流旁路。默认**开启**，而且不该关：RC4 密钥流在同方向的
        # 非豁免 body 之间连续累积，唯一保险的做法就是把整条流从第一个字节
        # 一字不差地存下来。解密用 tools/redwar_rc4.py。
        raw = conf.get("原始流", {}) or {}
        self.stream = StreamRecorder(
            base_dir=raw.get("目录", "logs/streams"),
            enabled=raw.get("启用", True),
            keep_sessions=int(raw.get("保留会话数", 10)),
            max_bytes=int(raw.get("单会话上限MB", 256)) * 1024 * 1024,
            log=log, hook=self._on_bytes,
        )

    # ---------- 已知 opcode 的持久化学习 ----------

    def _learned_path(self) -> str:
        return os.path.join(LOG_DIR, LEARNED_FILE)

    def _load_learned(self) -> set:
        try:
            with open(self._learned_path(), encoding="utf-8") as f:
                return set(json.load(f).get("opcodes", []))
        except (OSError, ValueError):
            return set()

    def _learn(self, op: str) -> None:
        """记住这个 opcode，之后（含重启后）不再作为"新类型"告警。"""
        if op in self.learned:
            return
        self.learned.add(op)
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(self._learned_path(), "w", encoding="utf-8") as f:
                json.dump({"_说明": "运行中观察到的良性 opcode，避免重启后重复告警",
                           "opcodes": sorted(self.learned)}, f,
                          ensure_ascii=False, indent=1)
        except OSError as exc:
            log.debug("保存已学习 opcode 失败(忽略): %s", exc)

    # ---------- 会话 ----------

    def on_connect(self) -> None:
        """每次 socket 连接建立时调用，重新开始登录静默窗口、重置分帧缓冲。

        加密载荷序号也在这里归零：RC4 状态随连接重置，不同连接的密文
        绝对不能混在一条链里喂。
        """
        self._session_start = time.time()
        self._session_id = time.strftime("%Y%m%d-%H%M%S")
        self._enc_index = {"c2s": 0, "s2c": 0}
        self._rc4, self._crypto, self._probe = {}, "off", [0, 0]
        self.reader = FrameReader(on_desync=self._note_desync)
        self.reader_out = FrameReader(on_desync=self._note_desync)

    def seq_mark(self):
        """取当前的消息到达序号。之后只认序号比它大的消息 = "这之后才到的"。"""
        return self._latest_seq

    def wrap(self, sock, **meta):
        """在 connect 之后、发第一个字节之前调用，返回带旁路的 socket。

        这样上行（c2s）也能被记录 —— 原来的 feed() 只喂了下行。
        """
        try:
            self.stream.open_session(**meta)
        except Exception as exc:
            log.debug("开原始流会话失败(忽略): %s", exc)
        return self.stream.wrap(sock)

    def enable_crypto(self, ctx: dict) -> bool:
        """用 FlashVars 的 uid/sid/level/firstLogin 开启实时解密。

        必须在 connect 之后、收到第一条非豁免消息之前调用 —— RC4 密钥流从
        第一条非豁免 body 开始累积，晚一步就永远对不上。

        密钥对不对不能预先知道，所以先进 probing：拿前几条解出来的 body 试
        protobuf 校验，通不过就退回"只录不解"，原始流照样留着供离线解密。
        """
        # 顺手把 uid 记下来。它是 FlashVars 里的，服务端的回包里一次都没出现过，
        # 而争霸战挑战（RceArenaOpt.uidself）这类请求必须显式带上自己的 uid。
        # 放在 live_decrypt 判断**之前**：实时解密关掉时 uid 照样是有效的。
        self.uid = str((ctx or {}).get("uid") or "")
        if not self.live_decrypt:
            return False
        rc4, why = crypto.from_ctx(ctx or {})
        if not rc4:
            log.info("实时解密未启用（%s）—— 原始流照录，可事后用 "
                     "tools/redwar_rc4.py 解", why)
            return False
        self._rc4, self._crypto, self._probe = rc4, "probing", [0, 0]

        # 上行**录制**要用一条独立的同源密钥流，绝不能和发送共用一个实例。
        #
        # RC4 是有状态的流密码：crypt() 一次就往前走 len(body) 个字节。
        # sender.send_frame 拿 rec.rc4_c2s 加密发出去，紧接着旁路又把这份字节
        # 喂回 feed(data,"c2s")，如果还用同一个实例去"解密留档"，
        # 同一帧就把密钥流推进了两次 —— 第一帧还对，从第二帧起我们加密的位置
        # 就比服务端预期多走一倍，服务端解出来是乱码，于是**默默丢弃**，
        # 表现就是"请求发出去了但永远没有回包"。2026-08-12 实盘定位到这里。
        #
        # 两条流同源、各自每帧只走一次，因此始终与服务端对齐。
        rec_rc4, _ = crypto.from_ctx(ctx or {})
        self._rc4_out_rec = (rec_rc4 or {}).get("c2s")
        log.info("实时解密已就绪：%s", why)
        return True

    @property
    def rc4_c2s(self):
        """C→S 方向的 RC4 实例（发送非豁免包时必须用同一个实例加密）。

        没有可用实例（未启用/自检失败）时返回 None。
        """
        if self._crypto == "failed":
            return None
        return self._rc4.get("c2s")

    def _decrypt(self, direction, op, body):
        """解一条非豁免 body。必须**无条件**调用，漏一条密钥流就永久错位。

        上行用的是录制专用的那条密钥流（_rc4_out_rec），不是发送用的那条，
        否则同一帧会把发送密钥流推进两次。见 enable_crypto 里的说明。
        """
        c = self._rc4_out_rec if direction == "c2s" else self._rc4.get(direction)
        if c is None or self._crypto == "failed":
            return None
        plain = c.crypt(body)
        if self._crypto == "probing" and direction == "s2c" and len(body) >= 4:
            ok, tot = self._probe
            self._probe = [ok + (1 if _pb_ok(plain) else 0), tot + 1]
            if self._probe[1] >= 6:
                if self._probe[0] >= 5:
                    self._crypto = "ok"
                    log.info("实时解密自检通过（%d/%d 条解出合法 protobuf）",
                             *self._probe)
                else:
                    self._crypto = "failed"
                    self._rc4 = {}
                    log.warning("实时解密自检失败（%d/%d）—— 密钥可能不对或流有缺口。"
                                "已退回只录不解，用 tools/redwar_rc4.py 事后解。",
                                *self._probe)
                    return None
        return plain

    def set_identity(self, *ids) -> None:
        """登录后把本次会话的 uid 等标识告诉录制器（用于过滤无关的广播）。"""
        for i in ids:
            if i:
                self.identity.add(str(i).strip())

    def note(self, event: str, **kv) -> None:
        """给原始流打事件标记（如 login_ok / sid）。"""
        try:
            self.stream.mark(event, **kv)
            self.stream.set_meta(**kv)
        except Exception:
            pass

    def _about_me(self, text: str) -> bool:
        return any(i in text for i in self.identity) if self.identity else False

    def _warn_once(self, key, fmt, *args):
        if key in self._warned:
            return
        self._warned.add(key)
        log.warning(fmt, *args)

    def _note_desync(self, ln, head, dropped):
        # RC4 密钥流按序累积：丢掉的字节里只要有一条非豁免 body，
        # 这条流从此永久解不开，只能断开重连重录。
        log.error("⚠ 分帧失步，丢弃 %d 字节 —— 本连接的 RC4 密钥流从此报废，"
                  "该方向后续消息无法解密（重连会重建密钥流）", dropped)
        self._write({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "op": "----",
                     "desync": True, "bad_len": ln, "dropped": dropped,
                     "head": head.hex(),
                     "note": "密钥流已报废，此后的加密消息解不开"})
        try:
            self.stream.mark("desync", dropped=dropped)
            self.stream.set_meta(keystream_broken=True)
        except Exception:
            pass

    # ---------- 落盘 ----------

    def _file(self):
        today = date.today().isoformat()
        want = os.path.join(LOG_DIR, f"frames-{today}.jsonl")
        if self._path != want:
            if self._fh:
                try:
                    self._fh.close()
                except OSError:
                    pass
            os.makedirs(LOG_DIR, exist_ok=True)
            self._fh = open(want, "a", encoding="utf-8")
            self._path = want
        return self._fh

    def _write(self, rec: dict):
        try:
            f = self._file()
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
        except OSError as exc:
            log.warning("写录制日志失败: %s", exc)

    def _dump_enc(self, op: str, body: bytes, direction: str,
                  index: int, off: int) -> str:
        """重点加密载荷单独存成 .bin，免去 hex 往返，便于单独查看。

        文件名带「会话时间戳 + 本连接内序号」：密钥流按这个顺序累积。
        注意完整解密要靠 logs/streams/ 的原始流，这里只是方便单独取用。
        """
        if not self.dump_enc:
            return ""
        d = os.path.join(LOG_DIR, ENC_DIR)
        name = (f"{self._session_id}-{index:03d}-{direction}-{op}"
                f"-off{off}-{len(body)}B.bin")
        try:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, name), "wb") as f:
                f.write(body)
            return os.path.join(ENC_DIR, name)
        except OSError as exc:
            log.warning("导出加密载荷失败: %s", exc)
            return ""

    # ---------- 主入口 ----------

    def _on_bytes(self, direction, data):
        """StreamRecorder 的旁路回调：双向字节都从这里进来。"""
        if direction == "c2s":
            if self.record_out:
                self.feed(data, "c2s")
        else:
            self.feed(data, "s2c")

    def feed(self, data: bytes, direction: str = "s2c") -> None:
        """把 socket 收发的原始字节喂进来。默认下行，兼容原有调用方式。"""
        if not self.enabled:
            return
        outgoing = direction == "c2s"
        reader = self.reader_out if outgoing else self.reader
        counts = self.counts_out if outgoing else self.counts

        for op, seq, body, off in reader.feed(data):
            counts[op] = counts.get(op, 0) + 1

            # 登录行 a,{uid},{secret} 和 TGW 头是 Transport 之外的裸写，
            # 不经过 Send()，绝不能算进密钥流。上行真帧的 opcode 都是 04xx。
            real_frame = op.startswith("04") if outgoing else True
            # 加密判定：按 SWF 里的 opcode 白名单，不再猜。空 body 不消耗密钥流。
            encrypted = (real_frame and bool(body)
                         and op not in EXEMPT_OPCODES[direction])
            # 必须无条件解，且每条只解一次 —— 漏一条密钥流就永久错位
            plain = self._decrypt(direction, op, body) if encrypted else None
            ent = _entropy(body) if len(body) >= ENTROPY_MIN_LEN else None
            if encrypted:
                self.enc_ops[op] = self.enc_ops.get(op, 0) + 1
            elif ent is not None and ent >= 7.5 and not _pb_ok(body):
                # 豁免名单说该明文，实测却是高熵密文 —— 游戏改版了，名单要更新
                self._warn_once(f"exempt-mismatch-{op}",
                                "opcode %s 在豁免名单里却是高熵密文（熵 %.2f）—— "
                                "游戏可能改版，白名单需要重新逆向", op, ent)

            rec = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "op": op, "seq": seq, "len": len(body),
            }
            if SCH:
                nm = SCH.name_of(op)
                if nm != op:
                    rec["msg"] = nm
            if outgoing:
                rec["dir"] = "c2s"
                # 登录行 a,{uid},{secret} 是「2字节长度 + 纯文本」，没有 opcode/seq，
                # 会被当成 op=612c('a,') 解析。长度前缀是对的所以不会失步，只是名字没意义。
                if not op.startswith("04"):
                    rec["note"] = "非标准帧（多半是登录行 a,uid,secret）"

            data = None
            if encrypted:
                # index 是本方向第几条「消耗密钥流」的 body —— 离线解密时必须
                # 严格按这个顺序喂。stream_off 能回原始流精确定位。
                idx = self._enc_index[direction]
                self._enc_index[direction] = idx + 1
                rec["enc"] = {"index": idx, "session": self._session_id,
                              "stream_off": off}
                if ent is not None:
                    rec["enc"]["entropy"] = round(ent, 3)

                if plain is None:
                    # 没开实时解密或自检没过：留密文取证，不做文本分析
                    rec["sha256"] = hashlib.sha256(body).hexdigest()
                    rec["hex"] = body.hex()
                    if op in EVENT_SIGNATURES or (
                            not outgoing and op not in KNOWN_OPCODES
                            and op not in self.learned):
                        blob = self._dump_enc(op, body, direction, idx, off)
                        if blob:
                            rec["blob"] = blob
                    self._write(rec)
                    if not outgoing:
                        self._maybe_flag(op, seq, body, "", rec)
                    continue

                # 解开了：换成明文往下走，关键词/图片检测因此对加密消息也生效
                rec["enc"]["decrypted"] = True
                body = plain
                if SCH:
                    data = SCH.decode(plain, op)
                    if data is not None:
                        rec["data"] = data
                        # 缓存最新一条，供每日任务"先读状态再决策"用
                        # （如 RseWPCExplore.leftFreeCnt 剩余免费探索次数）
                        if not outgoing:
                            self._latest_seq += 1
                            nm = rec.get("msg", op)
                            self.latest[nm] = (self._latest_seq, data)
                            hist = self.recent.setdefault(nm, [])
                            hist.append((self._latest_seq, data))
                            if len(hist) > 8:
                                del hist[:-8]

            # ---- 以下为明文消息的原有逻辑，行为保持不变 ----
            text = _readable_text(body) if len(body) <= 65536 else ""
            signature = EVENT_SIGNATURES.get(op)   # 已确认的重要事件，永远告警
            unknown = (not outgoing and op not in KNOWN_OPCODES
                       and op not in self.learned)
            hit_kw = ([k for k in ALERT_KEYWORDS if k in text]
                      if self.alert_keywords and not outgoing else [])
            image = next((n for m, n in IMAGE_MAGICS if m in body), None)

            in_warmup = bool(self._session_start) and \
                (time.time() - self._session_start) < self.warmup_sec
            if unknown:
                self._learn(op)

            # 广播消息里全是别人的昵称，只有提到我自己时关键词才算数
            suppressed = []
            if hit_kw and op in BROADCAST_OPCODES and not self._about_me(text):
                suppressed, hit_kw = hit_kw, []

            if signature:
                rec["event"] = signature
            if unknown:
                rec["unknown"] = True
                if in_warmup:
                    rec["warmup"] = True      # 登录爆发期学到的，未告警
            if hit_kw:
                rec["keywords"] = hit_kw
            if suppressed:
                rec["keywords_ignored"] = suppressed   # 留痕但不告警，便于事后核对
            if image:
                rec["image"] = image

            # 高频大包只记摘要；其余（尤其未知/命中关键词/带图片的）记完整 body
            important = unknown or hit_kw or image or signature
            if op in BULK_OPCODES and not important:
                if counts[op] % 50 != 1:      # 每 50 条留 1 条摘要即可
                    continue
                rec["note"] = "bulk-sampled"
                rec["text"] = text[:120]
            else:
                # 已确认的事件消息完整保存，绝不截断——这是最不能丢的数据
                cap = len(body) if signature else self.keep_body_max
                rec["hex"] = body[:cap].hex()
                if len(body) > cap:
                    rec["truncated"] = len(body)   # 标明原始长度，便于发现丢数据
                if text:
                    rec["text"] = text[:500]
            self._write(rec)

            if outgoing:
                continue
            alert_unknown = unknown and self.alert_unknown and not in_warmup
            if alert_unknown or hit_kw or image or signature:
                self._maybe_alert(op, seq, body, text, alert_unknown,
                                  hit_kw, image, signature, data=data)

            # ---- 超级强攻自动拒绝 ----
            if op == "027c" and self.auto_reject and self.on_super_storm and data:
                try:
                    self.on_super_storm(data)
                except Exception as exc:
                    log.warning("超级强攻自动拒绝回调失败: %s", exc)

    def _maybe_flag(self, op, seq, body, text, rec):
        """加密载荷的告警：沿用原有触发条件，只是在理由里补一句"载荷加密"。"""
        signature = EVENT_SIGNATURES.get(op)
        unknown = op not in KNOWN_OPCODES and op not in self.learned
        if unknown:
            self._learn(op)
        in_warmup = bool(self._session_start) and \
            (time.time() - self._session_start) < self.warmup_sec
        alert_unknown = unknown and self.alert_unknown and not in_warmup
        if alert_unknown or signature:
            self._maybe_alert(op, seq, body, text, alert_unknown, [], None,
                              signature, encrypted=True)

    def _maybe_alert(self, op, seq, body, text, unknown, hit_kw, image=None,
                     signature=None, encrypted=False, data=None):
        # 已确认事件/关键词/图片：每次都报（事关 5 分钟窗口）；
        # 仅"未知 opcode"：每种只报一次，避免良性新类型刷屏
        if not hit_kw and not image and not signature:
            if op in self._alerted:
                return
            self._alerted.add(op)
        # 已确认事件不受防抖限制——宁可重复也不能漏
        if not signature and time.time() - self._last_alert_ts < 3:
            return
        self._last_alert_ts = time.time()

        opname = SCH.describe(op) if SCH else op
        reason = []
        if signature:
            reason.append(signature)
        if hit_kw:
            reason.append("命中关键词 " + "/".join(hit_kw))
        if image:
            reason.append(f"收到 {image} 图片（可能是验证码）")
        if unknown:
            reason.append(f"新消息类型 {opname}")
        if encrypted:
            reason.append("载荷已加密（已完整存到 logs/enc/）")
        if data:
            # 解出来的关键字段直接进推送 —— 被谁打的、打的是谁
            key = [f"{k}={v}" for k, v in data.items()
                   if k in ("atkName", "atkUid", "deftName", "deftUid",
                            "type", "nResult") and v not in (None, "", 0)]
            if key:
                reason.append("｜".join(key))
        log.warning("⚠️ 异常事件：%s | %s len=%d | %s",
                    "；".join(reason), opname, len(body), text[:120])
        if self.on_alert:
            try:
                self.on_alert(op, seq, body, text, reason)
            except Exception as exc:
                log.warning("告警回调失败: %s", exc)

    def close(self):
        for r, name in ((self.reader, "s2c"), (self.reader_out, "c2s")):
            if r.pending():
                log.debug("%s 分帧缓冲还剩 %d 字节未成帧（连接中断时的半个包）",
                          name, r.pending())
        try:
            self.stream.close("recorder_close")
        except Exception:
            pass
        if self._fh:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

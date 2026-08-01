"""服务器消息录制 + 异常事件实时告警。

用途：像"超级强攻令"这种事件很随机（对方用令后你要在 5 分钟内输验证码，否则基地被强攻），
没法蹲着抓包。但保活守护进程本来就 24 小时连在游戏 socket 上，让它顺便：

  1. 把服务器发来的消息按协议帧切好，落盘到 logs/frames-YYYY-MM-DD.jsonl；
  2. 一旦发现"没见过的消息类型"或命中关键词（验证码/强攻/进攻…），
     立刻通过 PushPlus 推到你微信 —— 你就能在 5 分钟窗口内自己打开游戏处理。

注意：本模块只做"观察 + 通知你"，不会替你回应验证码。验证码是游戏用来确认真人在场的
机制，自动过验证码属于绕过人机验证，不在本项目范围内；及时叫你本人去处理才是正路。

帧格式（抓包确认）：[2字节大端长度 N][2字节 opcode][4字节 seq][body]，整帧 = 2 + N。
"""

import json
import os
import struct
import time
from datetime import date

from .log import LOG_DIR, get_logger

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


class FrameReader:
    """把 TCP 字节流按 [2字节长度] 切成协议帧。TCP 会粘包/拆包，必须缓冲重组。"""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes) -> list:
        """喂入新收到的字节，返回本次能完整解析出的帧 [(opcode_hex, seq, body), ...]。"""
        self.buf.extend(data)
        frames = []
        while len(self.buf) >= 2:
            ln = struct.unpack(">H", self.buf[:2])[0]
            if ln < 6:                      # 至少要放下 opcode(2)+seq(4)
                log.warning("帧解析失步（长度=%d），丢弃 %d 字节缓冲", ln, len(self.buf))
                self.buf.clear()
                break
            if len(self.buf) < 2 + ln:      # 还没收全，等下一批
                break
            chunk = bytes(self.buf[2:2 + ln])
            del self.buf[:2 + ln]
            frames.append((chunk[:2].hex(),
                           struct.unpack(">I", chunk[2:6])[0],
                           chunk[6:]))
        return frames


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


class Recorder:
    """录制服务器消息，并对异常事件回调告警。"""

    def __init__(self, config: dict, on_alert=None):
        conf = (config.get("录制", {}) or {})
        self.enabled = conf.get("启用", True)
        self.alert_unknown = conf.get("未知消息告警", True)
        self.alert_keywords = conf.get("关键词告警", True)
        self.keep_body_max = int(conf.get("单条最大记录字节", 2048))
        self.on_alert = on_alert
        self.reader = FrameReader()
        self.counts = {}
        self._alerted = set()      # 同一 opcode 只在首次出现时告警，避免刷屏
        self._last_alert_ts = 0.0
        self._path = None
        self._fh = None
        # 判断"广播消息是否与我有关"的标识：uid，以及可在配置里补充的游戏昵称
        self.identity = {str(x).strip() for x in conf.get("我的标识", []) if str(x).strip()}

        # 刚连上时服务器会一口气推几十条消息（登录爆发期），其中不少 opcode 只在
        # 登录时出现一次。这段时间只"学"不"报"，否则每次连接都被这批消息刷屏。
        # 超级强攻这类事件发生在稳定运行期，不会落在这个窗口里。
        self.warmup_sec = float(conf.get("登录静默秒", 20))
        self._session_start = 0.0
        self.learned = self._load_learned()

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

    def on_connect(self) -> None:
        """每次 socket 连接建立时调用，重新开始登录静默窗口。"""
        self._session_start = time.time()

    def set_identity(self, *ids) -> None:
        """登录后把本次会话的 uid 等标识告诉录制器（用于过滤无关的广播）。"""
        for i in ids:
            if i:
                self.identity.add(str(i).strip())

    def _about_me(self, text: str) -> bool:
        return any(i in text for i in self.identity) if self.identity else False

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

    # ---------- 主入口 ----------

    def feed(self, data: bytes) -> None:
        """把 socket 收到的原始字节喂进来。"""
        if not self.enabled:
            return
        for op, seq, body in self.reader.feed(data):
            self.counts[op] = self.counts.get(op, 0) + 1
            text = _readable_text(body) if len(body) <= 65536 else ""

            unknown = op not in KNOWN_OPCODES and op not in self.learned
            hit_kw = [k for k in ALERT_KEYWORDS if k in text] if self.alert_keywords else []
            image = next((n for m, n in IMAGE_MAGICS if m in body), None)

            # 新 opcode 一律记下来；登录爆发期内只学不报
            in_warmup = bool(self._session_start) and \
                (time.time() - self._session_start) < self.warmup_sec
            if unknown:
                self._learn(op)

            # 广播消息里全是别人的昵称，只有提到我自己时关键词才算数
            suppressed = []
            if hit_kw and op in BROADCAST_OPCODES and not self._about_me(text):
                suppressed, hit_kw = hit_kw, []

            rec = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "op": op, "seq": seq, "len": len(body),
            }
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
            # 高频大包只记摘要；其余（尤其未知/命中关键词/带图片的）记完整 body 供事后分析
            important = unknown or hit_kw or image
            if op in BULK_OPCODES and not important:
                if self.counts[op] % 50 != 1:      # 每 50 条留 1 条摘要即可
                    continue
                rec["note"] = "bulk-sampled"
                rec["text"] = text[:120]
            else:
                rec["hex"] = body[:self.keep_body_max].hex()
                if text:
                    rec["text"] = text[:500]
            self._write(rec)

            alert_unknown = unknown and self.alert_unknown and not in_warmup
            if alert_unknown or hit_kw or image:
                self._maybe_alert(op, seq, body, text, alert_unknown, hit_kw, image)

    def _maybe_alert(self, op, seq, body, text, unknown, hit_kw, image=None):
        # 关键词命中/收到图片：每次都报（事关 5 分钟窗口）；仅"未知 opcode"：每种只报一次
        if not hit_kw and not image:
            if op in self._alerted:
                return
            self._alerted.add(op)
        if time.time() - self._last_alert_ts < 3:   # 简单防抖
            return
        self._last_alert_ts = time.time()

        reason = []
        if hit_kw:
            reason.append("命中关键词 " + "/".join(hit_kw))
        if image:
            reason.append(f"收到 {image} 图片（可能是验证码）")
        if unknown:
            reason.append(f"新消息类型 {op}")
        log.warning("⚠️ 异常事件：%s | op=%s len=%d | %s",
                    "；".join(reason), op, len(body), text[:120])
        if self.on_alert:
            try:
                self.on_alert(op, seq, body, text, reason)
            except Exception as exc:
                log.warning("告警回调失败: %s", exc)

    def close(self):
        if self._fh:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

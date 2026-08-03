"""原始流录制 —— 为 RC4 解密准备数据。

为什么不能沿用现在按帧采样的 frames-*.jsonl
-------------------------------------------
RC4 是流密码：密钥流在一条 TCP 连接内连续累积、中途不重置。
少记一个字节，后面全部错位；采样丢一个大包，之后的全都解不开。
所以必须拿到「从连接建立那一刻起、按发生顺序、一个字节不缺」的字节流，
而且上下行密钥不同（见 docs/加密与协议还原.md），必须分开存。

设计
----
1. 录制侧只做一件事：把 send/recv 的字节原样落盘 + 记一条偏移。
   不切帧、不解析、不采样、不截断 —— 解析 bug 不可能污染数据。
2. 切帧、找加密起点、试密钥、解密，全部放到离线工具
   tools/redwar_rc4.py 里做，数据只抓一次，可以反复重跑。
3. 每条 TCP 连接一个独立目录（RC4 状态随连接重置，混在一起就废了）。

产物
----
logs/streams/<会话ID>/
    c2s.bin       客户端 -> 服务器 原始字节流
    s2c.bin       服务器 -> 客户端 原始字节流
    chunks.jsonl  每次 socket 调用一条 {"t":..,"d":"s2c","o":偏移,"n":长度}
                  以及 mark 事件 {"t":..,"e":"login_ok","c":..,"s":..,"sid":".."}
    meta.json     会话元信息（server/port/uid/sid/起止时间/字节数）

隐私
----
c2s.bin 和 meta.json 会包含 uid / secret / openkey / sid 等真实凭据
（登录握手是明文）。确保 logs/ 在 .gitignore 里；转发给别人之前想清楚，
这几个字段足够别人登录你的账号。

接线（socket_keepalive.py）
---------------------------
    from tankstorm import stream_recorder

    rec = stream_recorder.from_config(config, log=log)
    ...
    sock.connect((host, port))
    rec.open_session(host=host, port=port, uid=ctx.get("uid"))
    sock = rec.wrap(sock)                      # 之后照常 sendall/recv
    ...
    rec.mark("login_ok", sid=ctx.get("sid"))   # 拿到 sid 时打个标记
    rec.set_meta(sid=ctx.get("sid"))
    ...
    finally:
        rec.close("disconnected")
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime

DEFAULT_DIR = "logs/streams"
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 帧结构备忘，写进 meta.json 供离线工具参考
FRAMING = "[u16be len][u16be op][u32be seq][body]; len 覆盖 op+seq+body"


def _now() -> float:
    return round(time.time(), 3)


class StreamRecorder:
    """socket 层旁路录制器。线程安全，可跨重连复用（每次 open_session 开新会话）。"""

    def __init__(self, base_dir: str = DEFAULT_DIR, enabled: bool = True,
                 keep_sessions: int = 10, max_bytes: int = 256 * 1024 * 1024,
                 log=None, hook=None):
        if not os.path.isabs(base_dir):
            base_dir = os.path.join(_REPO_ROOT, base_dir)
        self.base_dir = base_dir
        self.enabled = bool(enabled)
        # hook(direction, data)：字节旁路给上层做帧级分析。
        # 即使 enabled=False（不落盘）也照常触发 —— 这样 Recorder 永远能看到双向数据。
        self.hook = hook
        self.keep_sessions = max(1, int(keep_sessions))
        self.max_bytes = int(max_bytes)
        self.log = log
        self._lock = threading.RLock()
        self.session_dir = None
        self._f = {"c2s": None, "s2c": None}
        self._off = {"c2s": 0, "s2c": 0}
        self._chunks = None
        self._meta = {}
        self._stopped = False

    # ---------------------------------------------------------------- 会话

    def open_session(self, host=None, port=None, **meta):
        """开一个新会话目录。每条 TCP 连接（含每次重连）调一次。"""
        if not self.enabled:
            return None
        with self._lock:
            self._close_locked("reopen")
            ts = datetime.now()
            name = ts.strftime("%Y%m%d-%H%M%S-") + f"{ts.microsecond // 1000:03d}"
            d = os.path.join(self.base_dir, name)
            os.makedirs(d, exist_ok=True)
            self.session_dir = d
            # buffering=0：每次 write 直落系统调用，进程被 kill 也不丢尾巴
            self._f["c2s"] = open(os.path.join(d, "c2s.bin"), "ab", buffering=0)
            self._f["s2c"] = open(os.path.join(d, "s2c.bin"), "ab", buffering=0)
            self._chunks = open(os.path.join(d, "chunks.jsonl"), "a", encoding="utf-8")
            self._off = {"c2s": 0, "s2c": 0}
            self._stopped = False
            self._meta = {
                "session": name,
                "started_at": ts.isoformat(timespec="seconds"),
                "host": host,
                "port": port,
                "framing": FRAMING,
            }
            self._meta.update({k: v for k, v in meta.items() if v is not None})
            self._write_meta_locked()
            self._event_locked("session_open", host=host, port=port)
            self._prune_locked()
        self._say("原始流录制开始: %s", self.session_dir)
        return self.session_dir

    def close(self, reason: str = None):
        with self._lock:
            d = self.session_dir
            self._close_locked(reason)
        if d:
            self._say("原始流录制结束: %s (%s)", d, reason)

    def _close_locked(self, reason=None):
        if self.session_dir is None:
            return
        try:
            self._event_locked("session_close", reason=reason)
            self._meta["ended_at"] = datetime.now().isoformat(timespec="seconds")
            self._meta["bytes_c2s"] = self._off["c2s"]
            self._meta["bytes_s2c"] = self._off["s2c"]
            self._meta["close_reason"] = reason
            self._write_meta_locked()
        except Exception:
            pass
        for k in ("c2s", "s2c"):
            try:
                if self._f[k]:
                    self._f[k].close()
            except Exception:
                pass
            self._f[k] = None
        try:
            if self._chunks:
                self._chunks.close()
        except Exception:
            pass
        self._chunks = None
        self.session_dir = None

    # ---------------------------------------------------------------- 录制

    def on_send(self, data):
        self._record("c2s", data)

    def on_recv(self, data):
        self._record("s2c", data)

    def _record(self, direction, data):
        if not data:
            return
        if self.hook is not None:
            try:
                self.hook(direction, bytes(data))
            except Exception as exc:      # 上层分析炸了不能影响录制和保活
                self._say("录制 hook 异常(忽略): %s", exc)
        if not self.enabled:
            return
        n = len(data)
        with self._lock:
            f = self._f.get(direction)
            if f is None or self._stopped:
                return
            off = self._off[direction]
            if off + n > self.max_bytes:
                # 停在这里而不是截断中间：前缀依然是完整可解的
                self._stopped = True
                self._event_locked("stopped", reason="max_bytes", dir=direction)
                self._say("原始流达到单会话上限，停止录制（已有数据仍可用）")
                return
            mv = memoryview(data)
            while mv:
                w = f.write(mv)
                if not w:
                    break
                mv = mv[w:]
            self._off[direction] = off + n
            self._chunk_locked(direction, off, n)

    def mark(self, event: str, **kv):
        """打事件标记（login_ok / enc_start / 重连 …），会连当前双向偏移一起记下。"""
        if not self.enabled:
            return
        with self._lock:
            self._event_locked(event, **kv)

    def set_meta(self, **kv):
        """补写会话元信息，比如握手后才拿到的 sid。"""
        if not self.enabled:
            return
        with self._lock:
            if self.session_dir is None:
                return
            self._meta.update({k: v for k, v in kv.items() if v is not None})
            self._write_meta_locked()
            self._event_locked("meta", **kv)

    def wrap(self, sock):
        """包一层旁路。返回值可当普通 socket 用（sendall/recv/select 都正常）。"""
        if not self.enabled and self.hook is None:
            return sock
        return TappedSocket(sock, self)

    # ------------------------------------------------------------- 内部写盘

    def _chunk_locked(self, direction, off, n):
        if self._chunks is None:
            return
        try:
            self._chunks.write(
                '{"t":%.3f,"d":"%s","o":%d,"n":%d}\n' % (_now(), direction, off, n))
            self._chunks.flush()
        except Exception:
            pass

    def _event_locked(self, event, **kv):
        if self._chunks is None:
            return
        rec = {"t": _now(), "e": event,
               "c": self._off["c2s"], "s": self._off["s2c"]}
        rec.update({k: v for k, v in kv.items() if v is not None})
        try:
            self._chunks.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._chunks.flush()
        except Exception:
            pass

    def _write_meta_locked(self):
        if self.session_dir is None:
            return
        try:
            with open(os.path.join(self.session_dir, "meta.json"), "w",
                      encoding="utf-8") as f:
                json.dump(self._meta, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _prune_locked(self):
        """只保留最近 N 个会话，别把磁盘吃满。"""
        try:
            names = sorted(d for d in os.listdir(self.base_dir)
                           if os.path.isdir(os.path.join(self.base_dir, d)))
        except OSError:
            return
        cur = os.path.basename(self.session_dir or "")
        for name in names[:-self.keep_sessions]:
            if name == cur:
                continue
            shutil.rmtree(os.path.join(self.base_dir, name), ignore_errors=True)

    def _say(self, fmt, *args):
        if self.log is not None:
            try:
                self.log.info(fmt, *args)
                return
            except Exception:
                pass


class TappedSocket:
    """透明代理：所有属性转发给真 socket，收发字节顺手落盘。"""

    __slots__ = ("_sock", "_rec", "_closed")

    def __init__(self, sock, rec: StreamRecorder):
        object.__setattr__(self, "_sock", sock)
        object.__setattr__(self, "_rec", rec)
        object.__setattr__(self, "_closed", False)

    def __getattr__(self, name):
        return getattr(self._sock, name)

    # ---- 发送 -----------------------------------------------------------

    def sendall(self, data, *a, **kw):
        try:
            r = self._sock.sendall(data, *a, **kw)
        except Exception as e:
            # 可能已经发出去一部分但拿不到确切字节数：标脏，离线工具会提示
            self._rec.mark("send_error", intended=len(data), err=repr(e))
            raise
        self._rec.on_send(data)
        return r

    def send(self, data, *a, **kw):
        n = self._sock.send(data, *a, **kw)
        if n:
            self._rec.on_send(memoryview(data)[:n])
        return n

    def write(self, data):  # 有些代码习惯用 write
        return self.sendall(data)

    # ---- 接收 -----------------------------------------------------------

    def recv(self, bufsize, *a, **kw):
        data = self._sock.recv(bufsize, *a, **kw)
        if data:
            self._rec.on_recv(data)
        return data

    def recv_into(self, buffer, nbytes=0, *a, **kw):
        n = self._sock.recv_into(buffer, nbytes, *a, **kw)
        if n:
            self._rec.on_recv(memoryview(buffer)[:n])
        return n

    def read(self, n=65536):
        return self.recv(n)

    # ---- 其它 -----------------------------------------------------------

    def makefile(self, *a, **kw):
        # 走 makefile 的读写绕开了旁路，数据会缺 —— 必须让它显形
        self._rec.mark("tap_bypassed", how="makefile")
        self._rec.set_meta(tap_bypassed=True)
        self._rec._say("警告: 代码用了 sock.makefile()，这条路径不经过录制，流会不完整")
        return self._sock.makefile(*a, **kw)

    def close(self, *a, **kw):
        if not self._closed:
            object.__setattr__(self, "_closed", True)
            if self._rec.session_dir:
                self._rec.close("socket_close")
        return self._sock.close(*a, **kw)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __repr__(self):
        return f"<TappedSocket {self._sock!r}>"


# ------------------------------------------------------------------ 配置入口

def from_config(config: dict = None, log=None) -> StreamRecorder:
    """从 config.json 读取配置。缺省即开启。

    config.json 里加：
        "录制": {
            "原始流": {
                "启用": true,
                "目录": "logs/streams",
                "保留会话数": 10,
                "单会话上限MB": 256
            }
        }
    """
    cfg = {}
    if isinstance(config, dict):
        cfg = (config.get("录制") or {}).get("原始流") or {}
    return StreamRecorder(
        base_dir=cfg.get("目录", DEFAULT_DIR),
        enabled=cfg.get("启用", True),
        keep_sessions=int(cfg.get("保留会话数", 10)),
        max_bytes=int(cfg.get("单会话上限MB", 256)) * 1024 * 1024,
        log=log,
    )

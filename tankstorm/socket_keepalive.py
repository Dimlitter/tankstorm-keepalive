# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""保持在线守护进程：连上游戏 socket、登录、定时发心跳，掉线自动重连。

解决的问题：坦克风暴长时间无操作会弹"指挥官，您是否在线"并踢下线，导致基地
被打。真正维持在线的是 Flash 客户端与 tankstorm-proxy.sincetimes.com:8001 的 socket
连接 + 定时心跳。本守护进程用纯 Python socket 复刻这条连接。

工作流程：
  1. get_game_context() 拿最新 openid/openkey/uid/sid/secret/server/port（每次连接前刷新，
     因为 openkey 会过期）；
  2. 连 TCP 到 server:port，发登录握手（见 session.open_link）；
  2.5 收完登录推送，核对协议编号对照（见 session.check_protocol），没通过就断开；
  3. 每 interval 秒发一次心跳（protocol.build_heartbeat）；
  4. 读服务器数据，若命中"是否在线"探测包则回应；
  5. 连接断开 → 退避重连（并刷新 openkey）。

协议细节（登录/心跳字节）来自 protocol.json，由 Wireshark 抓包分析生成；
没有该文件时本模块会给出清晰提示并退出，不会瞎跑。

每日任务默认**不在这里跑**。保活和每日任务是两件事：保活是常驻的、唯一的职责
就是别掉线；每日任务是一次性的批处理，跑完就该结束。混在一起的坏处是每次重连
都会重跑一轮任务，而且任务出问题会牵连保活。需要"连上顺便领一轮"时显式传
with_daily=True（即 --keepalive --daily）。
"""

import socket
import time

from . import jobs, notify, protocol, sender, session
from .log import get_logger
from .qzone import get_game_context
from .recorder import Recorder

log = get_logger()

# 一次连接撑过这么久就算"稳定过"，断开后退避从最小值重新算
STABLE_SEC = 300


def _install_auto_reject(rec, sock, config: dict) -> None:
    """超级强攻自动拒绝：在当前会话的 sock 和 RC4 上下文里构造回调。"""
    def on_super_storm(data):
        rc4 = rec.rc4_c2s
        if rc4 is None:
            log.warning("自动拒绝超级强攻失败：RC4 C→S 实例不可用"
                        "（实时解密未启用或密钥自检失败）")
            return
        if sender.send_reject_super_storm(sock, rc4, data):
            notify.send(config, "🛡️ 坦克风暴：已自动拒绝超级强攻",
                        f"进攻方：{data.get('atkName', '?')}（{data.get('atkUid', '?')}）\n"
                        f"防守方：{data.get('deftName', '?')}（{data.get('deftUid', '?')}）\n\n"
                        f"已自动发送 RceSuperStormOpt type=2 拒绝包。\n"
                        f"如果服务端要求验证码才接受拒绝，此包可能被忽略，"
                        f"请立刻打开游戏确认。")
    rec.on_super_storm = on_super_storm
    log.info("超级强攻自动拒绝已就绪")


def _one_session(qq, spec: dict, config: dict, rec, with_daily: bool) -> str:
    """跑一次完整连接，直到断开。返回断开原因（字符串）。"""
    ctx = get_game_context(qq)
    if not ctx.get("openkey"):
        return "未取得 openkey（登录态可能失效）"

    # 告诉录制器"我是谁"，用于过滤广播里与我无关的关键词误报。
    # 守护进程要长跑，这里不能因为取标识失败就掀翻整个连接。
    try:
        rec.set_identity(ctx.get("uid"), ctx.get("openid"), getattr(qq, "uin", None))
    except Exception as exc:
        log.debug("设置录制标识失败(忽略): %s", exc)

    if spec.get("http_warmup", True):
        session.http_warmup(qq, ctx)

    interval = session.heartbeat_interval(spec, config)
    try:
        sock = session.open_link(spec, ctx, rec)
    except OSError as exc:
        return f"连接失败: {exc}"

    try:
        # 先收完登录推送、核对协议编号对照，通过了才发心跳和任何请求。
        # 心跳也算：旧版的心跳编号在 2026102102 里是 RceClearData
        session.drain(sock)
        why = session.check_protocol(rec, ctx, config)
        if why:
            return f"协议编号核对没通过：{why}"
        if rec.auto_reject:
            _install_auto_reject(rec, sock, config)
        hb = protocol.build_heartbeat(spec, ctx)

        if with_daily:
            try:
                jobs.daily_round(rec, sock, spec, ctx, config)
            except Exception as exc:
                log.error("每日任务执行异常（不影响保活）: %s", exc)

        last_beat = 0.0
        beats = 0
        sock.settimeout(1.0)
        while True:
            now = time.time()
            if now - last_beat >= interval:
                sock.sendall(hb)
                beats += 1
                last_beat = now
                if beats % 10 == 1:
                    log.info("心跳运行中（第 %d 次，每 %.0fs）", beats, interval)
                else:
                    log.debug("心跳 #%d", beats)
            # 读服务器数据（超时就继续发心跳）。sock 已被 rec.wrap() 包过，
            # 收到的字节会自动旁路进录制器，这里不用再喂。
            try:
                data = sock.recv(8192)
            except socket.timeout:
                continue
            if not data:
                return f"服务器关闭连接（已发 {beats} 次心跳）"
            reply = protocol.maybe_online_reply(spec, data, ctx)
            if reply:
                sock.sendall(reply)
                log.info("收到在线探测，已回应")
    except OSError as exc:
        return f"连接中断: {exc}"
    finally:
        session.close(sock)


def _log_stats(rec) -> None:
    if rec.counts:
        top = sorted(rec.counts.items(), key=lambda kv: -kv[1])[:8]
        log.info("本次录制消息统计（前 8 类）：%s",
                 "，".join(f"{o}×{c}" for o, c in top))
    if rec.counts_out:
        top = sorted(rec.counts_out.items(), key=lambda kv: -kv[1])[:5]
        log.info("上行消息统计：%s", "，".join(f"{o}×{c}" for o, c in top))
    if rec.enc_ops:
        log.info("本次加密消息：%s",
                 "，".join(f"{o}×{c}" for o, c in
                           sorted(rec.enc_ops.items(), key=lambda kv: -kv[1])[:8]))
        log.info("原始流在 %s，离线解密用 tools/redwar_rc4.py", rec.stream.base_dir)


def run(qq, config: dict, with_daily: bool = False) -> int:
    """保活守护进程。只负责别掉线。

    with_daily=True 时，每次连上后顺带跑一轮每日任务（`--keepalive --daily`）。
    """
    conf = config.get("保持活跃", {})
    if not conf.get("启用", False):
        log.info("保持活跃未启用（config.json 保持活跃.启用=false）")
        return 0

    try:
        spec = protocol.load_spec()
    except protocol.ProtocolNotConfigured as exc:
        log.error("%s", exc)
        return 2

    run_hours = float(conf.get("持续小时", 0))       # 0 = 一直跑
    min_backoff = float(conf.get("重连最小秒", 5))
    max_backoff = float(conf.get("重连最大秒", 120))
    started = time.time()
    backoff = min_backoff

    # 录制服务器消息；发现异常事件（如超级强攻的验证码通知）立刻推送到手机
    def on_alert(op, seq, body, text, reason):
        notify.send(config, "⚠️ 坦克风暴：检测到异常事件，请立刻查看游戏",
                    f"原因：{'；'.join(reason)}\n"
                    f"消息类型：{op}  长度：{len(body)}\n"
                    f"内容片段：{text[:200] or '(无可读文本)'}\n\n"
                    f"若是「超级强攻」验证码，只有约 5 分钟处理时间，"
                    f"请马上打开游戏输入验证码。")
    rec = Recorder(config, on_alert=on_alert)

    # 启动时若未登录（如服务器首次部署），也走"推送二维码"流程
    if not qq.is_valid():
        session.relogin_with_push(qq, config)

    log.info("保持活跃启动（Ctrl+C 停止）")
    try:
        while True:
            if run_hours and (time.time() - started) >= run_hours * 3600:
                log.info("达到设定运行时长，退出")
                break
            if not qq.is_valid():
                session.relogin_with_push(qq, config)

            t0 = time.time()
            reason = _one_session(qq, spec, config, rec, with_daily=with_daily)
            log.warning("本次连接结束：%s", reason)
            if time.time() - t0 >= STABLE_SEC:
                backoff = min_backoff        # 刚稳定跑过一段，退避从头算
            wait = min(backoff, max_backoff)
            log.info("%.0f 秒后重连…", wait)
            time.sleep(wait)
            backoff = min(backoff * 2, max_backoff)
    except KeyboardInterrupt:
        log.info("手动停止保持活跃")
    finally:
        rec.close()
        _log_stats(rec)
    return 0

"""保持在线守护进程：连上游戏 socket、登录、定时发心跳，掉线自动重连。

解决的问题：坦克风暴长时间无操作会弹"指挥官，您是否在线"并把你踢下线，导致基地
被打。真正维持在线的是 Flash 客户端与 tankstorm-proxy.sincetimes.com:8001 的 socket
连接 + 定时心跳。本守护进程用纯 Python socket 复刻这条连接。

工作流程：
  1. get_game_context() 拿最新 openid/openkey/uid/sid/secret/server/port（每次连接前刷新，
     因为 openkey 会过期）；
  2. 连 TCP 到 server:port，发登录握手（protocol.build_login）；
  3. 每 interval 秒发一次心跳（protocol.build_heartbeat）；
  4. 读服务器数据，若命中"是否在线"探测包则回应；
  5. 连接断开 → 退避重连（并刷新 openkey）。

协议细节（登录/心跳字节）来自 protocol.json，由 Wireshark 抓包分析生成；
没有该文件时本模块会给出清晰提示并退出，不会瞎跑。
"""

import socket
import time

from . import daily, notify, protocol, sender
from .log import get_logger
from .qzone import get_game_context
from .recorder import Recorder

log = get_logger()


def _connect(host: str, port: int, timeout: float = 15) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    return sock


def _http_warmup(qq, ctx: dict) -> None:
    """连 socket 前，复刻浏览器的 HTTP 调用（loadIdInfo.war），让服务器完成会话注册。
    失败不影响后续 socket 连接，仅记录调试日志。"""
    base = ctx.get("coinserver") or "https://tankstorm-qzone.sincetimes.com/"
    params = {"openid": ctx.get("openid", ""), "openkey": ctx.get("openkey", ""),
              "uid": ctx.get("uid", ""), "pf": ctx.get("pf", "qzone")}
    if not params["openid"]:
        return
    try:
        r = qq.session.get(base.rstrip("/") + "/loadIdInfo.war", params=params,
                           headers={"Referer": ctx.get("canvas_url", "")}, timeout=15)
        log.debug("warmup loadIdInfo.war -> %s %r", r.status_code, r.text[:40])
    except Exception as exc:
        log.debug("warmup 失败(忽略): %s", exc)


def _one_session(qq, spec: dict, conf: dict, config: dict, rec=None) -> str:
    """跑一次完整连接，直到断开。返回断开原因（字符串）。"""
    ctx = get_game_context(qq)
    host = ctx.get("server") or spec.get("default_host", "tankstorm-proxy.sincetimes.com")
    port = int(ctx.get("port") or spec.get("default_port", 8001))
    if not ctx.get("openkey"):
        return "未取得 openkey（登录态可能失效）"

    if rec:
        # 告诉录制器"我是谁"，用于过滤广播里与我无关的关键词误报。
        # 守护进程要长跑，这里不能因为取标识失败就掀翻整个连接。
        try:
            rec.set_identity(ctx.get("uid"), ctx.get("openid"), getattr(qq, "uin", None))
        except Exception as exc:
            log.debug("设置录制标识失败(忽略): %s", exc)

    if spec.get("http_warmup", True):
        _http_warmup(qq, ctx)

    interval = float(conf.get("心跳间隔秒") or protocol.heartbeat_interval(spec))
    log.info("连接游戏服务器 %s:%d …", host, port)
    try:
        sock = _connect(host, port)
    except OSError as exc:
        return f"连接失败: {exc}"
    if rec:
        rec.on_connect()   # 开始登录静默窗口：这段时间的新 opcode 只学不报
        # 必须在发出第一个字节之前包上：这样客户端上行的包也会被录制，
        # 而不是像以前那样只记服务器下行。返回值当普通 socket 用即可。
        sock = rec.wrap(sock, host=host, port=port,
                        uid=ctx.get("uid"), sid=ctx.get("sid"))
        # 密钥三要素 uid/sid/level/firstLogin 都在 FlashVars 里，connect 时就齐了。
        # 必须赶在第一条非豁免消息到达之前开，RC4 密钥流从那一条开始累积。
        rec.enable_crypto(ctx)

        # 超级强攻自动拒绝：在当前会话的 sock 和 RC4 上下文里构造回调
        if rec.auto_reject:
            _sock_ref = sock     # 闭包捕获当前会话的 socket
            def _on_super_storm(data):
                rc4 = rec.rc4_c2s
                if rc4 is None:
                    log.warning("自动拒绝超级强攻失败：RC4 C→S 实例不可用"
                                "（实时解密未启用或密钥自检失败）")
                    return
                ok = sender.send_reject_super_storm(_sock_ref, rc4, data)
                if ok:
                    notify.send(config, "🛡️ 坦克风暴：已自动拒绝超级强攻",
                                f"进攻方：{data.get('atkName', '?')}（{data.get('atkUid', '?')}）\n"
                                f"防守方：{data.get('deftName', '?')}（{data.get('deftUid', '?')}）\n\n"
                                f"已自动发送 RceSuperStormOpt type=2 拒绝包。\n"
                                f"如果服务端要求验证码才接受拒绝，此包可能被忽略，"
                                f"请立刻打开游戏确认。")
            rec.on_super_storm = _on_super_storm
            log.info("超级强攻自动拒绝已就绪")

    try:
        steps = protocol.build_login_sequence(spec, ctx)
        for i, (data, delay) in enumerate(steps, 1):
            sock.sendall(data)
            log.debug("登录步骤 %d/%d 已发送（%d 字节）", i, len(steps), len(data))
            if delay:
                time.sleep(delay)
        log.info("已完成登录握手（%d 步），uid=%s secret=%s",
                 len(steps), ctx.get("uid"), ctx.get("secret"))
        if rec:
            # sid 是加密载荷分析时的头号候选密钥材料，记进会话元信息
            rec.note("login_ok", sid=ctx.get("sid"), uid=ctx.get("uid"))

        # 每日任务：登录完成后跑一次。失败不影响保活主循环——保活是主业务，
        # 领奖失败大不了明天再领，不能因此把连接掀翻。
        if config.get("每日任务", {}).get("启用", False):
            try:
                res, det = daily.run(rec, sock, config)
                _push_daily_summary(config, res, det)
            except Exception as exc:
                log.error("每日任务执行异常（不影响保活）: %s", exc)

        hb = protocol.build_heartbeat(spec, ctx)
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
                    log.info("心跳運行中（第 %d 次，每 %.0fs）", beats, interval)
                else:
                    log.debug("心跳 #%d", beats)
            # 读服务器数据（非阻塞式：超时就继续发心跳）
            try:
                data = sock.recv(8192)
            except socket.timeout:
                continue
            if not data:
                return f"服务器关闭连接（已发 {beats} 次心跳）"
            # 注意：不再在这里调 rec.feed(data)。
            # sock 已被 rec.wrap() 包过，收发字节会自动旁路进录制器；
            # 这里再喂一次会导致下行消息被记录两遍、分帧缓冲错乱。
            reply = protocol.maybe_online_reply(spec, data, ctx)
            if reply:
                sock.sendall(reply)
                log.info("收到在线探测，已回应")
    except OSError as exc:
        return f"连接中断: {exc}"
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _push_daily_summary(config: dict, results: dict, details: dict) -> None:
    """把每日任务成果推到 PushPlus，并在日志里打一份对照表。

    只有真正执行过的才推 —— 全是"未开启/冷却中"的轮次不值得打扰。
    """
    acted = {k: v for k, v in results.items()
             if v and not any(s in v for s in ("未开启", "冷却中", "未实测", "干跑"))}
    if not acted:
        return

    okn = sum(1 for v in acted.values() if v.startswith("成功"))
    bad = len(acted) - okn

    log.info("―― 每日任务成果 ―― 成功 %d，未成 %d", okn, bad)
    rows = []
    for k, v in acted.items():
        icon = "✅" if v.startswith("成功") else "❌"
        log.info("  %s %-12s %s", icon, k, v)
        extra = ""
        d = details.get(k)
        if isinstance(d, dict):
            kv = [f"{a}={d[a]}" for a in daily.REWARD_HINT if a in d]
            if kv:
                extra = f"<br><span style='color:#888'>{' '.join(kv)}</span>"
        rows.append(f"<tr><td>{icon}</td><td><b>{k}</b></td>"
                    f"<td>{v}{extra}</td></tr>")

    title = f"坦克风暴每日任务：成功 {okn}" + (f"，未成 {bad}" if bad else "")
    html = ("<p>本轮共执行 %d 项</p><table border='1' cellpadding='6' "
            "style='border-collapse:collapse;font-size:14px'>%s</table>"
            % (len(acted), "".join(rows)))
    notify.send(config, title, html, template="html")


def run_daily_once(qq, config: dict) -> int:
    """连一次游戏、跑一轮每日任务、断开退出。供 `main.py --daily` 测试用。

    与 --keepalive 的区别：不常驻、不发心跳循环，任务跑完就走。
    登录、建 RC4、实时解密这些前置步骤完全一致，所以测出来的行为可信。
    """
    try:
        spec = protocol.load_spec()
    except protocol.ProtocolNotConfigured as exc:
        log.error("%s", exc)
        return 2

    if not qq.is_valid() and not relogin_with_push(qq, config):
        return 1

    ctx = get_game_context(qq)
    host = ctx.get("server") or spec.get("default_host", "tankstorm-proxy.sincetimes.com")
    port = int(ctx.get("port") or spec.get("default_port", 8001))
    if not ctx.get("openkey"):
        log.error("未取得 openkey，登录态可能失效")
        return 1

    rec = Recorder(config, on_alert=None)
    try:
        sock = _connect(host, port)
    except OSError as exc:
        log.error("连接失败: %s", exc)
        return 1

    try:
        rec.on_connect()
        sock = rec.wrap(sock, host=host, port=port,
                        uid=ctx.get("uid"), sid=ctx.get("sid"))
        rec.enable_crypto(ctx)
        for data, delay in protocol.build_login_sequence(spec, ctx):
            sock.sendall(data)
            if delay:
                time.sleep(delay)
        log.info("已登录，uid=%s sid=%s", ctx.get("uid"), ctx.get("sid"))

        # 先收一会儿，让服务器把登录后的状态推完 —— guard 要靠这些判断免费次数
        sock.settimeout(1.0)
        deadline = time.time() + 6
        while time.time() < deadline:
            try:
                if not sock.recv(8192):
                    break
            except socket.timeout:
                continue
        log.info("登录态数据接收完毕，开始执行任务")

        results, details = daily.run(rec, sock, config)
        _push_daily_summary(config, results, details)
        failed = sum(1 for v in results.values()
                     if "失败" in v or "拦截" in v)
        return 1 if failed else 0
    except OSError as exc:
        log.error("连接中断: %s", exc)
        return 1
    finally:
        try:
            sock.close()
        except OSError:
            pass
        rec.close()


def relogin_with_push(qq, config: dict) -> bool:
    """需要重新扫码时：生成二维码并通过 PushPlus 推送给用户，等待扫码。
    二维码过期/超时则自动重发新码，一直重试直到扫码成功（守护进程不能自己退场）。"""
    # 先试静默续期：skey 只活约 24 小时，但 superkey/RK/ptcz 是长效的，
    # 能换发新 skey 而不必惊动你。成功就不用你动手了。
    if qq.silent_renew():
        log.info("已用长效凭据静默续期，无需人工介入")
        return True

    # 推送登录：直接往手机QQ推确认，免去扫码。
    # 这解决了"二维码图存本地、同一台手机相册扫码"被腾讯拒（限制本地扫码登录）的问题。
    push_uin = (config.get("登录", {}) or {}).get("推送登录QQ号") or qq.uin or None

    def on_qr(path, pushed=False):
        if pushed:
            notify.send_qrcode(
                config, "坦克风暴：请在手机QQ点「确认登录」", path,
                note=f"已向 QQ {push_uin} 推送登录确认，<b>打开手机QQ点确认即可，"
                     f"不用扫码</b>。<br>若没收到推送，可用<b>另一台设备</b>打开本条消息，"
                     f"再用手机QQ扫下面的码（同一台手机存图后扫会被拒）。")
        else:
            notify.send_qrcode(
                config, "坦克风暴：需要扫码登录", path,
                note="请用<b>另一台设备</b>打开本条消息，再用手机QQ扫码。"
                     "<br>把图存到手机再用同一台手机相册扫，腾讯会提示"
                     "「限制本地扫码登录」。")

    attempt = 0
    while True:
        attempt += 1
        # 注意：这里只说"正在尝试"，别在请求发出前就宣称已推送 —— 之前那样写，
        # 推送其实失败了日志却显示"已推送"，很误导。
        log.info("登录态失效，正在%s（第 %d 次尝试）",
                 f"向 QQ {push_uin} 发起推送登录" if push_uin else "生成二维码", attempt)
        if qq.qr_login(on_qr=on_qr, push_uin=push_uin):
            notify.send(config, "坦克风暴：已重新登录", "登录成功，保活已恢复在线。")
            return True
        log.warning("本轮登录未完成（超时/过期），15 秒后重试", )
        time.sleep(15)


def run(qq, config: dict) -> int:
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
                    f"若是「超级强攻」验证码，你只有约 5 分钟处理时间，"
                    f"请马上打开游戏输入验证码。")
    # on_super_storm 回调在 _one_session 里根据当前 sock 动态设置
    rec = Recorder(config, on_alert=on_alert)

    # 启动时若未登录（如服务器首次部署），也走"推送二维码"流程
    if not qq.is_valid():
        relogin_with_push(qq, config)

    log.info("保持活跃启动（Ctrl+C 停止）")
    try:
        while True:
            if run_hours and (time.time() - started) >= run_hours * 3600:
                log.info("达到设定运行时长，退出")
                break
            if not qq.is_valid():
                relogin_with_push(qq, config)

            reason = _one_session(qq, spec, conf, config, rec)
            log.warning("本次连接结束：%s", reason)
            # 断开后退避重连
            wait = min(backoff, max_backoff)
            log.info("%.0f 秒后重连…", wait)
            time.sleep(wait)
            backoff = min(backoff * 2, max_backoff)
            # 若刚成功跑过一段，重置退避
    except KeyboardInterrupt:
        log.info("手动停止保持活跃")
    finally:
        rec.close()
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
            if rec.stream.session_dir:
                log.info("解密：python tools/redwar_rc4.py %s/s2c.bin --uid %s --write",
                         rec.stream.session_dir, ctx.get("uid") or "<uid>")
    return 0

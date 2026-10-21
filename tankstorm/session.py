# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""连上游戏服务器并登录，以及登录态失效时的重新登录。

保活（socket_keepalive）和一次性任务（jobs：--daily / --country-war）共用这一套：

    ctx = get_game_context(qq)              每次连接前刷新，openkey 会过期
    sock = open_link(spec, ctx, rec)        连 TCP → 接录制器 → 开实时解密 → 发登录序列
    drain(sock)                             收完服务端登录后推来的状态
    check_protocol(rec, ctx, config)        核对协议编号对照，没通过就一个请求都不发

登录后的状态推送不能省：闸门要靠它判断免费次数，公会捐献的玩家名、
国战和争霸战的国家 ID 也都在里面。
"""

import socket
import time

from . import notify, protocol, wire, wiremap
from .log import get_logger
from .qzone import get_game_context
from .recorder import Recorder

log = get_logger()

DEFAULT_HOST = "tankstorm-proxy.sincetimes.com"
DEFAULT_PORT = 8001


def endpoint(spec: dict, ctx: dict):
    """游戏服务器地址：FlashVars 给了就用它，没给用 protocol.json 里的默认值。"""
    host = ctx.get("server") or spec.get("default_host", DEFAULT_HOST)
    port = int(ctx.get("port") or spec.get("default_port", DEFAULT_PORT))
    return host, port


def heartbeat_interval(spec: dict, config: dict) -> float:
    """心跳间隔：config 里填了非 0 就用它，否则用抓包得到的周期。"""
    conf = config.get("保持活跃", {}) or {}
    return float(conf.get("心跳间隔秒") or protocol.heartbeat_interval(spec))


def make_beater(sock, spec: dict, ctx: dict, config: dict) -> wire.Beater:
    """造一个任务执行期间用的心跳器（见 wire.Beater）。"""
    return wire.Beater(sock, protocol.build_heartbeat(spec, ctx),
                       heartbeat_interval(spec, config))


def http_warmup(qq, ctx: dict) -> None:
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


def close(sock) -> None:
    try:
        sock.close()
    except OSError:
        pass


def open_link(spec: dict, ctx: dict, rec) -> socket.socket:
    """连 TCP、接上录制器、开实时解密、发登录序列。返回已登录的 socket。

    顺序不能换：
      · rec.wrap() 要在发出第一个字节之前包上，客户端上行的包才会被录制
      · rec.enable_crypto() 要赶在第一条非豁免消息到达之前，
        RC4 密钥流从那一条开始累积，晚一步就永远对不上
    密钥三要素 uid/sid/level/firstLogin 都在 FlashVars 里，连接前就齐了。

    连接或登录失败抛 OSError，socket 已关闭。这个游戏版本先前核对没通过时抛
    wiremap.UnsupportedVersion（也是 OSError），这时还没连 TCP。
    """
    wiremap.activate(ctx)       # 先选好协议编号对照；先前核对没通过的版本不连
    host, port = endpoint(spec, ctx)
    log.info("连接游戏服务器 %s:%d …", host, port)
    sock = socket.create_connection((host, port), timeout=15)
    sock.settimeout(15)
    try:
        rec.on_connect()   # 开始登录静默窗口：这段时间的新 opcode 只学不报
        sock = rec.wrap(sock, host=host, port=port, uid=ctx.get("uid"),
                        sid=ctx.get("sid"), version=ctx.get("version"))
        rec.enable_crypto(ctx)
        steps = protocol.build_login_sequence(spec, ctx)
        for i, (data, delay) in enumerate(steps, 1):
            sock.sendall(data)
            log.debug("登录步骤 %d/%d 已发送（%d 字节）", i, len(steps), len(data))
            if delay:
                time.sleep(delay)
    except BaseException:
        close(sock)
        raise
    log.info("已登录（%d 步握手），uid=%s sid=%s",
             len(steps), ctx.get("uid"), ctx.get("sid"))
    # sid 是加密载荷分析时的头号候选密钥材料，记进会话元信息
    rec.note("login_ok", sid=ctx.get("sid"), uid=ctx.get("uid"))
    return sock


def check_protocol(rec, ctx: dict, config: dict):
    """收完登录推送后核对协议编号对照（见 wiremap.verify）。通过返回 None，否则返回原因。

    没通过时把这个游戏版本记为本进程不可用，并推送提醒：协议多半又被打乱了，
    这时一个任务请求都不能发。
    """
    ok, why = wiremap.verify(rec)
    if ok:
        log.info("协议编号核对通过：%s", why)
        return None
    version = ctx.get("version") or "（未知）"
    wiremap.reject(ctx.get("version") or "", why)
    log.error("协议编号核对没通过（游戏版本 %s）：%s。已断开，不发任何请求", version, why)
    notify.send(config, "坦克风暴：游戏协议可能又改了，脚本已停手",
                f"游戏版本 {version} 登录后核对没通过：{why}\n\n"
                f"脚本没有发任何任务请求。需要按 README「游戏更新后」重新生成编号对照表。")
    return why


def drain(sock, seconds: float = 6.0) -> None:
    """先收一会儿，让服务器把登录后的状态推完。连接断开就提前返回。"""
    sock.settimeout(1.0)
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if not sock.recv(8192):
                break
        except socket.timeout:
            continue
    log.info("登录态数据接收完毕，开始执行")


# ---------------------------------------------------------------- 一次性连接

def run_once(qq, config: dict, work) -> int:
    """连一次游戏、登录、把活交给 work，然后断开退出。

    `--daily` 和 `--country-war` 共用这一套。
    work(rec, sock, spec, ctx) 返回进程退出码。
    """
    try:
        spec = protocol.load_spec()
    except protocol.ProtocolNotConfigured as exc:
        log.error("%s", exc)
        return 2

    if not qq.is_valid() and not relogin_with_push(qq, config):
        return 1

    ctx = get_game_context(qq)
    if not ctx.get("openkey"):
        log.error("未取得 openkey，登录态可能失效")
        return 1

    rec = Recorder(config, on_alert=None)
    try:
        try:
            sock = open_link(spec, ctx, rec)
        except OSError as exc:
            log.error("连接失败: %s", exc)
            return 1
        try:
            drain(sock)
            if check_protocol(rec, ctx, config):
                return 1
            return work(rec, sock, spec, ctx)
        except OSError as exc:
            log.error("连接中断: %s", exc)
            return 1
        finally:
            close(sock)
    finally:
        rec.close()


# ---------------------------------------------------------------- 重新登录

def push_uin(qq, config: dict):
    """推送登录用的 QQ 号：config 里填了就用它，否则用 cookies.json 里的 uin。"""
    return (config.get("登录", {}) or {}).get("推送登录QQ号") or qq.uin or None


def qr_notifier(config: dict, uin):
    """二维码生成后的回调：把二维码推到 PushPlus，文案跟实际走的路径一致。"""
    def on_qr(path, pushed=False):
        if pushed:
            notify.send_qrcode(
                config, "坦克风暴：请在手机QQ点「确认登录」", path,
                note=f"已向 QQ {uin} 推送登录确认，<b>打开手机QQ点确认即可，"
                     f"不用扫码</b>。<br>若没收到推送，可用<b>另一台设备</b>打开本条消息，"
                     f"再用手机QQ扫下面的码（同一台手机存图后扫会被拒）。")
        else:
            notify.send_qrcode(
                config, "坦克风暴：需要扫码登录", path,
                note="请用<b>另一台设备</b>打开本条消息，再用手机QQ扫码。"
                     "<br>把图存到手机再用同一台手机相册扫，腾讯会提示"
                     "「限制本地扫码登录」。")
    return on_qr


def relogin_with_push(qq, config: dict) -> bool:
    """需要重新扫码时：生成二维码并通过 PushPlus 推送给用户，等待扫码。

    二维码过期/超时则自动重发新码，一直重试直到扫码成功（守护进程不能自己退场）。
    """
    # 先试静默续期：成功就不用人动手了
    if qq.silent_renew():
        log.info("已用长效凭据静默续期，无需人工介入")
        return True

    uin = push_uin(qq, config)
    on_qr = qr_notifier(config, uin)
    attempt = 0
    while True:
        attempt += 1
        # 这里只说"正在尝试"，别在请求发出前就宣称已推送 —— 之前那样写，
        # 推送其实失败了日志却显示"已推送"，很误导。
        log.info("登录态失效，正在%s（第 %d 次尝试）",
                 f"向 QQ {uin} 发起推送登录" if uin else "生成二维码", attempt)
        if qq.qr_login(on_qr=on_qr, push_uin=uin):
            notify.send(config, "坦克风暴：已重新登录", "登录成功，保活已恢复在线。")
            return True
        log.warning("本轮登录未完成（超时/过期），15 秒后重试")
        time.sleep(15)

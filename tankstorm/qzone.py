# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""登录后，还原游戏客户端启动时拿到的全部参数（socket 登录鉴权要用）。

坦克风暴的真实加载链路（已由抓包确认）：

  game.qzone.qq.com/100616028   ← 带 QQ 登录 cookie 请求，Qzone 当场签发 openid/openkey
        │  HTML 里： <iframe data-src="https://tankstorm-qzone.sincetimes.com/?openid=..&openkey=..&pf=qzone&pfkey=..">
        ▼
  tankstorm-qzone.sincetimes.com/?openid=..&openkey=..   ← 游戏外框页
        │  HTML 里： <param name="FlashVars" value="openid=..&openkey=..&uid=..&secret=..&server=tankstorm-proxy.sincetimes.com&region=18&port=8001&sid=..&pfkey=..&version=..&level=..">
        ▼
  RedWar.swf 用这些参数连 socket：tankstorm-proxy.sincetimes.com:8001

因此本模块两步走：抓 iframe URL → 抓 FlashVars，产出 socket 登录所需的完整 ctx。
openid/openkey 每次都是新鲜签发的，所以保活守护进程可反复调用本函数拿最新票据。
"""

import os
import re
from urllib.parse import parse_qsl, urljoin, urlparse

from . import GAME_URL, paths
from .log import get_logger

log = get_logger()

DEBUG_DIR = paths.user_path("debug")              # 抓下来的页面，属可写数据

CANVAS_HOST = "tankstorm-qzone.sincetimes.com"

# 外层页里的游戏 iframe 地址
_IFRAME_RE = re.compile(
    r'(?:data-src|src)=["\'](https?://%s/\?[^"\']+)["\']' % re.escape(CANVAS_HOST), re.I)
_CANVAS_URL_JS_RE = re.compile(
    r'canvas_url["\']?\s*[:=]\s*["\'](https?://%s[^"\']*)' % re.escape(CANVAS_HOST), re.I)
# iframe 页里传给 SWF 的 FlashVars
_FLASHVARS_RE = re.compile(
    r'(?:FlashVars["\']?\s*(?:value=)?["\']|flashvars\s*=\s*["\'])([^"\']+)', re.I)
# 游戏配置 flash/config_<version>.xml 里主程序 SWF 那一行
_GAME_SWF_RE = re.compile(r'<asset\s+name="game"\s+value="[^"]*RedWar_(\d+)\.swf"')

# socket 登录关心的字段（缺失不报错，按需使用）
WANTED = ("openid", "openkey", "uid", "secret", "sid", "region", "pf", "pfkey",
          "server", "port", "port1", "version", "level", "coinserver",
          "storageURL", "firstLogin", "lang")


def _dump(name: str, text: str) -> None:
    os.makedirs(DEBUG_DIR, exist_ok=True)
    with open(os.path.join(DEBUG_DIR, name), "w", encoding="utf-8", errors="replace") as f:
        f.write(text)


def parse_iframe_url(html: str) -> str | None:
    """从外层游戏页 HTML 里取出 tankstorm-qzone iframe 的完整 URL。"""
    m = _IFRAME_RE.search(html)
    if m:
        return m.group(1).replace("&amp;", "&")
    return None


def parse_flashvars(html: str) -> dict:
    """从 iframe 页 HTML 里解析 FlashVars（key=value&...）为 dict。"""
    m = _FLASHVARS_RE.search(html)
    if not m:
        return {}
    fv = m.group(1).replace("&amp;", "&")
    return dict(parse_qsl(fv, keep_blank_values=True))


def extract_context(outer_html: str, iframe_html: str) -> dict:
    """纯解析（不发请求），供离线测试。合并 iframe URL 参数 + FlashVars。"""
    ctx = {}
    iframe_url = parse_iframe_url(outer_html)
    if iframe_url:
        ctx["canvas_url"] = iframe_url
        ctx.update(dict(parse_qsl(urlparse(iframe_url).query, keep_blank_values=True)))
    ctx.update(parse_flashvars(iframe_html))   # FlashVars 更全，优先
    return ctx


def parse_swf_version(config_xml: str) -> str | None:
    """从游戏配置 XML 里取主程序 RedWar_<版本>.swf 的版本号。"""
    m = _GAME_SWF_RE.search(config_xml)
    return m.group(1) if m else None


def fetch_swf_version(s, ctx: dict) -> str | None:
    """查这次加载的主程序版本，查不到返回 None。

    FlashVars 的 version 是配置文件 config_<version>.xml 的版本，只改数据表时它也会涨，
    主程序不一定换：配置 2026103002 加载的是 RedWar_2026103001.swf。协议编号跟着
    主程序走，编号对照表要按主程序的版本选。
    """
    base, version = ctx.get("storageURL"), ctx.get("version")
    if not base or not version:
        return None
    try:
        r = s.get(f"{base.rstrip('/')}/flash/config_{version}.xml", timeout=20)
        r.raise_for_status()
        return parse_swf_version(r.text)
    except Exception as exc:
        log.warning("读取游戏配置失败: %s", exc)
        return None


def get_game_context(qq) -> dict:
    """在线版：真正发请求，返回 socket 登录所需的完整 ctx。qq 为已登录 QQSession。"""
    s = qq.session
    ctx = {"uin": qq.uin, "g_tk": str(qq.g_tk), "skey": s.cookies.get("skey") or ""}

    log.info("打开游戏页，获取 openid/openkey…")
    r = s.get(GAME_URL, timeout=20)
    _dump("1_outer.html", r.text)
    iframe_url = parse_iframe_url(r.text)
    if not iframe_url:
        m = _CANVAS_URL_JS_RE.search(r.text)
        log.warning("未从外层页解析到游戏 iframe 地址（HTML 已存 debug/1_outer.html）")
        if not m:
            return ctx
        iframe_url = m.group(1)
    iframe_url = urljoin(GAME_URL, iframe_url)
    ctx["canvas_url"] = iframe_url
    ctx.update(dict(parse_qsl(urlparse(iframe_url).query, keep_blank_values=True)))
    log.info("游戏外框: %s", iframe_url.split("?")[0])

    try:
        r2 = s.get(iframe_url, headers={"Referer": GAME_URL}, timeout=20)
        _dump("2_iframe.html", r2.text)
        fv = parse_flashvars(r2.text)
        ctx.update(fv)
        if fv:
            log.info("已解析 FlashVars：server=%s port=%s uid=%s sid=%s region=%s",
                     fv.get("server"), fv.get("port"), fv.get("uid"),
                     fv.get("sid"), fv.get("region"))
        else:
            log.warning("未在 iframe 页找到 FlashVars（HTML 已存 debug/2_iframe.html）")
    except Exception as exc:
        log.warning("请求 iframe 页失败: %s", exc)

    swf = fetch_swf_version(s, ctx)
    if swf:
        ctx["swf_version"] = swf
    log.info("游戏配置版本 %s，主程序版本 %s", ctx.get("version"), swf or "读不到，退回配置版本")

    have = [k for k in ("openid", "openkey", "uid", "secret", "sid", "server", "port")
            if ctx.get(k)]
    log.info("socket 登录参数就绪: %s", ", ".join(have) if have else "（不足，见 debug/）")
    return ctx

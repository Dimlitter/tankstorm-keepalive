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

from . import APPID, GAME_URL
from .log import get_logger

log = get_logger()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEBUG_DIR = os.path.join(BASE_DIR, "debug")

CANVAS_HOST = "tankstorm-qzone.sincetimes.com"

# 外层页里的游戏 iframe 地址
_IFRAME_RE = re.compile(
    r'(?:data-src|src)=["\'](https?://%s/\?[^"\']+)["\']' % re.escape(CANVAS_HOST), re.I)
_CANVAS_URL_JS_RE = re.compile(
    r'canvas_url["\']?\s*[:=]\s*["\'](https?://%s[^"\']*)' % re.escape(CANVAS_HOST), re.I)
# iframe 页里传给 SWF 的 FlashVars
_FLASHVARS_RE = re.compile(
    r'(?:FlashVars["\']?\s*(?:value=)?["\']|flashvars\s*=\s*["\'])([^"\']+)', re.I)

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

    have = [k for k in ("openid", "openkey", "uid", "secret", "sid", "server", "port")
            if ctx.get(k)]
    log.info("socket 登录参数就绪: %s", ", ".join(have) if have else "（不足，见 debug/）")
    return ctx

"""QQ 扫码登录（ptlogin2 协议），获取空间游戏所需 cookie（uin/skey/p_skey）。

流程：
  1. 请求 ptqrshow 拿二维码图片，同时得到 cookie qrsig；
  2. 手机 QQ 扫码；脚本轮询 ptqrlogin，携带 hash33(qrsig) 计算的 ptqrtoken；
  3. 扫码确认后返回 check_sig 跳转地址，GET 一次即种下全套登录 cookie；
  4. cookie 序列化到 cookies.json，下次运行直接复用；失效后需要重新扫码。

不涉及账号密码 —— 只用扫码，服务器上把 qrcode.png 取下来扫或直接看终端字符画。

推送登录（页面上点头像 → 手机收到确认）—— 2026-08-13 抓浏览器逆出来的
--------------------------------------------------------------------
它不是"另一种取二维码"，而是**挂在已有二维码会话上的一个动作**。顺序必须是：

    xlogin                      建立 pt_login_sig
    ptqrshow（普通）            拿到 PNG 和 qrsig
    pt_fetch_dev_uin            换取 dev_mid_sig（设备签名）
    ptqrshow?qr_push=1&type=1   把这个会话推给指定 QQ（不带 e/l/s/d/v 图片参数）
    ptqrlogin 轮询              和扫码共用一条轮询，带 has_onekey=1

三个错误码的含义（试出来的）：

    ec=313 提交参数错误  —— 没有 dev_mid_sig，服务端认不出这台设备
    ec=315 页面过期      —— 有 dev_mid_sig 但已失效，得重新 pt_fetch_dev_uin
    ec=0                —— 推送已发出，手机上点确认即可

设备记录挂在哪：不是 pt_guid_sig（2026-08-13 实测）
--------------------------------------------------
对着 pt_fetch_dev_uin 试出来的：

    什么设备 cookie 都不带                          -> errcode 22027，不补发
    只带 pt_guid_sig（哪怕浏览器里正在用的那个）    -> errcode 22027，不补发
    带 dev_mid_sig（+pt_guid_sig+pt_recent_uins）   -> errcode 22028，但 data:[] 且**不补发**
    浏览器自己发的同一个接口                        -> errcode 22028，data:[<uin>]，**补发**

也就是说 pt_guid_sig 不是设备身份，dev_mid_sig 才是；而这个接口对我们
只认不发。浏览器比我们多带 pt-ev-token / dlock / it_c / eas_sid 等设备安全
cookie，差别很可能在那里，但**尚未证实**。

第一个 dev_mid_sig 从哪来仍然未知 —— 两份登录页抓包都只拍到"把早就存在的那份
续下去"，没拍到它被创建。所以现在只能从浏览器搬一次（`--import-device`）。
注意搬来的签名有时效：拿一小时前抓包里的那份去推送，服务端回 ec=315。

pt_guid_sig 与它配对，由 xlogin 或登录成功时的 ptqrlogin 下发。注意
**xlogin 会无条件重新签发一个**，所以做 pt_fetch_dev_uin 之前要把原来那个存住。

另外两条路确实走不通：

- **静默续期**（pt_login）：该端点现在返回一张腾讯网首页 HTML，已下线。
- **网页「快捷登录」里的本地通道**：反复 CONNECT
  `localhost.ptlogin2.qq.com:430X`，是在跟本机 QQ 桌面客户端要票据，
  且有证书绑定。但实测**推送并不依赖它** —— 把那几个本地令牌全删掉，
  推送照常工作。
"""

import json
import os
import random
import re
import time

import requests

from . import GAME_URL
from . import paths
from .log import get_logger

log = get_logger()

PTLOGIN_APPID = "549000912"  # QQ空间的 ptlogin aid（游戏页 302 时携带的就是它）
DAID = "5"

# 设备/长效凭据：推送登录靠它们识别"推给哪台设备"，重新登录时不能清掉。
#
# 2026-08-13 抓包定位：真正让推送成立的是 **dev_mid_sig**（设备中间签名）。
# 早先这份名单里没有它，于是每次登录前的清理都会把它删掉，
# ptqrshow?qr_push=1 就只能回 ec=313「提交参数错误」。
# 一并保住同批的几个设备态 cookie，它们都是浏览器里跟着设备走的。
DEVICE_COOKIES = {"ptcz", "RK", "superkey", "supertoken", "superuin",
                  "pt2gguin", "pt_recent_uins", "ETK",
                  "dev_mid_sig", "pt_guid_sig", "uikey", "pt-ev-token",
                  "dlock", "it_c", "eas_sid", "pt_local_token",
                  "_qpsvr_localtk"}

# 从浏览器搬设备记录时**只搬这几个**：都是跟着设备走的，不含任何登录凭据。
# 特意**不搬** skey/p_skey/uin（登录态，会和本脚本自己的登录态打架），
# 也不搬 ptcz/RK/superkey/supertoken（长效登录凭据，同理）。
#
# 前三个是设备签名本体，后四个是浏览器请求 pt_fetch_dev_uin 时比我们多带的
# 设备安全类 cookie —— 只带前三个时服务端回 data:[] 且不补发签名，
# 浏览器带全了才回 data:[<uin>] 并补发，所以一并搬过来试。
DEVICE_BOOTSTRAP = ("dev_mid_sig", "pt_guid_sig", "pt_recent_uins",
                    "pt-ev-token", "dlock", "it_c", "eas_sid")
PTLOGIN_DOMAIN = ".ptlogin2.qq.com"

# cookie 和二维码是用户数据，必须落在 exe 旁边而不是打包的临时解压目录 ——
# 落错地方的后果是每次启动都要重新扫码，而且不报错。见 paths.py。
BASE_DIR = paths.app_dir()
COOKIE_FILE = paths.user_path("cookies.json")
QRCODE_FILE = paths.user_path("qrcode.png")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36")


def hash33(s: str) -> int:
    """腾讯 ptqrtoken 算法。"""
    e = 0
    for c in s:
        e += (e << 5) + ord(c)
    return 2147483647 & e


def calc_g_tk(p_skey: str) -> int:
    """腾讯 g_tk / bkn 算法，很多接口用它做 CSRF 校验。"""
    h = 5381
    for c in p_skey:
        h += (h << 5) + ord(c)
    return h & 0x7FFFFFFF


def _print_qr_ascii(png_path: str) -> None:
    """尽力把二维码渲染成终端字符画（依赖 Pillow，失败则静默跳过）。"""
    try:
        from PIL import Image
    except ImportError:
        return
    try:
        img = Image.open(png_path).convert("L")
        w, h = img.size
        px = img.load()
        binary = [[1 if px[x, y] < 128 else 0 for x in range(w)] for y in range(h)]
        ys = [y for y in range(h) if any(binary[y])]
        xs = [x for x in range(w) if any(row[x] for row in binary)]
        if not ys or not xs:
            return
        top, bottom, left, right = ys[0], ys[-1], xs[0], xs[-1]
        # 用左上角定位图形的第一段黑色横向长度 / 7 估算模块大小
        run = 0
        for x in range(left, right + 1):
            if binary[top][x]:
                run += 1
            else:
                break
        module = max(1, run // 7)
        n = (right - left + 1 + module // 2) // module
        lines = []
        # 终端多为深色背景，反色输出（黑模块→空格）扫码成功率更高
        for r in range(n):
            y = top + r * module + module // 2
            if y > bottom:
                break
            row = ""
            for c in range(n):
                x = left + c * module + module // 2
                dark = binary[y][x] if x <= right else 0
                row += "  " if dark else "██"
            lines.append(row)
        print("\n".join(lines))
        print("(若上方二维码扫不出来，请直接打开 qrcode.png 扫码)")
    except Exception as exc:  # 渲染失败不影响主流程
        log.debug("二维码字符画渲染失败: %s", exc)


class QQSession:
    """带 cookie 持久化的 QQ 登录会话。"""

    def __init__(self, cookie_file: str = COOKIE_FILE):
        self.cookie_file = cookie_file
        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA
        self._load_cookies()

    # ---------- cookie 持久化 ----------

    def _load_cookies(self) -> None:
        if not os.path.exists(self.cookie_file):
            return
        try:
            with open(self.cookie_file, encoding="utf-8") as f:
                jar = json.load(f)
            for c in jar:
                self.session.cookies.set(c["name"], c["value"],
                                         domain=c["domain"], path=c["path"],
                                         expires=c.get("expires"))
            log.debug("已从 %s 载入 %d 条 cookie", self.cookie_file, len(jar))
        except Exception as exc:
            log.warning("cookie 文件读取失败，将重新登录: %s", exc)

    def _save_cookies(self) -> None:
        # 必须保存 expires：没有它就无法判断票据何时到期，只能等请求失败才发现，
        # 也就没法在到期前静默续期。长效凭据(superkey/RK/ptcz)的有效期也靠它看。
        jar = [{"name": c.name, "value": c.value, "domain": c.domain,
                "path": c.path, "expires": c.expires}
               for c in self.session.cookies]
        with open(self.cookie_file, "w", encoding="utf-8") as f:
            json.dump(jar, f, ensure_ascii=False, indent=1)
        log.info("cookie 已保存到 %s", self.cookie_file)

    def ticket_status(self) -> dict:
        """返回各票据的剩余寿命，用于判断是否该续期。

        {名字: 剩余秒数或 None(会话cookie/无过期)}
        """
        now = time.time()
        out = {}
        for c in self.session.cookies:
            out[c.name] = None if not c.expires else round(c.expires - now)
        return out

    def expires_within(self, seconds: float) -> bool:
        """核心票据(skey/p_skey)是否将在 seconds 内过期。取不到过期时间时返回 False。"""
        st = self.ticket_status()
        vals = [st.get(n) for n in ("skey", "p_skey") if st.get(n) is not None]
        return bool(vals) and min(vals) <= seconds

    # ---------- 登录态 ----------

    @property
    def uin(self) -> str:
        raw = self.session.cookies.get("uin", domain=".qq.com") or \
            self.session.cookies.get("uin") or ""
        return raw.lstrip("o0")

    def _get_p_skey(self) -> str:
        for c in self.session.cookies:
            if c.name == "p_skey" and "qzone" in (c.domain or ""):
                return c.value
        return self.session.cookies.get("p_skey") or ""

    @property
    def g_tk(self) -> int:
        key = self._get_p_skey() or self.session.cookies.get("skey") or ""
        return calc_g_tk(key)

    def has_long_term_ticket(self) -> bool:
        """是否持有长效登录凭据（腾讯"快速登录/下次自动登录"用的那套）。

        skey/p_skey 约 24 小时就过期，但 superkey/RK/ptcz 是长效的 ——
        浏览器能几周不重新扫码就是靠它们换发新 skey。
        """
        names = {c.name for c in self.session.cookies}
        return bool(names & {"superkey", "RK", "ptcz"})

    def silent_renew(self) -> bool:
        """尝试用长效凭据静默换发新 skey，成功则不必重新扫码。

        走的是 ptlogin2 的快速登录(qlogin)通道：带着 superkey/RK/ptcz 请求
        xlogin 建立 login_sig，再走 pt_login 拿新票据。失败时保持原样返回 False，
        由调用方回退到扫码 —— 绝不能因为续期失败就把现有 cookie 弄坏。
        """
        if not self.has_long_term_ticket():
            log.info("没有长效凭据(superkey/RK/ptcz)，无法静默续期")
            return False

        before = {c.name: c.value for c in self.session.cookies}
        try:
            # 1) xlogin 建立 login_sig —— 快速登录的入口，会带出 pt_login_sig
            r = self.session.get(
                "https://xui.ptlogin2.qq.com/cgi-bin/xlogin",
                params={"proxy_url": "https://qzs.qq.com/qzone/v6/portal/proxy.html",
                        "daid": DAID, "hide_title_bar": "1", "low_login": "0",
                        "qlogin_auto_login": "1", "no_verifyimg": "1",
                        "link_target": "blank", "appid": PTLOGIN_APPID,
                        "style": "22", "target": "self", "s_url": GAME_URL},
                timeout=15)
            login_sig = self._cookie("pt_login_sig") or ""
            if not login_sig:
                log.info("静默续期：未取得 pt_login_sig，放弃")
                return False

            # 2) 快速登录：服务端凭 superkey/RK/ptcz 识别设备，直接下发新票据
            r = self.session.get(
                "https://ptlogin2.qq.com/pt_login",
                params={"u": self.uin, "aid": PTLOGIN_APPID, "daid": DAID,
                        "login_sig": login_sig, "u1": GAME_URL,
                        "ptlang": "2052", "pt_uistyle": "40",
                        "action": f"0-0-{int(time.time() * 1000)}"},
                headers={"Referer": "https://xui.ptlogin2.qq.com/"},
                timeout=15)
            m = re.search(r"ptuiCB\('(\d+)','\d+','([^']*)','\d+','([^']*)'", r.text)
            if m and m.group(1) == "0" and m.group(2):
                self.session.get(m.group(2), allow_redirects=True, timeout=20)
            elif m:
                log.info("静默续期被拒(code=%s): %s", m.group(1), m.group(3)[:60])
                return False
            else:
                # 2026-08-13 实测：这个端点已经不返回 ptuiCB 了，直接给一张
                # 腾讯网首页 HTML。也就是说 pt_login 这条快速登录通道没了。
                # 以前这里没有 else 分支，匹配不上就悄悄掉到最后一句
                # "静默续期未生效"，看不出到底是被拒还是接口变了。
                head = (r.text or "")[:120].replace("\n", " ")
                is_html = "<html" in r.text[:300].lower()
                log.info("静默续期：pt_login 没有返回 ptuiCB（%s）—— "
                         "腾讯这个接口已变更，不是凭据问题。返回开头：%s",
                         "是一张 HTML 页面" if is_html else "格式不认识", head)
                return False
        except requests.RequestException as exc:
            log.info("静默续期请求异常: %s", exc)
            return False

        if self.is_valid():
            self._save_cookies()
            left = self.ticket_status().get("skey")
            log.info("✅ 静默续期成功，无需扫码%s",
                     f"（新 skey 剩余 {left // 3600} 小时）" if left else "")
            return True

        # 没成功就还原，别把原来还能用的 cookie 搞坏
        for name, val in before.items():
            try:
                self.session.cookies.set(name, val)
            except Exception:
                pass
        log.info("静默续期未生效，需要重新扫码")
        return False

    def is_valid(self) -> bool:
        """访问游戏页：已登录返回 200 页面，未登录会 302 去 ptlogin。"""
        if not self.session.cookies.get("skey"):
            return False
        try:
            r = self.session.get(GAME_URL, allow_redirects=False, timeout=15)
        except requests.RequestException as exc:
            log.warning("登录态校验请求失败: %s", exc)
            return False
        if r.status_code == 200 and "ptlogin" not in r.text[:2000]:
            return True
        loc = r.headers.get("Location", "")
        log.info("登录态已失效 (status=%s, location=%s...)", r.status_code, loc[:80])
        return False

    # ---------- 扫码 / 推送登录 ----------

    def _clear_session_cookies(self, keep=()) -> None:
        """清掉会话票据，但保留 keep 里的设备/长效凭据。

        推送登录依赖 ptcz/RK/superkey 识别设备，全清就推不出去。
        """
        keep = set(keep)
        doomed = [(c.name, c.domain, c.path)
                  for c in self.session.cookies if c.name not in keep]
        for name, domain, path in doomed:
            try:
                self.session.cookies.clear(domain, path, name)
            except KeyError:
                pass

    def _cookie(self, name):
        """安全地取 cookie 值。

        同名 cookie 可能同时存在于 .qq.com 和 ptlogin2.qq.com 两个域上，
        requests 的 cookies.get() 遇到重名会直接抛 CookieConflictError。
        这里取最后一个（域更具体的通常是服务端刚下发的那个）。
        """
        vals = [c.value for c in self.session.cookies if c.name == name]
        return vals[-1] if vals else None

    def _set_cookie(self, name, value, domain=PTLOGIN_DOMAIN):
        """把某个 cookie 设成指定值，先清掉所有同名的。

        不先清就可能留下两个同名 cookie（不同域），之后 _cookie() 取到哪个
        全看顺序。
        """
        for c in [c for c in self.session.cookies if c.name == name]:
            try:
                self.session.cookies.clear(c.domain, c.path, c.name)
            except KeyError:
                pass
        self.session.cookies.set(name, value, domain=domain, path="/")

    def import_device_cookies(self, text: str) -> list:
        """从浏览器搬一份设备记录过来 —— 推送登录的一次性引导。

        `pt_fetch_dev_uin` 只能给已有的 dev_mid_sig 续期，签发不出第一个，
        所以这一份得从一个登录过 QQ 网页的浏览器里取。取法：在
        ptlogin2.qq.com 的页面上打开开发者工具，把 Cookie 复制出来。

        三种写法都认：
          - 开发者工具里复制的 Cookie 头：``name=value; name=value``
          - ``{"name": "value", ...}`` 的 JSON
          - ``[{"name": ..., "value": ...}, ...]`` 的 JSON（各类导出插件的格式）

        只取 DEVICE_BOOTSTRAP 里那几个，其余一律忽略。返回搬进来的名字列表。
        """
        text = (text or "").strip()
        pairs = {}
        if text.startswith(("{", "[")):
            data = json.loads(text)
            if isinstance(data, dict):
                pairs = {k: v for k, v in data.items() if isinstance(v, str)}
            else:
                pairs = {c["name"]: c["value"] for c in data
                         if isinstance(c, dict) and "name" in c}
        else:
            text = re.sub(r"^\s*Cookie\s*:\s*", "", text, flags=re.I)
            for kv in text.split(";"):
                k, sep, v = kv.strip().partition("=")
                if sep and k:
                    pairs[k.strip()] = v.strip()

        got = []
        for name in DEVICE_BOOTSTRAP:
            if pairs.get(name):
                self._set_cookie(name, pairs[name])
                got.append(name)
        if got:
            self._save_cookies()
        return got

    def device_status(self) -> str:
        """一句话说明设备记录当前处于什么状态，给 --check 和报错文案用。"""
        if self._cookie("dev_mid_sig"):
            return ("有设备签名 dev_mid_sig，可以试推送。"
                    "（签名有时效，过期时推送回 ec=315，得重搬一份新的）")
        if self._cookie("pt_guid_sig"):
            return ("没有设备记录：只有 pt_guid_sig，而 pt_fetch_dev_uin 只能给"
                    "已有的 dev_mid_sig 续期，签发不出第一个")
        return "没有任何设备凭据"

    def _push_login(self, push_uin):
        """把**已建立的二维码会话**推送到手机 QQ。返回 (是否成功, 说明)。

        关键在顺序（2026-08-13 抓浏览器抓出来的）：推送不是"另一种取二维码"，
        而是挂在已有 qrsig 会话上的一个动作 —— 页面上先加载二维码，
        点头像才发这一条。所以必须**先走一次普通 ptqrshow 拿到 qrsig**，
        再发这条；直接上来就发，服务端回 ec=313「提交参数错误」。

        另外它不带 e/l/s/d/v 那几个图片参数（本来就不是要图），但要带 u1。
        """
        if not self._cookie("qrsig"):
            return False, "还没有二维码会话（qrsig），推送无从挂载"

        # 给设备签名续期。注意这个接口**只能续期，不能签发第一个**：
        # 实测只带 pt_guid_sig（哪怕是浏览器里正在用的那个）一律 errcode 22027；
        # 带上 dev_mid_sig 才回 22028 并下发新的。所以没有 dev_mid_sig 时
        # 这一步是白跑，直接跳过，把话说清楚让用户去引导一次。
        guid_sig = self._cookie("pt_guid_sig")
        if guid_sig and self._cookie("dev_mid_sig"):
            try:
                # pt_guid_token = hash33(pt_guid_sig)，和 ptqrtoken = hash33(qrsig)
                # 是同一个套路（拿抓包里的一对逐位验证过）。
                r = self.session.get(
                    "https://ssl.ptlogin2.qq.com/pt_fetch_dev_uin",
                    params={"r": str(random.random()),
                            "pt_guid_token": str(hash33(guid_sig))},
                    headers={"Referer": "https://xui.ptlogin2.qq.com/"},
                    timeout=15)
                log.debug("pt_fetch_dev_uin: %s", (r.text or "")[:120])
            except requests.RequestException as exc:
                log.debug("pt_fetch_dev_uin 失败(忽略): %s", exc)
        if not self._cookie("dev_mid_sig"):
            return False, ("没有设备记录 dev_mid_sig，服务端认不出这台设备。"
                           "pt_fetch_dev_uin 只能给已有的续期、签发不出第一个，"
                           "所以要先从一个登录过 QQ 网页的浏览器搬一次："
                           "main.py --import-device <文件>（详见 README）")
        try:
            r = self.session.get(
                "https://ssl.ptlogin2.qq.com/ptqrshow",
                params={"qr_push": "1", "qr_push_uin": str(push_uin),
                        "type": "1", "appid": PTLOGIN_APPID,
                        "t": str(random.random()), "ptlang": "2052",
                        "u1": GAME_URL, "daid": DAID, "pt_3rd_aid": "0"},
                headers={"Referer": "https://xui.ptlogin2.qq.com/"},
                timeout=15)
        except requests.RequestException as exc:
            return False, f"请求异常 {exc}"
        text = (r.text or "")[:300]
        m = re.search(r'"ec"\s*:\s*(\d+)', text)
        ec = m.group(1) if m else ""
        if ec == "0":
            return True, ""
        em = re.search(r'"em"\s*:\s*"([^"]*)"', text)
        why = f"ec={ec} {em.group(1) if em else ''}".strip() or text.strip()
        if ec == "313":
            why += ("（多半是缺 dev_mid_sig 设备绑定；"
                    "见 README「关于免扫码」）")
        return False, why

    def _ptqrshow(self, push_uin=None):
        """请求二维码。返回 (response, 失败原因)；成功时原因为空串。

        腾讯拒绝时不返回 PNG，而是回一段 JSONP 或空内容；早先代码把它当成图片
        写进 qrcode.png，只报一句"未获取到 qrsig"，看不出真正原因。
        """
        params = {
            "appid": PTLOGIN_APPID, "e": "2", "l": "M", "s": "3", "d": "72",
            "v": "4", "t": str(random.random()), "daid": DAID, "pt_3rd_aid": "0",
            "ptlang": "2052", "u1": GAME_URL,
        }
        try:
            r = self.session.get("https://xui.ptlogin2.qq.com/ssl/ptqrshow",
                                 params=params,
                                 headers={"Referer": "https://xui.ptlogin2.qq.com/"},
                                 timeout=15)
        except requests.RequestException as exc:
            return None, f"请求异常 {exc}"

        body = r.content or b""
        if body[:8] == b"\x89PNG\r\n\x1a\n":
            if not self._cookie("qrsig"):
                return None, "返回了图但没有 qrsig，无法轮询"
            return r, ""

        # 失败：把 ec 码解出来，好判断是"参数变了"还是别的
        text = body[:300].decode("utf-8", "replace").strip()
        m = re.search(r'"ec"\s*:\s*(\d+)', text)
        ec = m.group(1) if m else ""
        if r.status_code == 403:
            return None, "网关直接 403（参数组合不被接受）"
        if ec:
            m2 = re.search(r'"em"\s*:\s*"([^"]*)"', text)
            return None, f"ec={ec} {m2.group(1) if m2 else ''}".strip()
        return None, (f"HTTP {r.status_code}，{len(body)} 字节，"
                      f"content-type={r.headers.get('Content-Type', '?')}")

    def qr_login(self, timeout_sec: int = 180, on_qr=None, push_uin=None) -> bool:
        """扫码登录。on_qr(qrcode_path) 在二维码生成后回调（用于推送到手机等）。

        push_uin 非空时启用**推送登录**：不用扫码，腾讯直接往该 QQ 号的手机客户端
        推一条登录确认，用户点"确认登录"即可。这解决了"把二维码图片存到本地、
        用同一台手机的相册扫码"被拒（提示"限制本地扫码登录"）的问题 ——
        腾讯的防钓鱼策略要求二维码显示在**另一块屏幕**上，而推送登录没有这个限制。

        参数取自真实客户端抓包：ptqrshow?qr_push=1&qr_push_uin=<uin>&type=1
        """
        s = self.session
        # uin 要在清 cookie 之前取，否则就拿不到了
        if push_uin is None:
            push_uin = self.uin or None

        # 清会话票据但**保住设备凭据** —— 推送靠 dev_mid_sig 之类识别"推给哪台设备"，
        # 全清了就只能回 ec=313。
        self._clear_session_cookies(keep=DEVICE_COOKIES)
        # 浏览器进登录页第一件事就是 xlogin，它建立 pt_login_sig 上下文。
        # 但它同时会**无条件重新签发 pt_guid_sig**（实测：本来就有一个也照换），
        # 而 pt_guid_sig 是和 dev_mid_sig 配对的，被换掉就对不上了。
        # 所以先存后还。
        guid_before = self._cookie("pt_guid_sig")
        try:
            s.get("https://xui.ptlogin2.qq.com/cgi-bin/xlogin",
                  params={"daid": DAID, "appid": PTLOGIN_APPID,
                          "hide_title_bar": "1", "low_login": "0",
                          "qlogin_auto_login": "1", "no_verifyimg": "1",
                          "link_target": "blank", "style": "22",
                          "target": "self", "s_url": GAME_URL},
                  timeout=15)
        except requests.RequestException as exc:
            log.debug("xlogin 预热失败(忽略): %s", exc)
        if guid_before and self._cookie("pt_guid_sig") != guid_before:
            self._set_cookie("pt_guid_sig", guid_before)
            log.debug("xlogin 换掉了 pt_guid_sig，已还原成与设备记录配对的那个")

        # 先拿二维码：不管走不走推送都要这一步 —— 推送是挂在这个会话上的
        r, why = self._ptqrshow()
        if r is None:
            log.error("二维码请求失败，无法登录：%s", why)
            return False

        pushed = False
        if push_uin:
            pushed, why = self._push_login(push_uin)
            if not pushed:
                log.info("推送登录没成（%s），本次用扫码", why)

        with open(QRCODE_FILE, "wb") as f:
            f.write(r.content)
        qrsig = self._cookie("qrsig")

        if pushed:
            log.info("已向 QQ %s 推送登录确认 —— 打开手机QQ点「确认登录」即可，"
                     "不需要扫码（扫码图仍保存在 %s 作为备用）", push_uin, QRCODE_FILE)
        else:
            log.info("请用手机 QQ 扫码登录（二维码已保存: %s）", QRCODE_FILE)
        # 只在"本机交互式使用且没有别的送达方式"时才弹图片查看器。
        # 有 on_qr（PushPlus 推送）时再弹窗没意义；推送登录更是压根不需要看图。
        if os.name == "nt" and on_qr is None and not pushed:
            try:
                os.startfile(QRCODE_FILE)
            except OSError:
                pass
        if not pushed:
            _print_qr_ascii(QRCODE_FILE)
        if on_qr:
            try:
                # 带上 pushed，让调用方的文案跟实际走的路径一致
                # （推送失败回退到扫码时，不能还提示"点确认登录"）
                on_qr(QRCODE_FILE, pushed)
            except Exception as exc:
                log.warning("二维码推送回调失败: %s", exc)

        ptqrtoken = hash33(qrsig)
        # 轮询参数照浏览器来：带上 xlogin 拿到的 login_sig，以及 has_onekey=1
        # （"一键/推送登录"标记，推送确认要靠它认）。早先 login_sig 传空串、
        # 也没有 has_onekey，扫码能过但推送这条路认不出来。
        login_sig = self._cookie("pt_login_sig") or ""
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            r = s.get("https://xui.ptlogin2.qq.com/ssl/ptqrlogin", params={
                "u1": GAME_URL, "ptqrtoken": ptqrtoken, "ptredirect": "0",
                "h": "1", "t": "1", "g": "1", "from_ui": "1", "ptlang": "2052",
                "action": f"0-0-{int(time.time() * 1000)}",
                "js_ver": "26071711", "js_type": "1", "login_sig": login_sig,
                "pt_uistyle": "40", "aid": PTLOGIN_APPID, "daid": DAID,
                "has_onekey": "1",
            }, headers={"Referer": "https://xui.ptlogin2.qq.com/"},
                timeout=15)
            m = re.search(r"ptuiCB\('(\d+)','\d+','([^']*)','\d+','([^']*)'", r.text)
            if not m:
                log.warning("轮询响应无法解析: %s", r.text[:200])
                time.sleep(3)
                continue
            code, url, msg = m.group(1), m.group(2), m.group(3)
            if code == "0":
                log.info("扫码确认成功: %s", msg)
                s.get(url, allow_redirects=True, timeout=20)  # check_sig，种登录 cookie
                self._save_cookies()
                if self.is_valid():
                    log.info("登录完成，uin=%s", self.uin)
                    self._save_cookies()  # 校验过程可能刷新 cookie，再存一次
                    return True
                log.error("check_sig 后登录态仍无效")
                return False
            if code == "65":
                log.error("二维码已失效，请重新运行")
                return False
            if code == "67":
                log.info("已扫码，请在手机上确认…")
            time.sleep(3)
        log.error("扫码超时（%d 秒）", timeout_sec)
        return False

    def ensure_login(self, on_qr=None, push_uin=None) -> bool:
        """保证登录可用。顺序：现成 cookie → 长效凭据静默续期 → 推送/扫码登录。

        需要人工介入的那步是最后手段 —— 守护进程要无人值守地跑。
        push_uin 见 qr_login()：给了就用推送登录，免去扫码。
        """
        if self.is_valid():
            left = self.ticket_status().get("skey")
            log.info("cookie 有效，uin=%s%s", self.uin,
                     f"（skey 剩余约 %.1f 小时）" % (left / 3600) if left else "")
            # 快到期就提前续，别等失效了才补救
            if self.expires_within(6 * 3600) and self.has_long_term_ticket():
                log.info("skey 即将到期，提前静默续期…")
                self.silent_renew()
            return True
        if self.silent_renew():
            return True
        return self.qr_login(on_qr=on_qr, push_uin=push_uin)

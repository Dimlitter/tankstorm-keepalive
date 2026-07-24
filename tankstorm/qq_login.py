"""QQ 扫码登录（ptlogin2 协议），获取空间游戏所需 cookie（uin/skey/p_skey）。

流程：
  1. 请求 ptqrshow 拿二维码图片，同时得到 cookie qrsig；
  2. 手机 QQ 扫码；脚本轮询 ptqrlogin，携带 hash33(qrsig) 计算的 ptqrtoken；
  3. 扫码确认后返回 check_sig 跳转地址，GET 一次即种下全套登录 cookie；
  4. cookie 序列化到 cookies.json，下次运行直接复用；失效后需要重新扫码。

不涉及账号密码 —— 只用扫码，服务器上把 qrcode.png 取下来扫或直接看终端字符画。
"""

import json
import os
import random
import re
import time

import requests

from . import GAME_URL
from .log import get_logger

log = get_logger()

PTLOGIN_APPID = "549000912"  # QQ空间的 ptlogin aid（游戏页 302 时携带的就是它）
DAID = "5"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COOKIE_FILE = os.path.join(BASE_DIR, "cookies.json")
QRCODE_FILE = os.path.join(BASE_DIR, "qrcode.png")

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
                                         domain=c["domain"], path=c["path"])
            log.debug("已从 %s 载入 %d 条 cookie", self.cookie_file, len(jar))
        except Exception as exc:
            log.warning("cookie 文件读取失败，将重新登录: %s", exc)

    def _save_cookies(self) -> None:
        jar = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
               for c in self.session.cookies]
        with open(self.cookie_file, "w", encoding="utf-8") as f:
            json.dump(jar, f, ensure_ascii=False, indent=1)
        log.info("cookie 已保存到 %s", self.cookie_file)

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

    # ---------- 扫码登录 ----------

    def qr_login(self, timeout_sec: int = 180, on_qr=None) -> bool:
        """扫码登录。on_qr(qrcode_path) 在二维码生成后回调（用于推送到手机等）。"""
        s = self.session
        s.cookies.clear()

        r = s.get("https://ssl.ptlogin2.qq.com/ptqrshow", params={
            "appid": PTLOGIN_APPID, "e": "2", "l": "M", "s": "3", "d": "72",
            "v": "4", "t": str(random.random()), "daid": DAID, "pt_3rd_aid": "0",
        }, timeout=15)
        with open(QRCODE_FILE, "wb") as f:
            f.write(r.content)
        qrsig = s.cookies.get("qrsig")
        if not qrsig:
            log.error("未获取到 qrsig，二维码请求失败")
            return False

        log.info("请用手机 QQ 扫码登录（二维码已保存: %s）", QRCODE_FILE)
        if os.name == "nt":
            try:
                os.startfile(QRCODE_FILE)  # 本机运行时直接弹出图片
            except OSError:
                pass
        _print_qr_ascii(QRCODE_FILE)
        if on_qr:
            try:
                on_qr(QRCODE_FILE)         # 无头服务器上把二维码推送出去
            except Exception as exc:
                log.warning("二维码推送回调失败: %s", exc)

        ptqrtoken = hash33(qrsig)
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            r = s.get("https://ssl.ptlogin2.qq.com/ptqrlogin", params={
                "u1": GAME_URL, "ptqrtoken": ptqrtoken, "ptredirect": "0",
                "h": "1", "t": "1", "g": "1", "from_ui": "1", "ptlang": "2052",
                "action": f"0-0-{int(time.time() * 1000)}",
                "js_ver": "20032614", "js_type": "1", "login_sig": "",
                "pt_uistyle": "40", "aid": PTLOGIN_APPID, "daid": DAID,
            }, timeout=15)
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

    def ensure_login(self, on_qr=None) -> bool:
        if self.is_valid():
            log.info("cookie 有效，uin=%s", self.uin)
            return True
        return self.qr_login(on_qr=on_qr)

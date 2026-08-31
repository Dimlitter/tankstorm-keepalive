# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""消息推送：PushPlus（https://www.pushplus.plus）。

用途聚焦一件事：保活守护进程跑在服务器上时，如果 QQ 登录态过期、需要重新扫码，
就把二维码图片推送到你的微信，你扫一下即可恢复。平时不打扰。

token 配置在 config.local.json 的 通知.pushplus_token（该文件已 gitignore，不进仓库）。
"""

import base64

import requests

from .log import get_logger

log = get_logger()

PUSHPLUS_URL = "https://www.pushplus.plus/send"


def _token(config: dict) -> str:
    return (config.get("通知", {}) or {}).get("pushplus_token", "").strip()


def send(config: dict, title: str, content: str, template: str = "txt") -> bool:
    """推送一条文本/HTML 消息。content 为 HTML 时 template 传 'html'。"""
    token = _token(config)
    if not token:
        log.warning("未配置 pushplus_token（config.local.json 通知.pushplus_token），跳过推送")
        return False
    try:
        r = requests.post(PUSHPLUS_URL, json={
            "token": token, "title": title, "content": content, "template": template,
        }, timeout=15)
        data = r.json()
        if data.get("code") == 200:
            log.info("PushPlus 推送成功: %s", title)
            return True
        log.warning("PushPlus 推送返回异常: %s", data)
    except Exception as exc:
        log.warning("PushPlus 推送失败: %s", exc)
    return False


def send_qrcode(config: dict, title: str, qrcode_path: str, note: str = "") -> bool:
    """把二维码 PNG 以内嵌图片(HTML)推送。打开 PushPlus 消息即可看到二维码。"""
    try:
        with open(qrcode_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except OSError as exc:
        log.warning("读取二维码失败: %s", exc)
        return False
    html = (
        f'<p>{note or "坦克风暴登录态已过期，请用手机 QQ 扫码重新登录："}</p>'
        f'<p><img src="data:image/png;base64,{b64}" '
        f'style="width:220px;height:220px;border:1px solid #ddd"/></p>'
        f'<p style="color:#888;font-size:12px">'
        f'手机上可长按/保存图片，用「手机QQ→扫一扫→相册」选它扫码；'
        f'二维码有效期约 2 分钟，过期后脚本会自动重发。</p>'
    )
    return send(config, title, html, template="html")

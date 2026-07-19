"""运行结果通知：Server酱 或通用 webhook，都不配就只写日志。"""

import requests

from .log import get_logger

log = get_logger()


def send(config: dict, title: str, content: str) -> None:
    conf = config.get("通知", {})
    sendkey = conf.get("server酱sendkey", "").strip()
    webhook = conf.get("webhook", "").strip()

    if sendkey:
        try:
            requests.post(f"https://sctapi.ftqq.com/{sendkey}.send",
                          data={"title": title, "desp": content}, timeout=15)
            log.info("已通过 Server酱 推送结果")
        except requests.RequestException as exc:
            log.warning("Server酱 推送失败: %s", exc)

    if webhook:
        try:
            requests.post(webhook, json={"title": title, "content": content}, timeout=15)
            log.info("已通过 webhook 推送结果")
        except requests.RequestException as exc:
            log.warning("webhook 推送失败: %s", exc)

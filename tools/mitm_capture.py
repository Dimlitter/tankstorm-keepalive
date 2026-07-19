"""mitmproxy 插件：边玩边把游戏请求录成 HAR 风格的 capture.json。

适用场景：Flash 页游的请求不一定走浏览器 DevTools 能看到的通道，
但 Flash 播放器走系统代理（IE 代理设置），所以用 mitmproxy 挂系统代理最稳。

用法（本机 Windows）：
  1. pip install mitmproxy   （装在 D:\\miniconda 环境里即可）
  2. mitmdump -s tools/mitm_capture.py --set capture_filter=100616028,qzoneapp
  3. 首次需要装证书：代理生效后浏览器访问 http://mitm.it 按提示安装（Flash 大多
     走 http，不装证书通常也能抓到游戏请求）
  4. Windows 设置 → 网络 → 代理 → 手动，127.0.0.1:8080
  5. 打开游戏，把每日任务手动做一遍（签到、领奖、抽奖各点一次）
  6. Ctrl+C 停止，得到 capture.json，然后：
     python tools/har2endpoints.py capture.json
"""

import json
import time

from mitmproxy import ctx, http

OUTPUT = "capture.json"


class Capture:
    def __init__(self):
        self.entries = []

    def load(self, loader):
        loader.add_option("capture_filter", str, "100616028,qzoneapp",
                          "逗号分隔的 URL 关键字")

    def response(self, flow: http.HTTPFlow):
        keywords = [k.strip() for k in ctx.options.capture_filter.split(",")]
        if not any(k in flow.request.pretty_url for k in keywords):
            return
        try:
            resp_text = flow.response.get_text(strict=False) or ""
        except Exception:
            resp_text = "<binary>"
        self.entries.append({
            "request": {
                "method": flow.request.method,
                "url": flow.request.pretty_url,
                "postData": {"text": flow.request.get_text(strict=False) or ""}
                            if flow.request.content else None,
            },
            "response": {
                "content": {
                    "mimeType": flow.response.headers.get("content-type", ""),
                    "text": resp_text[:2000],
                }
            },
            "time": time.strftime("%H:%M:%S"),
        })
        ctx.log.info(f"[捕获 {len(self.entries)}] {flow.request.method} "
                     f"{flow.request.pretty_url[:100]}")
        self._save()

    def _save(self):
        with open(OUTPUT, "w", encoding="utf-8") as f:
            json.dump({"log": {"entries": self.entries}}, f,
                      ensure_ascii=False, indent=1)

    def done(self):
        self._save()
        ctx.log.info(f"共捕获 {len(self.entries)} 条，已保存到 {OUTPUT}")


addons = [Capture()]

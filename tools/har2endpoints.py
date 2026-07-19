"""把浏览器/抓包工具导出的 HAR 文件转换成 endpoints.json 的候选请求。

用法（在项目根目录）：
  python tools/har2endpoints.py 抓包.har
  python tools/har2endpoints.py 抓包.har --filter 100616028,qzoneapp,游戏方域名

做的事：
  1. 过滤：只保留命中 --filter 关键字的请求，剔除图片/SWF/JS 等静态资源；
  2. 参数模板化：把 URL 和 POST 体里出现的真实 openid/openkey/uin 值
     替换成 {openid}/{openkey}/{uin} 占位符，时间戳参数替换成 {ts_ms}；
  3. 输出 endpoints-candidates.json，每条带响应片段，方便辨认哪条是签到、
     哪条是领奖，然后把确认的条目复制进 endpoints.json 对应任务的 requests。

抓包方法见 README 第 3 步。
"""

import argparse
import json
import re
import sys
from urllib.parse import parse_qs, urlparse

STATIC_EXT = (".png", ".jpg", ".jpeg", ".gif", ".swf", ".js", ".css",
              ".ico", ".woff", ".mp3", ".xml", ".txt")


def collect_secrets(entries) -> dict:
    """扫描全部请求，取出现过的 openid/openkey/uin 真实值 → 占位符名。"""
    secrets = {}
    for e in entries:
        url = e["request"]["url"]
        qs = parse_qs(urlparse(url).query)
        for key, ph in (("openid", "openid"), ("openkey", "openkey"),
                        ("pf", "pf"), ("pfkey", "pfkey"), ("uin", "uin")):
            for v in qs.get(key, []):
                if len(v) >= 5:
                    secrets[v] = ph
        post = (e["request"].get("postData") or {}).get("text", "") or ""
        for m in re.finditer(r"(openid|openkey|pfkey|pf|uin)=([^&\s]{5,})", post):
            secrets[m.group(2)] = m.group(1)
    return secrets


def templatize(text: str, secrets: dict) -> str:
    for value, name in sorted(secrets.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(value, "{%s}" % name)
    # 13 位毫秒时间戳 → {ts_ms}，10 位秒时间戳 → {ts}
    text = re.sub(r"(?<=[=/])1[6-9]\d{11}(?=[&\s]|$)", "{ts_ms}", text)
    text = re.sub(r"(?<=[=/])1[6-9]\d{8}(?=[&\s]|$)", "{ts}", text)
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("har", help="HAR 文件路径")
    ap.add_argument("--filter", default="100616028,qzoneapp",
                    help="逗号分隔的 URL 关键字，命中任一才保留")
    ap.add_argument("-o", "--output", default="endpoints-candidates.json")
    args = ap.parse_args()

    with open(args.har, encoding="utf-8-sig") as f:
        har = json.load(f)
    entries = har.get("log", {}).get("entries", [])
    keywords = [k.strip() for k in args.filter.split(",") if k.strip()]

    picked = []
    for e in entries:
        url = e["request"]["url"]
        if not any(k in url for k in keywords):
            continue
        path = urlparse(url).path.lower()
        if path.endswith(STATIC_EXT):
            continue
        picked.append(e)

    if not picked:
        print("没有命中任何请求。试试放宽 --filter，比如加上游戏服务器的域名")
        return 1

    secrets = collect_secrets(picked)
    print(f"命中 {len(picked)} 条请求；识别到 {len(secrets)} 个可模板化的参数值")

    candidates = []
    for e in picked:
        req = e["request"]
        post = (req.get("postData") or {}).get("text") or None
        resp_text = ((e.get("response") or {}).get("content") or {}).get("text", "") or ""
        mime = ((e.get("response") or {}).get("content") or {}).get("mimeType", "")
        item = {
            "method": req["method"],
            "url": templatize(req["url"], secrets),
            "data": templatize(post, secrets) if post else None,
            "headers": {},
            "success_contains": [],
            "already_done_contains": [],
            "_响应类型": mime,
            "_响应片段": resp_text[:200],
        }
        if "amf" in mime.lower() or "octet-stream" in mime.lower():
            item["_警告"] = "响应像是 AMF/二进制协议，纯文本回放可能不够，需要额外编解码支持"
        candidates.append(item)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(candidates, f, ensure_ascii=False, indent=2)
    print(f"已写出 {args.output}\n"
          f"下一步：打开它，根据 _响应片段 辨认每条请求对应的功能，\n"
          f"把需要的条目（去掉 _ 开头的注释字段，补上 success_contains）\n"
          f"复制到 endpoints.json 对应任务的 requests 里。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

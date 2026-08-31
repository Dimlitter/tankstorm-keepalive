# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""配置驱动的任务执行引擎：按 endpoints.json 里的请求模板逐个回放。

模板里可用的变量（写成 {openid} 这种形式，仅替换已知变量名，URL 里出现的
其它花括号不受影响）：
  {openid} {openkey} {pf} {pfkey} {uin} {skey} {g_tk}   —— 来自登录态/游戏页
  {ts}      当前秒级时间戳
  {ts_ms}   当前毫秒级时间戳
  {rand}    0~1 随机小数（腾讯接口常用的防缓存参数）
  {date}    今天日期 YYYY-MM-DD
"""

import random
import re
import time
from datetime import date

import requests

from .log import get_logger

log = get_logger()

_KNOWN_VARS = ("openid", "openkey", "pf", "pfkey", "uin", "skey", "g_tk",
               "ts", "ts_ms", "rand", "date", "canvas_url", "game_host")
_VAR_RE = re.compile(r"\{(%s)\}" % "|".join(_KNOWN_VARS))


def render(template: str, ctx: dict) -> str:
    def sub(m):
        name = m.group(1)
        if name == "ts":
            return str(int(time.time()))
        if name == "ts_ms":
            return str(int(time.time() * 1000))
        if name == "rand":
            return str(random.random())
        if name == "date":
            return date.today().isoformat()
        return str(ctx.get(name, m.group(0)))
    return _VAR_RE.sub(sub, template)


def _render_obj(obj, ctx):
    if isinstance(obj, str):
        return render(obj, ctx)
    if isinstance(obj, dict):
        return {k: _render_obj(v, ctx) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_render_obj(v, ctx) for v in obj]
    return obj


class TaskResult:
    def __init__(self, name: str):
        self.name = name
        self.status = "跳过"       # 成功 / 已完成过 / 失败 / 跳过
        self.detail = ""


def _run_one_request(session: requests.Session, spec: dict, ctx: dict,
                     base_headers: dict, retries: int) -> tuple[bool, str]:
    """执行单条请求，返回 (是否成功, 摘要)。"""
    method = spec.get("method", "GET").upper()
    url = render(spec["url"], ctx)
    headers = dict(base_headers)
    headers.update(_render_obj(spec.get("headers", {}), ctx))
    data = spec.get("data")
    if data is not None:
        data = _render_obj(data, ctx)

    last_err = ""
    for attempt in range(1, retries + 1):
        try:
            r = session.request(method, url, data=data, headers=headers, timeout=20)
            text = r.text
            log.debug("%s %s -> %s %s", method, url.split("?")[0],
                      r.status_code, text[:300].replace("\n", " "))

            for kw in spec.get("already_done_contains", []):
                if kw in text:
                    return True, f"已完成过（命中「{kw}」）"
            ok_kws = spec.get("success_contains", [])
            if not ok_kws:
                return r.status_code < 400, f"HTTP {r.status_code}"
            for kw in ok_kws:
                if kw in text:
                    return True, f"成功（命中「{kw}」）"
            last_err = f"响应未命中成功关键字: {text[:120]}"
        except requests.RequestException as exc:
            last_err = f"请求异常: {exc}"
        if attempt < retries:
            time.sleep(2 * attempt)
    return False, last_err


def run_tasks(session: requests.Session, endpoints: dict, config: dict,
              ctx: dict, only: str | None = None) -> list[TaskResult]:
    """按 endpoints['tasks'] 顺序执行；config['任务'] 里为 true 的才跑。

    only: 只跑指定名称的任务（命令行 --task 用）。
    """
    switches = config.get("任务", {})
    retries = int(config.get("重试次数", 3))
    gap = float(config.get("请求间隔秒", 1.5))
    base_headers = _render_obj(endpoints.get("base_headers", {}), ctx)

    results = []
    for task in endpoints.get("tasks", []):
        name = task.get("name", "未命名任务")
        res = TaskResult(name)
        results.append(res)

        if only is not None and name != only:
            continue
        if only is None and not switches.get(name, False):
            res.detail = "config.json 中未开启"
            continue
        reqs = task.get("requests", [])
        if not reqs:
            res.status = "失败"
            res.detail = "endpoints.json 中没有为该任务配置请求（先抓包导入）"
            log.warning("[%s] %s", name, res.detail)
            continue

        log.info("── 执行任务: %s（共 %d 条请求）", name, len(reqs))
        ok_all, details = True, []
        for i, spec in enumerate(reqs, 1):
            repeat = int(spec.get("repeat", 1))
            for j in range(repeat):
                ok, detail = _run_one_request(session, spec, ctx, base_headers, retries)
                tag = f"请求{i}" + (f"#{j + 1}" if repeat > 1 else "")
                log.info("  %s: %s", tag, detail)
                details.append(f"{tag}:{detail}")
                if not ok:
                    ok_all = False
                    if not spec.get("continue_on_fail", False):
                        break
                time.sleep(float(spec.get("delay_after", gap)))
            else:
                continue
            break
        res.status = "成功" if ok_all else "失败"
        res.detail = "; ".join(details)
    return results


def summarize(results: list[TaskResult]) -> str:
    lines = []
    for r in results:
        if r.status == "跳过":
            continue
        lines.append(f"[{r.status}] {r.name} — {r.detail}")
    done = sum(1 for r in results if r.status == "成功")
    fail = sum(1 for r in results if r.status == "失败")
    lines.append(f"合计: 成功 {done}，失败 {fail}")
    return "\n".join(lines)

# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""每日任务的当天状态：各任务做了几次、上次发送时刻、服务器给的下次可用时刻。

存在 logs/daily-state.json，跨天自动清空。结构：

    {"date": "2026-09-01",
     "done":  {任务名: 今日成功次数},
     "last":  {任务名: 上次发送的 unix 秒},       # 写死冷却秒数的任务用
     "until": {任务名: 服务器给的下次可用时刻},   # 读响应得来的冷却
     "miss":  {任务名: 连续没等到响应的轮数}}

⚠️ 测试和重放脚本要先把 STATE_FILE 指到临时文件再跑，否则会把真实的当天
计数冲掉（踩坑记录第十一条，犯过两次）。
"""

import json
import os
from datetime import date

from ..log import LOG_DIR, get_logger

log = get_logger()

STATE_FILE = os.path.join(LOG_DIR, "daily-state.json")


def load() -> dict:
    """读今天的状态。文件不存在、读坏了或日期不是今天，都返回一份空的。"""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    if st.get("date") != date.today().isoformat():
        st = {"date": date.today().isoformat(), "done": {}, "last": {}}
    return st


def save(st: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except OSError as exc:
        log.debug("每日状态保存失败(忽略): %s", exc)


def reset() -> bool:
    """清空今日计数。返回原来有没有这个文件。"""
    if not os.path.exists(STATE_FILE):
        return False
    os.remove(STATE_FILE)
    return True

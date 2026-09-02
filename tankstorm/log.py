# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
import logging
import os
import sys
from datetime import date

from .paths import user_path                      # noqa: E402

# 日志和当天任务计数是**可写数据**，打包后要落在 exe 旁边而不是临时解压目录，
# 否则每次运行完就被删掉，当天次数永远从零开始。见 paths.py。
LOG_DIR = user_path("logs")


def _force_utf8_console() -> None:
    """把 stdout/stderr 钉成 UTF-8。

    输出**重定向到文件或管道**时，Python 不走控制台的 Unicode 通道，
    而是回落到系统 locale 编码 —— 中文 Windows 上是 GBK，编不了日志里的
    ✅ / ❌，于是抛 UnicodeEncodeError 把整个进程带崩。
    README 里 cron 那行 `>> logs/cron.log` 正是这种用法，打包成 exe 后
    用户更容易这么跑，所以这不是显示问题，是会真的挂掉。

    errors="replace" 是兜底：再冷门的字符最多显示成问号，不该让程序退出。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass          # 流被换成了不支持 reconfigure 的对象，忽略即可


_force_utf8_console()


def get_logger(name: str = "tankstorm") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    os.makedirs(LOG_DIR, exist_ok=True)
    logfile = os.path.join(LOG_DIR, f"{date.today().isoformat()}.log")
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    return logger

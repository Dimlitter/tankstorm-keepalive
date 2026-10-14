# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""工具脚本共用：把 stdout/stderr 钉成 UTF-8。

输出重定向到文件或管道时，Python 回落到系统 locale 编码 —— 中文 Windows 上是
GBK，编不了 ✅ 这类字符就抛 UnicodeEncodeError，脚本跑一半就崩。主程序在
tankstorm/log.py 里修过，工具脚本不经过那里，所以单独来一份。
"""

import sys


def utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""路径解析：区分「随程序走的只读数据」和「用户自己的可写数据」。

为什么需要这个模块
------------------
项目原本所有路径都从 `__file__` 往上推，源码运行没问题。但一旦用 PyInstaller
打成单文件 exe，`__file__` 指向的是**临时解压目录**（`sys._MEIPASS`），
进程退出就被删掉。于是会出现这种情况：

  · cookies.json 写进临时目录 → 下次启动又要扫码
  · logs/ 和 daily-state.json 每次运行完就蒸发 → 当天次数永远重新计
  · 用户改了 exe 旁边的 config.json → 程序根本读不到

症状是"看着能跑，实际什么都留不下"，而且不会报错，很难发现。

于是把路径分成两类：

  app_dir()      可写的用户数据。打包后 = **exe 所在目录**，源码运行 = 仓库根目录。
                 cookies.json / logs/ / debug/ / config.local.json 都归这里。
  bundled_dir()  只读的随包数据。打包后 = PyInstaller 解压目录，源码运行 = 仓库根目录。
                 schema.json、以及 config.json / protocol.json 的出厂默认值。

`data_file()` 把两者接起来：exe 旁边有用户自己那份就用它，没有就退回随包的默认值。
"""

import os
import shutil
import sys

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_ROOT = os.path.dirname(_PKG_DIR)


def is_frozen() -> bool:
    """是不是 PyInstaller 打出来的可执行文件。"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> str:
    """可写数据目录。

    打包后取 **exe 自己所在的目录**（不是 `_MEIPASS`，那个会被删），
    这样用户下载一个 exe 放到哪，cookies 和日志就跟到哪。
    """
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return _SRC_ROOT


def bundled_dir() -> str:
    """只读随包数据目录。源码运行时就是仓库根目录。"""
    return getattr(sys, "_MEIPASS", None) or _SRC_ROOT


def user_path(name: str) -> str:
    """用户数据的完整路径（不检查是否存在）。写文件一律用这个。"""
    return os.path.join(app_dir(), name)


def data_file(name: str) -> str:
    """读配置/协议表用：优先 exe 旁边用户自己那份，没有才用随包默认值。"""
    mine = user_path(name)
    if os.path.exists(mine):
        return mine
    return os.path.join(bundled_dir(), name)


def ensure_user_copy(name: str) -> bool:
    """把随包的默认文件复制到 exe 旁边，方便用户直接改。

    已经存在就原样不动（绝不覆盖用户改过的内容）。返回是否真的复制了。
    只在打包运行时有意义 —— 源码运行两边是同一个文件。
    """
    if not is_frozen():
        return False
    mine = user_path(name)
    if os.path.exists(mine):
        return False
    src = os.path.join(bundled_dir(), name)
    if not os.path.exists(src):
        return False
    try:
        shutil.copyfile(src, mine)
        return True
    except OSError:
        return False

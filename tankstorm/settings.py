# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""读配置：config.json 打底，config.local.json 深度覆盖（PushPlus token 等密钥放后者）。

探测脚本、测试脚本要读配置也用这里的 load()。直接 json.load(config.json)
读不到 config.local.json 里的 token，二维码就推不出去。
"""

import json
import os

from . import paths

# 打包成 exe 后，第一次运行要复制到 exe 旁边让用户改的出厂文件
USER_EDITABLE = ("config.json", "protocol.json")


def load_json(path: str, required: bool = True) -> dict:
    """读一个 JSON 文件。required=True 时文件不存在抛 FileNotFoundError。"""
    if not os.path.exists(path):
        if required:
            raise FileNotFoundError(f"缺少文件: {path}")
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def deep_merge(base: dict, over: dict) -> dict:
    """把 over 深度合并进 base 并返回 base。"""
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load() -> dict:
    """config.json + config.local.json 合并后的配置。

    config.json：exe 旁边有用户那份就用它，没有才用随包默认值。
    config.local.json：放密钥，已 gitignore，一律在程序目录。
    路径在调用时才解析，这样首次运行刚复制出来的那份也能读到。
    """
    config = load_json(paths.data_file("config.json"))
    local = load_json(paths.user_path("config.local.json"), required=False)
    return deep_merge(config, local)


def ensure_user_copies() -> list:
    """打包运行时把出厂配置复制到 exe 旁边（已存在绝不覆盖）。返回复制了哪些。

    不复制的话用户看不到 config.json，也就无从配置。源码运行时什么都不做。
    """
    return [f for f in USER_EDITABLE if paths.ensure_user_copy(f)]

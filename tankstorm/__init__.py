# tankstorm-keepalive  Copyright (C) 2026 Dimlitter
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# 本程序是自由软件：你可以依据自由软件基金会发布的 GNU Affero 通用公共许可证
# （第 3 版，或你选择的任何更新版本）之条款，再分发和/或修改它。
# 本程序希望能有用，但不提供任何担保；甚至不含适销性或特定用途适用性的默示担保。
# 详见随附的 LICENSE 文件，或 <https://www.gnu.org/licenses/>。
"""坦克风暴（QQ空间 appid 100616028）保活、每日任务与国战 —— 纯 Python 复刻游戏 socket 连接。

模块分工
--------
    qq_login / qzone      QQ 扫码登录，从游戏页取 socket 登录要的参数（FlashVars）
    session               连服务器、登录、收完登录推送；登录态失效时重新登录
    socket_keepalive      保活守护进程（--keepalive）
    jobs                  一次性任务（--daily / --country-war）
    daily/                每日任务：任务表、闸门、判成败、当天状态
    arena / country_war / guild / shop
                          争霸战、国战、公会战、功勋商城（每日任务的自定义执行器）
    wire / account        请求原语（发请求、等回包、心跳）与账号状态读取
    protocol / crypto / proto_encode / schema / sender
                          帧格式、RC4、protobuf 编解码、opcode 表
    wiremap               按游戏版本在本项目编号和线上编号之间翻译（2026102102 起协议会被打乱）
    recorder / stream_recorder
                          双向消息录制、异常事件告警、原始流落盘
    settings / paths / log / notify
                          配置、路径、日志、PushPlus 推送
"""

APPID = "100616028"
GAME_URL = f"https://game.qzone.qq.com/{APPID}?via={APPID}_QZSTORE.V6.MY-APPS&"

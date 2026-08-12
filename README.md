# tankstorm-keepalive

QQ 空间 Flash 页游「坦克风暴」（appid `100616028`）的**保活守护**与**每日任务**工具。

纯 Python 请求实现 —— 不开浏览器、不跑 Flash、不截图识图，可以扔到无界面的
Linux 服务器上常驻。

```bash
python main.py --keepalive    # 保活：常驻，防掉线
python main.py --daily        # 每日任务：跑一轮，退出
```

---

## 目录

- [这是什么](#这是什么)
- [两个独立的东西](#两个独立的东西)
- [安装](#安装)
- [登录](#登录)
- [保活](#保活)
- [每日任务](#每日任务)
- [部署](#部署)
- [配置](#配置)
- [安全机制](#安全机制)
- [命令一览](#命令一览)
- [排错](#排错)
- [开发](#开发)

---

## 这是什么

| 能力 | 说明 |
|---|---|
| **保活** | 连游戏 socket 定时发心跳，防止长时间无操作被踢下线导致基地被打 |
| **每日任务** | 自动做签到、英雄开采、将领冶炼、技能书、特工派遣、配件探索、军备制造、战略训练、军事演习、矿区争夺、国家宝箱、公会捐献等 |
| **事件告警** | 被超级强攻时 PushPlus 推到微信，带进攻方名字 |

不做的事：**不参与游戏内人机验证（超级强攻验证码）的识别、填写或自动应答。**
脚本只负责观察、解码、及时通知你本人。

---

## 两个独立的东西

这是本项目最重要的一条约定，**保活和每日任务不是一回事**：

|  | 保活 `--keepalive` | 每日任务 `--daily` |
|---|---|---|
| 形态 | 常驻进程，跑几天几周 | 一次性批处理，跑完退出 |
| 职责 | 只有一个：别掉线 | 把当天的免费次数领干净 |
| 断线 | 自动重连，退避重试 | 直接结束，下次再跑 |
| 出错 | 必须尽量不出错 | 失败无所谓，明天还能领 |

**为什么要分开**：早先每日任务挂在保活里面，于是每次断线重连都要重跑一轮任务，
任务本身出问题还会牵连保活这个更重要的进程。现在默认互不干扰。

确实想"连上顺带领一轮"，显式写：

```bash
python main.py --keepalive --daily
```

---

## 安装

需要 **Python 3.10+**（代码里用了 `str | None` 这类新语法）。

```bash
conda create -n tank python=3.12 -y && conda activate tank
pip install -r requirements.txt
```

> Windows 本机注意：如果 `python` 指向的是旧版（`python --version` 看一下），
> 直接用完整路径，例如 `D:\miniconda\python.exe main.py --check`。

---

## 登录

```bash
python main.py --login
```

用手机 QQ 扫码。二维码会同时：终端字符画、存成 `qrcode.png`、推到 PushPlus（若已配）。

> **必须用另一台设备扫。** 存到手机再用同一台手机的相册扫，腾讯会拒，
> 提示「限制本地扫码登录」。

登录态约 1.5 天过期，脚本会用长效凭据（`ptcz`/`RK`/`superkey`）**自动续期**，
正常情况几周不用再扫。cookie 存在 `cookies.json`（已 gitignore）。

验证：

```bash
python main.py --check
```

会打印 `uid` / `sid` / `level` 等游戏上下文，能打印出来就说明登录态可用。

> **推送登录（免扫码）目前不可用。** 腾讯把 `ptqrshow` 的 `qr_push` 参数形状改了：
> 带 `type=1` 一律返回 `ec=313 提交参数错误`，换别的 `type` 值网关直接 403。
> 设备凭据齐全，不是缺票据。脚本会自动回退到扫码，不影响使用。

---

## 保活

```bash
python main.py --keepalive
```

- socket 连 `tankstorm-proxy.sincetimes.com:8001`
- 每 10 秒发 8 字节心跳 `0006040e00000000`
- 断线自动重连（退避 5s → 120s）
- 登录态失效会自动重新登录，需要人工扫码时把二维码推到 PushPlus

`Ctrl+C` 停止。想跑固定时长就设 `保持活跃.持续小时`。

---

## 每日任务

```bash
python main.py --daily      # 跑一轮
python main.py --list       # 看有哪些任务、今天做了几次
python main.py --reset      # 清空今日计数
```

### 它是怎么做一件事的

每个任务都走同一条流水线，**每一步都不能省**：

```
前置请求（开面板/查询）
      ↓  真实客户端每个动作前都会先发这个，服务端据此建上下文
闸门（读前置的响应）
      ↓  剩余免费次数 > 0 才继续；读不到就不做
动作请求
      ↓
判成败（按 ret）
      ↓  ret=0 成功；ret=1 要花钱→停；其它 ret→当天不再试
后续步骤（可选）
         占下探到的矿、按活跃度逐档领奖、国家宝箱领取+开箱
```

一轮里会**把当天的次数做完**，而不是做一次就走。免费次数是**分档**的，
比如英雄开采 `freeVisitCnt=[3,1,1]` 表示低/中/高三档各有 3/1/1 次，
三档都要领 —— 高档收益高一个数量级（实测 `nAddRes` 5000 / 10000 / 50000）。

### 任务状态

`--list` 里每项标着：

- **实测** —— 参数来自真实抓包，可放心开
- **待确认** —— 默认跳过，需先抓包核对

---

## 部署

保活和每日任务分开跑。

**systemd（保活常驻）**

```ini
[Unit]
Description=tankstorm keepalive
After=network-online.target

[Service]
WorkingDirectory=/opt/tankstorm
ExecStart=/opt/conda/envs/tank/bin/python main.py --keepalive
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

**cron（每日任务，每天早上跑一次）**

```cron
30 7 * * * cd /opt/tankstorm && /opt/conda/envs/tank/bin/python main.py --daily >> logs/cron.log 2>&1
```

任务有冷却或次数没领完时，多跑几次没有坏处 —— 已完成的会自动跳过。

---

## 配置

`config.json` 是模板；**密钥放 `config.local.json`**（已 gitignore，会深度合并覆盖）。

```jsonc
{
  "保持活跃": { "启用": true, "心跳间隔秒": 0, "重连最小秒": 5, "重连最大秒": 120 },
  "每日任务": {
    "启用": true,
    "间隔秒": 3,
    "响应等待秒": 6,
    "允许未实测参数": false,
    "任务": { "每日签到": true, "英雄开采": true }
  },
  "录制": { "启用": true, "实时解密": true, "自动拒绝超级强攻": true },
  "通知": { "PushPlus": { "token": "放到 config.local.json 里" } }
}
```

> `录制.启用` 别关。整条解码链路都挂在它下面，关掉之后每日任务读不到任何响应。

---

## 安全机制

**绝不消耗券、勋章等付费资源**，四层拦截：

| 层 | 作用 |
|---|---|
| **白名单** | 只允许发任务表里登记的 opcode，表外的连构造机会都没有 |
| **危险字段硬校验** | 字段名命中 `credit/cost/buy/num/cnt/item/card/ticket/discount` 等的，值必须为 0，否则拒发 |
| **免费次数闸门** | 先查询、读到剩余次数 > 0 才发；**读不到就不发** |
| **每日次数上限 + 冷却** | 每项每天最多 N 次，可重复任务两次之间还要等冷却 |

还有一条铁律：**没有次数依据就不重复**。曾经给没有次数字段的任务放开重试、
靠"服务器拒绝再收手"来试探，结果是免费次数用完后继续发，服务器转而扣勋章。
现在读不到剩余次数的任务一轮只做一次。

> 游戏里"消耗勋章"会弹确认框，但那是**纯客户端 UI** —— 协议里不存在二次确认消息。
> 脚本直接发包不经过任何对话框，服务器收到就扣。所以确认框对脚本的保护是 0，
> 必须在我们这一侧把危险字段拦死。

---

## 命令一览

| 命令 | 作用 |
|---|---|
| `--login` | 强制重新扫码登录 |
| `--check` | 验证登录态，打印 uid/sid/level |
| `--keepalive` | 保活常驻 |
| `--keepalive --daily` | 保活，且每次连上后顺带跑一轮任务 |
| `--daily` | 跑一轮每日任务后退出 |
| `--list` | 列出每日任务及今日进度 |
| `--reset` | 清空今日任务计数 |

不带参数会打印用法。

---

## 排错

| 现象 | 原因 / 处理 |
|---|---|
| `TypeError: unsupported operand type(s) for \|` | Python 版本低于 3.10，换 3.12 |
| 所有任务都「未收到响应」 | 先看 `录制.启用` 是不是被关了 |
| 「闸门拦截：没收到 RseXxxOpen」 | 前置请求没回包，多半是登录态/连接问题，`--check` 看一眼 |
| 「需要花钱才能做」 | 正常，免费次数用完了，脚本主动停手 |
| 「没有可做的（已领过）」 | 正常，今天那份已经领过 |
| 推送登录失败 | 已知问题，见[登录](#登录)，自动回退扫码 |

日志在 `logs/`：

- `logs/YYYY-MM-DD.log` —— 运行日志
- `logs/frames-YYYY-MM-DD.jsonl` —— 收发的每一帧（含解码结果），排错主力
- `logs/streams/<会话>/` —— 原始字节流，供事后离线解密

> `logs/` 里有真凭据（uid/sid/openkey），已 gitignore，发出去前想清楚。

---

## 开发

```
main.py              命令行入口
tankstorm/
  qq_login.py        QQ 扫码登录（ptlogin2）
  qzone.py           取游戏页 FlashVars（uid/sid/level/firstLogin）
  socket_keepalive.py 保活主循环 + 单轮任务入口
  daily.py           每日任务表与执行引擎
  crypto.py          RC4（双向，密钥从 FlashVars 推）
  recorder.py        收发录制、实时解密、事件告警
  schema.py          协议 schema，protobuf 解码
  proto_encode.py    protobuf 编码
tools/               抓包分析与协议还原工具链
docs/                协议文档
```

### 抓包分析链路

```bash
python tools/pcap_split.py 抓包.pcapng -o streams/
python tools/redwar_rc4.py streams/<端口>/c2s.bin \
       --uid U --sid S --level L --first-login false --write
python tools/capture_daily.py streams/<端口>/c2s.bin.decrypted/frames.jsonl
```

### 游戏更新后

opcode 或字段号可能变。重新还原协议：

```bash
python tools/extract_proto.py <新的.swf> out
cp out/schema.json tankstorm/ && cp out/redwar.proto out/opcodes.json docs/
```

---

## 文档

原理、协议逆向过程、RC4 怎么破的 —— 见 Wiki。

仓库内保留：

- [docs/protocol-reverse-engineering.md](docs/protocol-reverse-engineering.md) —— TCP 二进制协议逆向入门与踩坑
- [docs/redwar.proto](docs/redwar.proto) —— 从 SWF 还原的完整协议定义
- [docs/opcodes.json](docs/opcodes.json) —— opcode ↔ 消息名对照

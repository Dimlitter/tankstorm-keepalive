# 坦克风暴 自动脚本（保持活跃 + 任务）

QQ空间 Flash 小游戏「坦克风暴」（appid 100616028）的**服务器端保持在线**脚本：用纯
Python `socket` 复刻游戏客户端与服务器的心跳连接，让账号在无人操作时也不会因闲置被踢
下线（避免基地被打）。无需浏览器、无需 Flash、无需图像识别，可挂在无界面的 Linux
服务器上常驻。

> ⚠️ **免责声明**：本项目为个人学习与自用的游戏自动化工具，仅用于维持**自己账号**的在线
> 状态。与游戏运营方、腾讯均无关联。使用可能违反游戏用户协议，请自行评估风险，后果自负。
>
> 🔒 **隐私**：`cookies.json`、抓包文件（`*.pcapng/*.har/*.saz`）、`debug/`、`logs/`
> 含你的真实登录凭据（openid/openkey/QQ号等），已在 `.gitignore` 中排除，**切勿上传**。

## 架构真相（抓包 + SWF 字节码双重确认）

坦克风暴是 **Flash 游戏**，主程序 `RedWar.swf` 通过 **裸 TCP socket** 连到
`tankstorm-proxy.sincetimes.com:8001`（备用 443）跟服务器通信。

因此：

- **游戏核心动作（含"在线状态"、签到、领奖、抽奖等）都走这条 socket**，不是 HTTP。
  用 HTTP 抓包工具（浏览器 HAR、Fiddler、mitmproxy）**抓不到**它们——那些只能看到
  资源下载（.swf/.dat/图片）和 QQ 空间平台埋点。
- **保持活跃 = 维持这条 socket + 定时心跳。** 本项目用纯 Python `socket` 复刻它。
- 少数**社交功能**（好友赠礼/邀请/召回）确实是 HTTP `.war` 接口，可用 HTTP 脚本做。

> ⚠️ **勘误**：本文档早期版本依据 `config.xml` 里的 `encrypt=false` 断言"协议未加密"，
> **这是错的**。真实情况是 **protobuf 序列化 + RC4 加密**，只有白名单内的少数 opcode
> 走明文（见下方「协议与加密」）。当初之所以误判，是因为两个巧合：静态重放用到的
> 心跳/认证包恰好都在**发送豁免名单**里，能读到中文昵称的世界广播恰好在**接收豁免名单**
> 里 —— 两者都推不出"协议没加密"。教训写在
> [docs/protocol-reverse-engineering.md](docs/protocol-reverse-engineering.md)。

登录参数链路（`tankstorm/qzone.py` 已实现，离线验证通过）：

```
game.qzone.qq.com/100616028   ← 带 QQ cookie 请求，Qzone 当场签发 openid/openkey
      │  <iframe data-src="https://tankstorm-qzone.sincetimes.com/?openid=..&openkey=..&pfkey=..">
      ▼
tankstorm-qzone.sincetimes.com/?openid=..   ← 外框页
      │  <param name="FlashVars" value="..&uid=..&secret=..&server=tankstorm-proxy.sincetimes.com&port=8001&sid=..&region=..">
      ▼
连 socket：tankstorm-proxy.sincetimes.com:8001
```

好消息：openid/openkey 每次由 Qzone 用登录 cookie 现签，**保活守护进程能全自动拿到
新鲜票据**，不需要人工交互。

## 目录

```
main.py                     入口（--login/--check/--keepalive/--task/--list）
config.json                 开关：保持活跃、任务、通知（token 留空，填到 config.local.json）
config.local.json           本地密钥（PushPlus token），已 gitignore，不进仓库
protocol.json               socket 协议细节（登录/心跳字节）—— 抓包后生成，缺它保活会提示
protocol.example.json       协议模板示例（说明结构）
endpoints.json              社交 HTTP 任务模板（好友赠礼等，可选）
tankstorm/
  qq_login.py               扫码登录 + cookie 持久化
  qzone.py                  提取 openid/openkey/uid/sid/level/firstLogin（登录+密钥材料）
  protocol.py               按 protocol.json 拼/拆 socket 包
  socket_keepalive.py       保持在线守护进程（连 socket、心跳、掉线重连、掉线推二维码）
  recorder.py               双向录制 + 实时解密 + 异常事件告警
  crypto.py                 RC4 实现、双向密钥推导、豁免名单（录制器与离线工具共用）
  schema.py / schema.json   opcode↔消息名对照 + protobuf 解码（563 opcode / 873 消息）
  stream_recorder.py        socket 旁路，原始双向字节流落盘（解密的前提）
  notify.py                 PushPlus 推送（掉线需扫码时把二维码发到微信）
  engine.py                 HTTP 任务引擎（社交任务用）
tools/
  ── 抓包与日志分析 ──
  pcap_analyze.py           解析 Wireshark 抓包 → 定位登录握手+心跳（零依赖）
  analyze_frames.py         分析录制日志，定位异常事件消息
  har2endpoints.py          HAR → 社交 HTTP 任务模板
  ── SWF 逆向工具链 ──
  swfparse.py               SWF 容器：解压 + 遍历 tag
  abcparse.py               ABC 字节码结构解析（常量池/类/方法/traits）
  disasm.py                 AVM2 反汇编 + load() 带缓存；其余三个工具的基础
  extract_proto.py          从 SWF 还原全套协议（换游戏版本时重跑）
  dump_class.py             按类名反汇编，看成员和方法体
  xref.py                   全库交叉引用（按字符串/名字找引用点）
  ── 离线解密 ──
  redwar_rc4.py             对原始流做离线 RC4 解密并按 schema 标注字段
docs/
  protocol-reverse-engineering.md   TCP 二进制协议逆向入门（方法论 + 本项目实例）
  加密与协议还原.md                  RC4 与协议还原的完整逆向记录
  redwar.proto                      还原出的 .proto（873 消息 / 5582 字段）
  opcodes.json                      opcode → 消息名对照（563 条）
run_daily.bat / run_daily.sh / requirements.txt
```

## 环境

本机（Windows，D:\miniconda，Python 3.12）：

```bat
D:\miniconda\python.exe -m pip install -r requirements.txt
```

服务器（conda）：

```bash
conda create -n tank python=3.12 -y && conda activate tank
pip install -r requirements.txt
```

## 配置 PushPlus（掉线扫码提醒，可选但强烈建议）

保活跑在服务器上时，QQ 登录态偶尔会过期、需要重新扫码。为此脚本会把二维码
通过 [PushPlus](https://www.pushplus.plus) 推到你微信，你扫一下即可恢复——平时不打扰。

1. 微信关注 PushPlus，拿到你的 token；
2. 新建 `config.local.json`（此文件已 gitignore，不会上传）：

```json
{ "通知": { "pushplus_token": "你的token" } }
```

不配也能用，只是掉线时不会主动通知你。

## 第 1 步：登录 + 自检（本机）

```bat
D:\miniconda\python.exe main.py --check
```

弹出 `qrcode.png`，手机 QQ 扫码。`--check` 会打印提取到的 socket 登录参数
（server/port/openid/openkey/uid/sid…）。全都有值就说明登录链路通了。

## 协议与加密（抓包 + SWF 字节码还原）

```
protobuf 序列化 (com.netease.protobuf, BIG_ENDIAN)
        ↓
RC4 加密（仅 body，白名单 opcode 豁免）
        ↓
包头 [2字节大端长度 N][2字节 opcode][4字节 seq] —— 永远明文
        ↓
裸 TCP socket
```

**帧**：整帧 = `2 + N`，长度字段不含自身；客户端所有包 `seq` 恒为 0，服务器 `seq` 单调递增。

**登录序列（4 步，`protocol.json`）**：TGW 网关头 → `a,{uid},{secret}` →
认证包 `041c`（固定密钥 `Redwarhq2018HoneyHoneyHoney`）→ build 包 `041d`。
`uid/secret` 每次由 `qzone.py` 从游戏页 FlashVars 现取。

**心跳**：`00 06 04 0e 00 00 00 00`（opcode `040e`），每 10 秒一次。

**RC4 加密**（`tankstorm/crypto.py`，源自 `RedWar_2026073102.swf` 字节码）：

| 项 | 内容 |
|---|---|
| 密钥中段 | `mid = int(level) * 100 + (0 if firstLogin else 1)` |
| 接收密钥 | `uid ‖ mid ‖ sid`，S 表**倒序** `S[k] = 255 - k` |
| 发送密钥 | `sid ‖ uid ‖ mid`，S 表正序 `S[k] = k` |
| 魔改点 | KSA 取模用 `len(key) - 2`；KSA 结束后 `i = j = 11`（均非混淆假分支） |
| 接收豁免 | `0215 0228 0229 0230 0283` |
| 发送豁免 | `040e 041c 041d 0455` |

密钥三要素 `uid/sid/level/firstLogin` **全部来自网页 FlashVars**，socket 还没连就齐了，
所以保活进程能边收边解（见下一节）。

> ⚠️ **密钥流是连续的**：RC4 状态在同方向所有**非豁免 body** 之间累积，中途漏一条就
> 永久错位。这也是为什么孤立的单个数据包在数学上无法解密，以及为什么
> `录制 → 原始流` 默认开启且不建议关。

**换游戏版本后**：重跑 `python tools/extract_proto.py <新swf> out`，把 `out/schema.json`
拷进 `tankstorm/`。豁免名单若有变动，录制器会主动告警提示需要重新核对。

## 第 2 步：启动保持活跃

```bat
D:\miniconda\python.exe main.py --keepalive
```

它会：拉取游戏页取最新 openid/openkey/uid/secret → （可选）调 loadIdInfo.war 预热
→ 连 `tankstorm-proxy.sincetimes.com:8001` → 发登录序列 → 每 10 秒发心跳 → 掉线自动
刷新票据重连。`config.json → 保持活跃` 控制间隔、运行时长、重连退避。

> **实盘验证要点**：闲置踢下线很可能是 Flash 客户端自己检测鼠标不动后弹广告并断开
> （客户端行为），纯 Python 客户端没有这个逻辑，理论上维持 socket+心跳就能一直在线。
> 首次跑起来后，观察日志是否稳定发心跳、有没有被服务器断开。若服务器仍会断开（说明
> 它在服务端做闲置判断，可能需要那个每 10 秒的 0422 轮询包），把当时的日志发我，
> 我再从抓包里补上对应逻辑。

部署到服务器（Linux）挂后台常驻：

```bash
nohup ~/tank/run_keepalive.sh &     # 或用 systemd / screen / tmux
```

cookie 失效时守护进程会**自动生成新二维码并推送到 PushPlus**（你微信里点开即可看到），
扫码后自动恢复在线，不用你上服务器操作；没扫的话会隔一会儿重发新码。

## 第 3 步：异常事件录制与告警（应对「超级强攻令」）

游戏里别人可以用**超级强攻令**强制进攻你，这种事很随机，没法蹲着抓包。
保活守护进程 24 小时挂在 socket 上，正好顺带做三件事：

**1. 双向录制**
服务器下行与客户端上行都按帧切好写进 `logs/frames-日期.jsonl`（高频大包采样，
避免日志爆炸）；原始字节流一字不差存到 `logs/streams/<会话>/`（解密的前提）；
重点加密载荷另存 `logs/enc/*.bin`，永不采样、永不截断。

**2. 实时解密**
密钥三要素在 FlashVars 里就位，连接建立时即可建双向 RC4，边收边解。
启动日志会打印「实时解密已就绪」和自检结果。

> 自检机制：前 6 条非豁免消息解出来必须是合法 protobuf，通不过就**自动退回"只录不解"**，
> 原始流照常保留 —— 密钥错时不会静默产出垃圾数据。

**3. 实时告警**（PushPlus 推微信）

| 触发条件 | 说明 |
|---|---|
| 已确认事件消息 | `027c` RseSuperStormOpt、`0268` RseCountryOpt —— **每次都报**，不受"只报一次"/登录静默/已学习影响 |
| 没见过的消息类型 | 主力检测器；登录爆发期只学不报，学到的存进 `logs/known-opcodes.json`，重启后不重复打扰 |
| 消息命中关键词 | 且**提到你自己**（uid/昵称）才算 |
| 服务器推来图片 | 保留作廉价哨兵，但见下方说明 |

解密启用后，超级强攻的推送会**直接带上进攻方身份**：

```
⚠️ RseSuperStormOpt —— 超级强攻通知
atkName=钢铁洪流｜atkUid=99887766｜deftName=我的基地
```

比一张要眯眼辨认的验证码图片可操作性强得多 —— 你一眼就知道被谁打了。

> ⚠️ **关键词匹配的坑（已实测踩到）**：`0283` 是**世界广播**，正文里全是其他玩家的
> 自定义昵称。有玩家取名叫「强攻宝贝」「在线包强攻，强攻令2270」，广播一刷就误报。
> 所以广播类消息只有在**提到你自己**时关键词才算数；被忽略的命中会记为
> `keywords_ignored`，可事后核对。uid 自动填入，也可在 `config.json → 录制 → 我的标识`
> 里补游戏昵称。

> ℹ️ **图片检测在本协议上不会命中**：还原出的 873 个消息、5582 个字段里
> **`bytes` 类型为 0 个** —— socket 根本不传二进制块。名字带 `pic` 的字段全是 `string`，
> 装的是 QQ 头像标识，客户端拿去走 HTTP 取图。该检测留作通用哨兵，别指望它。

> 📌 **定位**：本项目只负责**观察、解读并第一时间通知你本人**，不代为回应游戏内的
> 人机验证。

事后分析：

```bash
python tools/analyze_frames.py --around 19:13     # 某时刻前后 5 分钟
python tools/analyze_frames.py --unknown          # 只看没见过的消息类型

# 离线解密原始流（sid 从同目录 meta.json 自动读）
python tools/redwar_rc4.py logs/streams/<会话>/s2c.bin --uid <你的uid> --write
```

## 第 4 步（可选）：社交 HTTP 任务

好友赠礼/邀请/召回是现成 HTTP 接口（`joyorder.war`/`receiveGift.war`/`recall.war`）。
需要的话按 `endpoints.json` 填模板，用 `python main.py` 跑。核心游戏任务（签到/领奖/
抽奖）仍走 socket，要做的话得继续逆向 socket 协议（见"下一步"）。

## SWF 逆向工具链

协议还原不再依赖抓包猜测，可以直接从 SWF 字节码读出来。四个工具都建立在
`abcparse.py`（ABC 结构解析）+ `disasm.py`（AVM2 反汇编）之上：

```bash
# 还原全套协议：opcode 表 + .proto + schema.json
python tools/extract_proto.py RedWar_xxx.swf out

# 按类名反汇编，看成员和方法体
python tools/dump_class.py RedWar_xxx.swf Transport
python tools/dump_class.py RedWar_xxx.swf _-0ly --only encrypt,init

# 全库交叉引用：哪里引用了某个字符串
python tools/xref.py RedWar_xxx.swf --string rc4
```

对 `RedWar_2026073102.swf` 的实测规模：**4482 个类、62967 个方法、74216 条常量池
字符串**，完整解析约 2.3 秒（带磁盘缓存，二次加载秒开）。

> SWF 被 secureSWF 混淆（类名变成 `_-4I4` 这种），但混淆器动不了**字符串字面量**和
> **数值常量** —— 协议还原正是靠这两点：protobuf-as3 的报错模板
> `Bad data format: <消息名>.<字段名> cannot be set twice.` 泄露了全部消息名和字段名，
> `toString()` 里的 `'rc4'` 字面量坐实了加密算法。方法论见
> [docs/protocol-reverse-engineering.md](docs/protocol-reverse-engineering.md)，
> 完整过程见 [docs/加密与协议还原.md](docs/加密与协议还原.md)。

## 下一步 / 说明

- **仅保活**：只需第 1~3 步。这是当前主目标，已实盘验证可用。
- **每日任务**：现在有了 873 消息的 schema（其中 `Rce*` 客户端请求 232 个、
  `Rse*` 服务器响应 295 个），签到/领奖/抽奖对应哪个消息、字段怎么填都能查表，
  比早期靠抓包猜的路子好走得多。
- 会封号吗？行为等同你自己挂机在线，风险低，但毕竟是自动化，自行权衡。
- 游戏改版/换服务器：重跑 `extract_proto.py` 更新 schema，必要时重抓包更新
  `protocol.json`。

## 项目定位

维持自己账号的在线状态、把服务器发生的事及时解读并通知到你本人。观察、录制、
分析、告警 —— 到此为止；游戏内的人机验证由你本人处理。

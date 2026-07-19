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

## 架构真相（已由抓包确认）

坦克风暴是 **Flash 游戏**，主程序 `RedWar.swf` 通过 **裸 TCP socket** 连到
`tankstorm-proxy.sincetimes.com:8001`（备用 443）跟服务器通信，协议**未加密**
（config.xml: `encrypt=false`）。

因此：

- **游戏核心动作（含"在线状态"、签到、领奖、抽奖等）都走这条 socket**，不是 HTTP。
  用 HTTP 抓包工具（浏览器 HAR、Fiddler、mitmproxy）**抓不到**它们——那些只能看到
  资源下载（.swf/.dat/图片）和 QQ 空间平台埋点。
- **保持活跃 = 维持这条 socket + 定时心跳。** 本项目用纯 Python `socket` 复刻它。
- 少数**社交功能**（好友赠礼/邀请/召回）确实是 HTTP `.war` 接口，可用 HTTP 脚本做。

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
config.json                 开关：保持活跃、任务、通知
protocol.json               socket 协议细节（登录/心跳字节）—— 抓包后生成，缺它保活会提示
protocol.example.json       协议模板示例（说明结构）
endpoints.json              社交 HTTP 任务模板（好友赠礼等，可选）
tankstorm/
  qq_login.py               扫码登录 + cookie 持久化
  qzone.py                  提取 openid/openkey/uid/sid/server/port（socket 登录参数）
  protocol.py               按 protocol.json 拼/拆 socket 包
  socket_keepalive.py       保持在线守护进程（连 socket、心跳、掉线重连）
  engine.py / notify.py     HTTP 任务引擎 / 结果通知
tools/
  pcap_analyze.py           解析 Wireshark 抓包 → 定位登录握手+心跳（零依赖）
  har2endpoints.py          HAR → 社交 HTTP 任务模板
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

## 第 1 步：登录 + 自检（本机）

```bat
D:\miniconda\python.exe main.py --check
```

弹出 `qrcode.png`，手机 QQ 扫码。`--check` 会打印提取到的 socket 登录参数
（server/port/openid/openkey/uid/sid…）。全都有值就说明登录链路通了。

## socket 协议（已逆向完成）

`protocol.json` 已由 `wireshark.pcapng` 抓包逆向生成，无需再抓包。要点：

- 帧结构：`[2字节大端长度][2字节opcode][4字节seq(客户端恒为0)][protobuf]`
- 登录序列（4 步）：TGW 网关头 → `a,{uid},{secret}` → 认证包(041c, 固定密钥
  Redwarhq2018HoneyHoneyHoney) → build 包(041d)。`uid/secret` 每次由 qzone.py 从
  游戏页 FlashVars 现取。
- 心跳：`00 06 04 0e 00 00 00 00`（opcode 040e），每 10 秒一次，静态重放。

> 若某天游戏改版/换服务器导致保活失效，重抓一次包（Wireshark 过滤
> `ip.addr==193.112.238.18 && tcp.port==8001`），跑 `tools/pcap_analyze.py`
> 和 `tools/analyze_conn`（分析脚本），把新的登录序列/心跳更新进 `protocol.json` 即可。

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

cookie 失效时会退出并（若配了）推送提醒，届时 `python main.py --login` 重新扫码。

## 第 4 步（可选）：社交 HTTP 任务

好友赠礼/邀请/召回是现成 HTTP 接口（`joyorder.war`/`receiveGift.war`/`recall.war`）。
需要的话按 `endpoints.json` 填模板，用 `python main.py` 跑。核心游戏任务（签到/领奖/
抽奖）仍走 socket，要做的话得继续逆向 socket 协议（见"下一步"）。

## 下一步 / 说明

- **仅保活**：只需第 1~3 步。这是当前主目标。
- **每日任务也纯请求**：需要在抓包里进一步分析各操作对应的 socket 消息，逐个加到
  协议里，工程量更大——先把保活跑通，再逐步扩展。
- 会封号吗？行为等同你自己挂机在线，风险低，但毕竟是自动化，自行权衡。
- 游戏改版/换服务器：重抓一次包重新生成 `protocol.json` 即可。

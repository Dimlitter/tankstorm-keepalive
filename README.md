# 坦克风暴 自动脚本

QQ空间小游戏「坦克风暴」的**保持在线 + 每日任务**脚本。纯 Python 请求实现，
不开浏览器、不跑 Flash、不截图识图，可以扔到无界面的 Linux 服务器上常驻。

- **保活**：防止长时间无操作被踢下线导致基地被打（已实盘验证）
- **每日任务**：自动领签到、抽免费奖、做免费探索等（15 项参数已抓包实测）
- **事件告警**：被超级强攻时 PushPlus 推到微信，带进攻方名字

> 原理、协议逆向过程、加密怎么破的 → [docs/原理与逆向.md](docs/原理与逆向.md)

---

# 快速开始

## 1. 装环境

```bash
conda create -n tank python=3.12 -y && conda activate tank
pip install -r requirements.txt
```

Windows 本机可以直接用现成的 Python：`D:\miniconda\python.exe`

## 2. 登录

```bash
python main.py --login
```

手机 QQ 会收到**登录确认推送**，点一下即可，不用扫码。

> 收不到推送就退回扫码，此时二维码会存成 `qrcode.png`。**必须用另一台设备打开来扫**
> ——存到手机再用同一台手机相册扫，腾讯会拒（提示"限制本地扫码登录"）。

登录态约 1.5 天过期，但脚本会用长效凭据**自动续期**，正常情况几周不用管。

## 3. 看看有哪些任务

```bash
python main.py --list
```

```
每日任务（已启用，实发模式）
执行顺序 任务           opcode   消息                     上限    今日    参数     开关
   1   每日签到         04a4     RceDailySignIn         1     0     实测     ✅开
   6   英雄开采         0402     RceHeroVisit           3     0     实测     ✅开
  14   军事演习         04a7     RceWarGameOpt          3/60分冷却 0     实测     ✅开
  21   每日任务         043d     RceDailyTask           1     0     待确认      关
```

- **实测** = 参数来自真实抓包，可放心开
- **待确认** = 默认跳过，需先抓包核对（见下方「补充未实测任务」）

## 4. 跑一轮试试

```bash
python main.py --daily
```

只连一次、跑一轮任务、退出。**直接真实发送**——干跑只能打印字节数，给不出任何有效信息，已移除。

每个任务会先发**前置请求**（开面板/查询）再发动作 —— 真实客户端就是这个顺序，
少了这一步服务端不认，参数再对也不生效。发完等响应判成败：

```
―― 每日任务成果 ―― 成功 5，未成 1
  ✅ 每日签到      成功：ret=0
  ✅ 配件探索      成功：ret=12672 leftTime=2      ← ret 是这次拿到的石油
  ✅ 英雄开采      成功：hasCreditVisit=[2,1,1]     ← 三档各剩几次
  ❌ 特工派遣      未收到响应（可能次数已用完或请求被忽略）
```

同一份成果会**推送到你的 PushPlus**。剩余次数归零时会标注「今日到此为止」，
之后不再重试。

## 5. 挂上常驻

确认没问题后，保活 + 每日任务一起常驻：

```bash
nohup ./run_keepalive.sh >> logs/keepalive.log 2>&1 &
```

它会保持在线、定时重跑任务（冷却到了自动继续）、掉线自动重连、
登录过期自动续期或推送确认到你手机。

---

# 配置

只需要动 `config.json` 这几处：

## 任务开关

```json
"每日任务": {
    "启用": true,
    "任务": {
        "每日签到": true,
        "英雄开采": true,
        "月卡领取": false       // 没开月卡就关着
    }
}
```

## PushPlus 推送（可选，强烈建议）

被强攻、需要重新登录时会推到你微信。token 填在 **`config.local.json`**
（该文件已 gitignore，不会进仓库）：

```json
{ "通知": { "pushplus_token": "你的token" } }
```

测一下通不通：

```bash
python -c "import main,tankstorm.notify as n; n.send(main.load_config(),'坦克风暴','测试')"
```

---

# 常用命令

| 命令 | 作用 |
|---|---|
| `python main.py --login` | 登录（推送确认到手机） |
| `python main.py --check` | 验证登录态，打印 uid/sid/level 等 |
| `python main.py --list` | 列出所有任务及状态 |
| `python main.py --daily` | 跑一轮任务并打印成果（真实发送） |
| `python main.py --keepalive` | 保活常驻（含每日任务） |

---

# 安全机制

自动化最怕的是**悄悄把券和勋章刷掉**。四层防护：

| 层 | 作用 |
|---|---|
| **白名单** | 只允许发任务表里登记的 opcode，表外的连构造机会都没有 |
| **危险字段拦截** | 字段名带 `credit/cost/buy/item/card/ticket` 等的，值必须为 0，否则拒发 |
| **前置请求** | 复刻真实客户端的开面板/查询顺序，不凭空构造服务端不认的调用 |
| **事后读剩余次数** | 动作响应里的 `hasCreditVisit`/`leftTime` 归零就停，不再重试 |
| **每日上限 + 冷却** | 每天最多 N 次；可重复任务两次之间还要等冷却 |

> ⚠️ 游戏里那个"消耗勋章"确认框是**纯客户端 UI**，脚本发包不经过它，
> 服务器收到就扣。所以保护必须做在脚本这一侧 —— 这也是为什么参数**只能抓包实测，不能猜**。

「免费次数用完自动购买」**在代码层面禁止**，不提供开关。

---

# 补充未实测的任务

还有 6 项没抓到参数（七天乐、每日资源、战功排名、月卡、周任务、每日任务），
默认关闭。补齐方法：

1. **停掉服务器上的保活**（一个账号只能一个会话，否则互踢）
2. 浏览器打开游戏，记下 FlashVars 里的 `uid` / `sid` / `level` / `firstLogin`
3. Wireshark 开抓，**必须在游戏加载前就开始**（RC4 密钥流从连接建立起累积，
   中途缺字节后面全解不开）：
   ```
   ip.addr == 193.112.238.18 && tcp.port == 8001
   ```
4. 在游戏里把要补的任务**手动点一遍**，然后停止抓包存成 `.pcapng`
5. 拆流 → 解密 → 提参数：
   ```bash
   python tools/pcap_split.py 抓包.pcapng -o streams/
   python tools/redwar_rc4.py streams/<端口>/c2s.bin \
          --uid <uid> --sid <sid> --level <level> --first-login <firstLogin> --write
   python tools/capture_daily.py streams/<端口>/c2s.bin.decrypted/frames.jsonl
   ```
6. 把打印出的字段填进 `tankstorm/daily.py` 的 `TASKS`，`confidence` 改成 `"实测"`

> `pcap_split.py` 会自动按方向拆分并**检测空洞** —— 缺字节会明确报出来，
> 而不是产出一份解不开的流。

---

# 目录

```
main.py                 入口
config.json             配置（任务开关、保活、录制）
config.local.json       密钥（PushPlus token），已 gitignore
protocol.json           socket 协议（登录握手/心跳字节）
tankstorm/
  qq_login.py           登录、静默续期、推送登录
  qzone.py              提取 openid/openkey/uid/sid/level
  protocol.py           拼/拆 socket 包
  crypto.py             RC4 与密钥推导
  socket_keepalive.py   保活守护进程
  daily.py              每日任务引擎（白名单/危险字段/前置请求/冷却）
  sender.py             组帧 + 加密 + 发送
  proto_encode.py       protobuf 编码
  schema.py/.json       563 opcode、873 消息的对照
  recorder.py           消息录制 + 异常告警
  notify.py             PushPlus 推送
tools/
  pcap_split.py         pcapng 按方向拆流
  redwar_rc4.py         RC4 解密
  capture_daily.py      从解密结果提取任务参数
  extract_proto.py      从 SWF 还原协议（游戏更新后重跑）
  abcparse.py/disasm.py SWF 字节码解析
docs/
  原理与逆向.md          协议怎么来的、加密怎么破的
  redwar.proto          873 个消息定义
  opcodes.json          563 个 opcode 对照
```

---

# 说明

- **游戏更新后**：opcode 可能平移。重跑 `python tools/extract_proto.py <新swf> out`，
  对比 `out/opcodes.json` 与 `docs/opcodes.json`，有变化就替换并核对任务表。
- **会封号吗**：行为等同你自己挂机+点每日任务，风险低，但毕竟是自动化，自行权衡。
- **`logs/` 里有真凭据**：`c2s.bin`、`meta.json` 含明文 uid/secret/openkey/sid，
  发给别人前想清楚。已 gitignore。
- 本项目只做观察、解读、通知，以及复刻客户端已有的协议消息；
  不识别、不代答游戏内的人机验证。

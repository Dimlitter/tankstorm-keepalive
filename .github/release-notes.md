坦克风暴保活 + 每日任务脚本。**命令行工具，没有图形界面。**

## 下载哪个

| 系统 | 文件 |
|---|---|
| Windows（普通 64 位）| `tankstorm-windows-x64.zip` |
| Windows（骁龙等 ARM 机型）| `tankstorm-windows-arm64.zip` |
| macOS（M 系列芯片）| `tankstorm-macos-arm64.tar.gz` |
| macOS（Intel 芯片）| `tankstorm-macos-x64.tar.gz` |
| Linux x86_64 / ARM64 | `tankstorm-linux-x64.tar.gz` / `tankstorm-linux-arm64.tar.gz` |

解压后是一个可执行文件加 `config.json`、`endpoints.json`、`README.md`。
运行时产生的 `cookies.json`、`logs/` 都落在**可执行文件所在目录**，
整个目录拷走即可迁移。

`SHA256SUMS.txt` 是全部产物的校验值。

## 怎么跑

```
tankstorm --login      扫码登录（二维码会存成 qrcode.png，终端里也画一份）
tankstorm --check      验证登录态
tankstorm --daily      跑一轮每日任务
tankstorm --keepalive  常驻保活
```

macOS / Linux 下先 `chmod +x tankstorm`，命令写成 `./tankstorm --login`。

首次运行会把出厂 `config.json` 复制到程序目录（已存在则不覆盖），
按需修改后重跑即可。PushPlus token 之类的私密配置建议写在 `config.local.json`，
它会深度合并覆盖 `config.json`。

## 两件必须提前知道的事

**macOS 会拦。** 这些二进制没有 Apple 开发者签名，Gatekeeper 会提示
"无法验证开发者"。放行方法：

```
xattr -dr com.apple.quarantine ./tankstorm
```

**Windows 可能报毒。** PyInstaller 打出来的单文件程序被杀软误报是常见现象，
本项目又是游戏自动化工具，触发概率更高。介意的话可以直接用源码运行，
不需要任何编译步骤：

```
pip install -r requirements.txt
python main.py --login
```

源码运行和打包运行功能完全一致，只是后者省掉装 Python 的步骤。

## 安全设计

脚本对游戏账号的所有操作都遵循一条硬规则：**先查询、服务端说还有免费次数才做，
读不到次数就不做**。所有可能消耗勋章/券的字段被硬编码为 0，
并有 opcode 白名单和危险字段校验兜底。详见 README 的「安全机制」一节。

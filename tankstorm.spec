# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包规格。

用 .spec 而不是命令行 --add-data，是因为后者的路径分隔符在 Windows 上是 `;`、
在 macOS/Linux 上是 `:`，同一条命令没法跨平台复用。spec 里用元组，干净。

打包出来的是**单文件**可执行程序。随包带三份出厂数据（config / endpoints /
protocol）和协议 schema；首次运行时 main.py 会把前三份复制到程序目录，
让用户直接编辑（已存在则不覆盖）。运行期产生的 cookies.json、logs/、debug/
一律落在程序所在目录，不在临时解压目录里 —— 见 tankstorm/paths.py。

本地试打包：
    pyinstaller --clean --noconfirm tankstorm.spec
"""

import os

datas = [
    # schema.json 必须放回 tankstorm/ 子目录，schema.py 就是按包内相对路径找它的
    ("tankstorm/schema.json", "tankstorm"),
    # AGPL 要求分发二进制时随附许可证全文，不带就是违反自己的许可证
    ("LICENSE", "."),
    # 出厂默认配置，首次运行复制到程序目录
    ("config.json", "."),
    ("endpoints.json", "."),
    ("protocol.json", "."),
]
datas = [(src, dst) for src, dst in datas if os.path.exists(src)]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    # 这几个模块 daily.py 是用 __import__("tankstorm.xxx") 动态引的（避免和
    # daily 成环），PyInstaller 静态分析看不见字符串里的模块名，必须显式列出。
    hiddenimports=["tankstorm.arena", "tankstorm.country_war",
                   "tankstorm.guild", "tankstorm.shop"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 这些是打包环境里可能被牵连进来的重量级库，项目一个都不用
    excludes=["tkinter", "unittest", "pydoc", "doctest", "test",
              "numpy", "pandas", "matplotlib", "setuptools", "pip"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="tankstorm",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 压缩会显著提高被杀软误报的概率，不用
    runtime_tmpdir=None,
    console=True,       # 命令行工具，必须留控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,   # 交给 runner 的原生架构，不做交叉编译
    codesign_identity=None,
    entitlements_file=None,
)

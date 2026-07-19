@echo off
rem 本机 Windows 常驻保活。双击运行，掉线会自动重连。关窗口即停止。
chcp 65001 >nul
cd /d %~dp0
:loop
D:\miniconda\python.exe main.py --keepalive
echo 守护进程退出，10 秒后重启（Ctrl+C 结束）...
timeout /t 10 >nul
goto loop

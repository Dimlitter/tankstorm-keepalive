@echo off
rem 本机 Windows 计划任务用：任务计划程序 → 创建基本任务 → 每天 → 启动程序 → 选这个 bat
chcp 65001 >nul
cd /d %~dp0
D:\miniconda\python.exe main.py

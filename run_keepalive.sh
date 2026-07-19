#!/bin/bash
# Linux 服务器常驻保活。建议用 systemd 或 screen/tmux 挂后台：
#   nohup ./run_keepalive.sh >> logs/keepalive.log 2>&1 &
# socket_keepalive 内部已有掉线重连，这里的外层循环是防脚本本身异常退出。
cd "$(dirname "$0")"
# 改成你服务器上 conda 环境的 python 路径（conda env list 可查）：
PYTHON="${CONDA_PYTHON:-$HOME/miniconda3/envs/tank/bin/python}"
while true; do
    "$PYTHON" main.py --keepalive
    echo "守护进程退出，10 秒后重启..."
    sleep 10
done

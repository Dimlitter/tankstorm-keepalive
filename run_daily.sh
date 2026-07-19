#!/bin/bash
# Linux 服务器 cron 用。crontab -e 加一行（每天 08:30 跑）：
#   30 8 * * * /path/to/红警自动请求/run_daily.sh
# 注意服务器时区：TZ 不是 Asia/Shanghai 的话 cron 时间要换算，或先 timedatectl set-timezone Asia/Shanghai
cd "$(dirname "$0")"
# 按你服务器上 conda 的实际路径改这一行（conda env list 可查）：
PYTHON="${CONDA_PYTHON:-$HOME/miniconda3/envs/tank/bin/python}"
"$PYTHON" main.py >> logs/cron.log 2>&1

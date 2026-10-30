#!/bin/bash
set -euo pipefail

task_name="${1:-}"
shift || true

if [[ -z "$task_name" || "$#" -eq 0 ]]; then
    echo "[!] run_task.sh 用法: run_task.sh <ai|pc> <命令> [参数...]"
    exit 2
fi

if [[ "$task_name" == "pc" ]]; then
    pause_file="${PC_TASK_PAUSE_FILE:-/app/data/.pause_pc_login}"
    if [[ -e "$pause_file" ]]; then
        echo "[*] 检测到云电脑挂机暂停标记，跳过本次任务。"
        exit 0
    fi
fi

exec 9>/tmp/ctyun-auto-task.lock
if ! flock -n 9; then
    echo "[*] 另一个积分任务正在运行，跳过本次任务 ($task_name)。"
    exit 0
fi

exec "$@"

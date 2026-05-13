#!/bin/bash
set -e

# 1. 验证必须的基础环境变量
if [ -z "$APP_USER" ] || [ -z "$APP_PASSWORD" ]; then
    echo "[!] 错误: 必须提供 APP_USER 和 APP_PASSWORD 环境变量。"
    exit 1
fi

DEVICECODE_FILE="/app/data/.devicecode_${APP_USER}"
RESTART_AT_FILE="/tmp/ctyun_restart_at"

# 2. DEVICECODE 配置逻辑
if [ -n "$DEVICECODE" ]; then
    echo "$DEVICECODE" > "$DEVICECODE_FILE"
    echo "[*] 检测到传入的 DEVICECODE: $DEVICECODE"
elif [ -f "$DEVICECODE_FILE" ]; then
    export DEVICECODE=$(cat "$DEVICECODE_FILE")
    echo "[*] 读取到已保存的 DEVICECODE: $DEVICECODE"
else
    export DEVICECODE="web_$(cat /dev/urandom | tr -dc 'a-zA-Z0-9' | fold -w 32 | head -n 1)"
    echo "$DEVICECODE" > "$DEVICECODE_FILE"
    echo "[*] 首次启动，已生成并持久化 DEVICECODE: $DEVICECODE"
fi

# 写入所有环境变量供 cron 进程读取（确保 Python 脚本能获取账号密码等）
env >> /etc/environment

# 3. 动态生成 Cron 配置
CRON_LOGIN_EXPR="${CRON_LOGIN:-0 3,20 * * *}"
CRON_PC_EXPR="${CRON_PC:-0 4,6 * * *}"

echo "$CRON_LOGIN_EXPR root /usr/bin/python3 /app/login_script.py > /proc/1/fd/1 2>&1" > /etc/cron.d/ctyun-cron
echo "$CRON_PC_EXPR root /usr/bin/python3 /app/pc_login.py > /proc/1/fd/1 2>&1" >> /etc/cron.d/ctyun-cron
chmod 0644 /etc/cron.d/ctyun-cron
crontab /etc/cron.d/ctyun-cron

service cron start
echo "[*] Cron 定时服务已启动。"
echo "    - AI对话任务: $CRON_LOGIN_EXPR"
echo "    - 云电脑挂机: $CRON_PC_EXPR"

# 4. 首次运行自动触发机制 (相当于原 deploy.sh 首次脱离时的动作)
if [ "${INIT_RUN:-false}" == "true" ]; then
    echo "[*] 检测到 INIT_RUN=true，开始执行初始积分与挂机任务..."
    python3 /app/login_script.py || true
    nohup env PYTHONUNBUFFERED=1 python3 -u /app/pc_login.py > /app/data/pc_login_once.log 2>&1 &
fi

set +e
echo "[*] 启动进程守护模式..."

should_restart_ctyun_now() {
    if [ ! -f "$RESTART_AT_FILE" ]; then
        return 1
    fi

    local restart_at
    restart_at=$(tr -d '[:space:]' < "$RESTART_AT_FILE" 2>/dev/null)
    if ! [[ "$restart_at" =~ ^[0-9]+$ ]]; then
        echo "[!] 检测到无效的重启计划文件，已忽略并清理。"
        rm -f "$RESTART_AT_FILE"
        return 1
    fi

    local now
    now=$(date +%s)
    [ "$now" -ge "$restart_at" ]
}

run_ctyun_with_watch() {
    local duration="$1"
    local scheduled_restart=0

    timeout --foreground "$duration" dotnet CtYun.dll &
    local timeout_pid=$!

    while kill -0 "$timeout_pid" 2>/dev/null; do
        if should_restart_ctyun_now; then
            echo "[*] 检测到兑换成功后的重启计划已到时，准备重启 CtYun.dll。"
            scheduled_restart=1
            rm -f "$RESTART_AT_FILE"
            kill "$timeout_pid" 2>/dev/null || true
            sleep 1
            pkill -f "dotnet CtYun.dll" 2>/dev/null || true
            break
        fi
        sleep 2
    done

    wait "$timeout_pid"
    local exit_code=$?
    if [ "$scheduled_restart" -eq 1 ]; then
        return 200
    fi
    return "$exit_code"
}

# 开启无限循环，接管程序的生命周期
while true; do
    echo "======================================================"
    echo "[*] 启动 CtYun.dll..."
    run_ctyun_with_watch 2m
    sleep 10
    run_ctyun_with_watch 24h

    # 获取上方进程退出时的状态码
    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 200 ]; then
        echo "[*] 已按兑换计划完成重启。"
    elif [ $EXIT_CODE -eq 124 ]; then
        echo "[!] 触发定时机制：程序已连续运行 24 小时，执行强制重启。"
    else
        echo "[!] CtYun.dll 进程已退出 (退出码: $EXIT_CODE)。可能正在等待开机或发生了异常。"
    fi

    echo "[*] 容器挂起中，将在 2 分钟 (120秒) 后重新启动程序，请等待..."
    sleep 120
done

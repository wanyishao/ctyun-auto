#!/bin/bash
set -u
umask 077

if [ -z "${APP_USER:-}" ] || [ -z "${APP_PASSWORD:-}" ]; then
    echo "[!] 错误: 必须提供 APP_USER 和 APP_PASSWORD 环境变量。"
    exit 1
fi

DEVICECODE_FILE="/app/data/.devicecode_${APP_USER}"
RESTART_AT_FILE="/tmp/ctyun_restart_at"
PC_TASK_ACTIVE_FILE="${PC_TASK_ACTIVE_FILE:-/tmp/ctyun_pc_task_active}"
PC_DLL_STOPPED_FILE="${PC_DLL_STOPPED_FILE:-/tmp/ctyun_dll_stopped}"
PC_DLL_UNSAFE_FILE="${PC_DLL_UNSAFE_FILE:-/app/data/.ctyun_dll_unsafe}"
PC_TASK_PAUSE_FILE="${PC_TASK_PAUSE_FILE:-/app/data/.pause_pc_login}"
DLL_SUPERVISOR_PID=""
DLL_PROCESS_GROUP=""
DLL_GROUP_START=""
SHUTDOWN_REQUESTED=0

if [ ! -r /app/task_guard.sh ]; then
    echo "[!] 缺少任务安全检查文件 /app/task_guard.sh，拒绝启动守护流程。"
    exit 1
fi
# shellcheck source=task_guard.sh
source /app/task_guard.sh

if [ -n "${DEVICECODE:-}" ]; then
    printf '%s\n' "$DEVICECODE" > "$DEVICECODE_FILE"
    echo "[*] 检测到传入的 DEVICECODE，已保存。"
elif [ -f "$DEVICECODE_FILE" ]; then
    DEVICECODE=$(cat "$DEVICECODE_FILE")
    export DEVICECODE
    echo "[*] 已读取保存的 DEVICECODE。"
else
    DEVICECODE="web_$(cat /dev/urandom | tr -dc 'a-zA-Z0-9' | fold -w 32 | head -n 1)"
    printf '%s\n' "$DEVICECODE" > "$DEVICECODE_FILE"
    export DEVICECODE
    echo "[*] 首次启动，已生成并持久化 DEVICECODE。"
fi

env > /etc/environment
chmod 0600 /etc/environment

CRON_LOGIN_EXPR="${CRON_LOGIN:-0 3,20 * * *}"
CRON_PC_EXPR="${CRON_PC:-0 4,6 * * *}"
echo "$CRON_LOGIN_EXPR root /app/run_task.sh ai /usr/bin/python3 /app/login_script.py > /proc/1/fd/1 2>&1" > /etc/cron.d/ctyun-cron
echo "$CRON_PC_EXPR root PC_AUTO_RETRY=1 /app/run_task.sh pc /usr/bin/python3 -u /app/pc_login.py > /proc/1/fd/1 2>&1" >> /etc/cron.d/ctyun-cron
chmod 0644 /etc/cron.d/ctyun-cron
service cron start
echo "[*] Cron 定时服务已启动。"
echo "    - AI对话任务: $CRON_LOGIN_EXPR"
echo "    - 云电脑挂机: $CRON_PC_EXPR"

should_restart_ctyun_now() {
    [ -f "$RESTART_AT_FILE" ] || return 1
    local restart_at
    restart_at=$(tr -d '[:space:]' < "$RESTART_AT_FILE" 2>/dev/null)
    if ! [[ "$restart_at" =~ ^[0-9]+$ ]]; then
        echo "[!] 检测到无效的重启计划文件，已忽略并清理。"
        rm -f "$RESTART_AT_FILE"
        return 1
    fi
    [ "$(date +%s)" -ge "$restart_at" ]
}

pc_task_pid() {
    pc_task_resolve_active "$PC_TASK_ACTIVE_FILE" "$PC_DLL_STOPPED_FILE"
}

pc_task_is_active() {
    pc_task_pid >/dev/null
}

pc_user_pause_requested() {
    [ -e "$PC_TASK_PAUSE_FILE" ]
}

dll_group_has_live_process() {
    [ -n "$DLL_PROCESS_GROUP" ] || return 1
    [ "$(pc_task_resources_status "$DLL_PROCESS_GROUP")" = live ]
}

set_dll_unsafe() {
    local reason="$1"
    if ! pc_write_dll_unsafe "$PC_DLL_UNSAFE_FILE" "$reason" \
        "${DLL_PROCESS_GROUP:-unknown}" "${DLL_SUPERVISOR_PID:-unknown}"; then
        echo "[!] 无法写入 DLL_UNSAFE 持久标记；守护进程必须立即退出。"
        SHUTDOWN_REQUESTED=1
        return 1
    fi
    echo "[!] 已建立持久 DLL_UNSAFE 标记：$reason"
}

stop_dll_group() {
    if [ -z "$DLL_PROCESS_GROUP" ]; then
        if [ -n "$DLL_SUPERVISOR_PID" ]; then
            set_dll_unsafe "CtYun.dll 监督进程存在但进程组 ID 缺失"
            return 1
        fi
        return 0
    fi
    if [[ ! "$DLL_PROCESS_GROUP" =~ ^[0-9]+$ ]]; then
        set_dll_unsafe "CtYun.dll 进程组 ID 无效"
        return 1
    fi
    echo "[*] 正在停止 CtYun.dll 进程组 ($DLL_PROCESS_GROUP)。"
    local status
    status=$(pc_task_resources_status "$DLL_PROCESS_GROUP")
    if [ "$status" = unknown ]; then
        set_dll_unsafe "无法读取 CtYun.dll 进程组状态"
        return 1
    fi
    if [ "$status" = gone ]; then
        if [ -n "$DLL_SUPERVISOR_PID" ]; then wait "$DLL_SUPERVISOR_PID" 2>/dev/null || true; fi
        DLL_SUPERVISOR_PID=""
        DLL_PROCESS_GROUP=""
        DLL_GROUP_START=""
        return 0
    fi
    if [ -z "$DLL_SUPERVISOR_PID" ] || ! pc_task_pid_matches_start "$DLL_SUPERVISOR_PID" "$DLL_GROUP_START"; then
        set_dll_unsafe "CtYun.dll 进程组仍有进程，但无法确认原监督进程身份"
        return 1
    fi
    kill -TERM -- "-$DLL_PROCESS_GROUP" 2>/dev/null || true
    local remaining=30
    while [ "$remaining" -gt 0 ]; do
        status=$(pc_task_resources_status "$DLL_PROCESS_GROUP")
        [ "$status" = gone ] && break
        if [ "$status" = unknown ]; then
            set_dll_unsafe "停止 CtYun.dll 时无法确认进程组状态"
            return 1
        fi
        sleep 0.5
        remaining=$((remaining - 1))
    done
    status=$(pc_task_resources_status "$DLL_PROCESS_GROUP")
    if [ "$status" = live ]; then
        echo "[!] CtYun.dll 未及时退出，发送 KILL 并等待进程组清理。"
        kill -KILL -- "-$DLL_PROCESS_GROUP" 2>/dev/null || true
        remaining=20
        while [ "$remaining" -gt 0 ]; do
            status=$(pc_task_resources_status "$DLL_PROCESS_GROUP")
            [ "$status" = gone ] && break
            if [ "$status" = unknown ]; then
                set_dll_unsafe "KILL CtYun.dll 后无法确认进程组状态"
                return 1
            fi
            sleep 0.5
            remaining=$((remaining - 1))
        done
    fi
    status=$(pc_task_resources_status "$DLL_PROCESS_GROUP")
    if [ "$status" != gone ]; then
        set_dll_unsafe "CtYun.dll 进程组停止后仍未确认完全退出 ($status)"
        return 1
    fi
    if [ -n "$DLL_SUPERVISOR_PID" ]; then
        wait "$DLL_SUPERVISOR_PID" 2>/dev/null || true
    fi
    DLL_SUPERVISOR_PID=""
    DLL_PROCESS_GROUP=""
    DLL_GROUP_START=""
    return 0
}

acknowledge_pc_task() {
    local task_pid="$1" state owner_pid owner_start task_pgid task_start
    state=$(pc_task_field "$PC_TASK_ACTIVE_FILE" state 2>/dev/null || true)
    owner_pid=$(pc_task_field "$PC_TASK_ACTIVE_FILE" owner_pid 2>/dev/null || true)
    owner_start=$(pc_task_field "$PC_TASK_ACTIVE_FILE" owner_start 2>/dev/null || true)
    task_pgid=$(pc_task_field "$PC_TASK_ACTIVE_FILE" task_pgid 2>/dev/null || true)
    task_start=$(pc_task_field "$PC_TASK_ACTIVE_FILE" task_start 2>/dev/null || true)
    if ! pc_dll_launch_allowed "$PC_DLL_UNSAFE_FILE"; then
        echo "[!] DLL_UNSAFE 标记存在；拒绝网页连接授权。"
        return 1
    fi
    if [[ "$state" != running || "$task_pid" != "$owner_pid" \
        || ! "$owner_pid" =~ ^[0-9]+$ || ! "$task_pgid" =~ ^[0-9]+$ \
        || ! "$task_start" =~ ^[0-9]+$ ]] || ! pc_task_pid_matches_start "$owner_pid" "$owner_start"; then
        echo "[!] 任务或 DLL 进程组状态无法确认；拒绝授权网页连接。"
        return 1
    fi
    if [ -n "$DLL_PROCESS_GROUP" ] && ! stop_dll_group; then
        return 1
    fi
    if [ -e "$PC_DLL_UNSAFE_FILE" ]; then
        echo "[!] DLL_UNSAFE 标记存在；拒绝网页连接授权。"
        return 1
    fi
    state=$(pc_task_field "$PC_TASK_ACTIVE_FILE" state 2>/dev/null || true)
    owner_pid=$(pc_task_field "$PC_TASK_ACTIVE_FILE" owner_pid 2>/dev/null || true)
    owner_start=$(pc_task_field "$PC_TASK_ACTIVE_FILE" owner_start 2>/dev/null || true)
    task_pgid=$(pc_task_field "$PC_TASK_ACTIVE_FILE" task_pgid 2>/dev/null || true)
    task_start=$(pc_task_field "$PC_TASK_ACTIVE_FILE" task_start 2>/dev/null || true)
    if [[ "$state" != running || "$owner_pid" != "$task_pid" \
        || ! "$task_pgid" =~ ^[0-9]+$ || ! "$task_start" =~ ^[0-9]+$ ]] \
        || ! pc_task_pid_matches_start "$owner_pid" "$owner_start"; then
        echo "[!] 挂机任务在 DLL 停止期间改变状态；拒绝写入授权。"
        return 1
    fi
    if [ -n "$DLL_PROCESS_GROUP" ] || [ -n "$DLL_SUPERVISOR_PID" ]; then
        echo "[!] CtYun.dll 进程组身份仍存在；拒绝网页连接授权。"
        return 1
    fi
    local ack_temp="${PC_DLL_STOPPED_FILE}.tmp.$$"
    printf 'owner_pid=%s\ntask_pgid=%s\n' "$task_pid" "$task_pgid" > "$ack_temp"
    chmod 0600 "$ack_temp"
    mv -f "$ack_temp" "$PC_DLL_STOPPED_FILE"
    echo "[*] 已确认 CtYun.dll 进程组退出，授权挂机进程 $task_pid。"
}

wait_for_pc_task_to_finish() {
    local pause_notice_shown=0
    while :; do
        local task_pid=""
        task_pid=$(pc_task_pid 2>/dev/null || true)
        if [ -n "$task_pid" ]; then
            if [[ "$task_pid" == unsafe:* ]]; then
                echo "[!] 检测到未确认退出的挂机进程组 ($task_pid)，保持 DLL 停止；不会自动清理 unsafe 标记。"
                if [ -n "$DLL_PROCESS_GROUP" ] && ! stop_dll_group; then sleep 5; continue; fi
                sleep 5
                continue
            fi
            if [[ "$task_pid" == starting:* ]]; then
                if [ -n "$DLL_PROCESS_GROUP" ] && ! stop_dll_group; then sleep 1; continue; fi
                sleep 1
                continue
            fi
            if ! acknowledge_pc_task "$task_pid"; then
                if [ -e "$PC_DLL_UNSAFE_FILE" ]; then sleep 5; continue; fi
                sleep 1
                continue
            fi
            echo "[*] 网页挂机期间保持 CtYun.dll 纯协议保活停止。"
            while :; do
                local current_task_pid=""
                current_task_pid=$(pc_task_pid 2>/dev/null || true)
                [ "$current_task_pid" = "$task_pid" ] || break
                if pc_user_pause_requested && [ "$pause_notice_shown" -eq 0 ]; then
                    echo "[*] 检测到维护暂停标记，网页任务将安全退出。"
                    pause_notice_shown=1
                fi
                sleep 1
            done
            if [ -e "$PC_TASK_ACTIVE_FILE" ]; then
                if [[ "$current_task_pid" == unsafe:* ]] \
                    || [ "$(pc_task_field "$PC_TASK_ACTIVE_FILE" state 2>/dev/null || true)" = unsafe ]; then
                    echo "[!] 挂机父进程已消失但任务进程仍未确认退出；保持 DLL 停止并保留 unsafe 标记。"
                    rm -f "$PC_DLL_STOPPED_FILE"
                    return 1
                fi
                echo "[*] 挂机正在安全清理；等待 active 标记移除后再恢复 DLL。"
                sleep 1
                continue
            fi
            if [ -e "$PC_DLL_UNSAFE_FILE" ]; then
                echo "[!] DLL_UNSAFE 状态保持；禁止恢复 CtYun.dll。"
                return 1
            fi
            rm -f "$PC_DLL_STOPPED_FILE"
            echo "[*] 挂机进程组与浏览器已退出，允许恢复 CtYun.dll 纯协议保活。"
            return 0
        fi

        if pc_user_pause_requested; then
            if [ "$pause_notice_shown" -eq 0 ]; then
                echo "[*] 检测到维护暂停标记，保持 CtYun.dll 纯协议保活停止。"
                pause_notice_shown=1
            fi
            if [ -n "$DLL_PROCESS_GROUP" ] && ! stop_dll_group; then return 1; fi
            sleep 1
            continue
        fi
        return 0
    done
}

request_shutdown() {
    [ "$SHUTDOWN_REQUESTED" -eq 0 ] || return
    trap - TERM INT
    SHUTDOWN_REQUESTED=1
    echo "[*] 收到容器停止信号，停止 Cron、网页任务和 CtYun.dll。"
    service cron stop >/dev/null 2>&1 || true
    local task_pid=""
    task_pid=$(pc_task_pid 2>/dev/null || true)
    local task_owner="$task_pid"
    if [[ "$task_pid" == starting:* ]]; then task_owner="${task_pid#starting:}"; fi
    if [[ "$task_owner" =~ ^[0-9]+$ ]]; then
        kill -TERM "$task_owner" 2>/dev/null || true
        while [ -e "$PC_TASK_ACTIVE_FILE" ]; do
            local current_task_state=""
            current_task_state=$(pc_task_pid 2>/dev/null || true)
            [[ "$current_task_state" == unsafe:* ]] && break
            sleep 1
        done
    fi
    task_pid=$(pc_task_pid 2>/dev/null || true)
    if [[ "$task_pid" == unsafe:* ]]; then
        echo "[!] 挂机资源处于未确认状态；保留标记并保持 DLL 停止。"
    fi
    if [ -n "$DLL_PROCESS_GROUP" ] && ! stop_dll_group; then
        echo "[!] DLL 停止状态未确认，保留 DLL_UNSAFE 标记。"
    fi
    if [ ! -e "$PC_DLL_UNSAFE_FILE" ] && [ ! -e "$PC_TASK_ACTIVE_FILE" ]; then
        rm -f "$PC_DLL_STOPPED_FILE"
    fi
    exit 0
}
trap request_shutdown TERM INT

cleanup_on_exit() {
    if [ -n "$DLL_PROCESS_GROUP" ] || [ -n "$DLL_SUPERVISOR_PID" ]; then
        stop_dll_group >/dev/null 2>&1 || set_dll_unsafe "entrypoint 退出时 DLL 清理未确认"
    fi
    if [ ! -e "$PC_DLL_UNSAFE_FILE" ] && [ ! -e "$PC_TASK_ACTIVE_FILE" ]; then
        rm -f "$PC_DLL_STOPPED_FILE"
    fi
}
trap cleanup_on_exit EXIT

run_ctyun_with_watch() {
    local duration="$1"
    local scheduled_restart=0
    local scheduled_pause=0
    if ! pc_dll_launch_allowed "$PC_DLL_UNSAFE_FILE"; then
        echo "[!] DLL_UNSAFE 标记存在，拒绝启动 CtYun.dll。"
        return 203
    fi
    if pc_user_pause_requested; then return 202; fi
    if pc_task_is_active; then return 201; fi

    setsid timeout --signal=TERM --kill-after=10s "$duration" dotnet CtYun.dll &
    DLL_SUPERVISOR_PID=$!
    local attempts=0
    while [ "$attempts" -lt 20 ] && ! kill -0 "$DLL_SUPERVISOR_PID" 2>/dev/null; do
        sleep 0.1
        attempts=$((attempts + 1))
    done
    DLL_PROCESS_GROUP=$(ps -o pgid= -p "$DLL_SUPERVISOR_PID" 2>/dev/null | tr -d '[:space:]')
    DLL_GROUP_START=$(pc_task_proc_start_ticks "$DLL_SUPERVISOR_PID" 2>/dev/null || true)
    if [[ ! "$DLL_PROCESS_GROUP" =~ ^[0-9]+$ || ! "$DLL_GROUP_START" =~ ^[0-9]+$ ]]; then
        set_dll_unsafe "无法取得 CtYun.dll 进程组 ID"
        kill -TERM "$DLL_SUPERVISOR_PID" 2>/dev/null || true
        echo "[!] 无法取得 CtYun.dll 进程组 ID，已禁止后续 DLL 和网页连接。"
        return 203
    fi

    while kill -0 "$DLL_SUPERVISOR_PID" 2>/dev/null; do
        if pc_task_is_active; then
            echo "[*] 检测到网页挂机任务，停止并等待 CtYun.dll 完全退出。"
            scheduled_pause=1
            local task_pid
            task_pid=$(pc_task_pid 2>/dev/null || true)
            if [[ "$task_pid" =~ ^[0-9]+$ ]]; then
                if ! acknowledge_pc_task "$task_pid"; then
                    echo "[*] DLL 交接尚未完成；停止后由守护循环重新校验授权。"
                fi
            fi
            break
        fi
        if pc_user_pause_requested; then
            echo "[*] 检测到维护暂停标记，停止 CtYun.dll。"
            scheduled_pause=1
            break
        fi
        if should_restart_ctyun_now; then
            echo "[*] 检测到兑换成功后的重启计划已到时，准备重启 CtYun.dll。"
            scheduled_restart=1
            rm -f "$RESTART_AT_FILE"
            break
        fi
        sleep 1
    done

    if [ "$scheduled_pause" -eq 1 ] || [ "$scheduled_restart" -eq 1 ]; then
        if ! stop_dll_group; then return 203; fi
        [ "$scheduled_restart" -eq 1 ] && return 200
        pc_user_pause_requested && return 202
        return 201
    fi
    wait "$DLL_SUPERVISOR_PID"
    local exit_code=$?
    local dll_status
    dll_status=$(pc_task_resources_status "$DLL_PROCESS_GROUP")
    if [ "$dll_status" = gone ]; then
        DLL_SUPERVISOR_PID=""
        DLL_PROCESS_GROUP=""
        DLL_GROUP_START=""
    else
        if ! stop_dll_group; then return 203; fi
    fi
    return "$exit_code"
}

if [ "${INIT_RUN:-false}" = "true" ]; then
    echo "[*] 检测到 INIT_RUN=true，开始执行初始 AI 对话与云电脑挂机任务..."
    /app/run_task.sh ai /usr/bin/python3 /app/login_script.py || true
    nohup env PYTHONUNBUFFERED=1 PC_AUTO_RETRY=1 /app/run_task.sh pc /usr/bin/python3 -u /app/pc_login.py > /app/data/pc_login_once.log 2>&1 &
fi

echo "[*] 启动进程守护模式..."
while [ "$SHUTDOWN_REQUESTED" -eq 0 ]; do
    if ! pc_dll_launch_allowed "$PC_DLL_UNSAFE_FILE"; then
        echo "[!] DLL_UNSAFE 标记存在；不启动 DLL，也不授权网页连接。移除标记前守护进程保持安全等待。"
        sleep 30
        continue
    fi
    if pc_task_is_active || pc_user_pause_requested; then
        wait_for_pc_task_to_finish
        continue
    fi

    echo "======================================================"
    echo "[*] 启动 CtYun.dll..."
    run_ctyun_with_watch 2m
    EXIT_CODE=$?
    if [ "$EXIT_CODE" -eq 201 ] || [ "$EXIT_CODE" -eq 202 ]; then
        wait_for_pc_task_to_finish
        continue
    elif [ "$EXIT_CODE" -eq 203 ]; then
        echo "[!] DLL 状态未能安全确认，已进入 fail-closed 等待。"
        sleep 30
        continue
    fi

    sleep 10
    [ "$SHUTDOWN_REQUESTED" -eq 0 ] || break
    run_ctyun_with_watch 24h
    EXIT_CODE=$?

    if [ "$EXIT_CODE" -eq 201 ] || [ "$EXIT_CODE" -eq 202 ]; then
        wait_for_pc_task_to_finish
        continue
    elif [ "$EXIT_CODE" -eq 203 ]; then
        echo "[!] DLL 状态未能安全确认，已进入 fail-closed 等待。"
        sleep 30
        continue
    elif [ "$EXIT_CODE" -eq 200 ]; then
        echo "[*] 已按兑换计划完成重启。"
    elif [ "$EXIT_CODE" -eq 124 ]; then
        echo "[!] 触发定时机制：程序已连续运行 24 小时，执行强制重启。"
    else
        echo "[!] CtYun.dll 进程已退出 (退出码: $EXIT_CODE)。可能正在等待开机或发生了异常。"
    fi
    echo "[*] 容器挂起中，将在 2 分钟 (120秒) 后重新启动程序，请等待..."
    sleep 120
done

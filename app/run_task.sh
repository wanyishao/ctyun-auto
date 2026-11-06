#!/bin/bash
set -euo pipefail
umask 077

task_name="${1:-}"
if [[ -z "$task_name" || "$#" -lt 2 ]]; then
    echo "[!] run_task.sh 用法: run_task.sh <ai|pc> <命令> [参数...]"
    exit 2
fi
shift
if [[ "$task_name" != ai && "$task_name" != pc ]]; then
    echo "[!] 未知任务类型: $task_name"
    exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
guard_file="${PC_TASK_GUARD_FILE:-$script_dir/task_guard.sh}"
if [[ ! -r "$guard_file" ]]; then
    echo "[!] 缺少任务安全检查文件，拒绝启动。"
    exit 1
fi
# shellcheck source=task_guard.sh
source "$guard_file"

pause_file="${PC_TASK_PAUSE_FILE:-/app/data/.pause_pc_login}"
active_file="${PC_TASK_ACTIVE_FILE:-/tmp/ctyun_pc_task_active}"
dll_ack_file="${PC_DLL_STOPPED_FILE:-/tmp/ctyun_dll_stopped}"
dll_unsafe_file="${PC_DLL_UNSAFE_FILE:-/app/data/.ctyun_dll_unsafe}"
lock_file="${PC_TASK_LOCK_FILE:-/tmp/ctyun-auto-task.lock}"
state_lock_file="${PC_TASK_STATE_LOCK_FILE:-/tmp/ctyun_pc_task_state.lock}"
handoff_timeout="${PC_DLL_HANDOFF_TIMEOUT_SECONDS:-300}"
if [[ ! "$handoff_timeout" =~ ^[0-9]+$ ]] || (( handoff_timeout < 1 )); then
    handoff_timeout=300
elif (( handoff_timeout > 3600 )); then
    handoff_timeout=3600
fi

exec 9>"$lock_file"
if ! flock -n 9; then
    echo "[*] 另一个积分任务正在运行，跳过本次任务 ($task_name)。"
    exit 0
fi

if [[ "$task_name" == pc ]]; then
    if [[ -e "$pause_file" ]]; then
        echo "[*] 检测到云电脑挂机暂停标记，跳过本次任务。"
        exit 0
    fi

    if [[ -e "$active_file" ]]; then
        old_status="$(pc_task_resolve_active "$active_file" "$dll_ack_file" || true)"
        if [[ -e "$active_file" ]]; then
            if [[ "$old_status" == unsafe:* ]]; then
                echo "[!] 已有 unsafe 挂机标记；拒绝覆盖并停止新任务。"
                exit 1
            fi
            echo "[*] 已有网页挂机任务或其资源仍受 active 标记保护，跳过新任务 ($old_status)。"
            exit 0
        fi
        echo "[*] 已确认旧任务进程组和浏览器资源全部退出，清理安全的旧 active 标记。"
    fi

    # These API-only modes never open a browser or need a DLL handoff.
    if [[ "$#" -ge 2 && ( "$1" == */python* || "$1" == python* ) ]] \
        && { [[ " ${*} " == *" --precheck-complete "* ]] \
            || [[ " ${*} " == *" --redeem-completed-task "* ]]; }; then
        api_mode="precheck"
        [[ " ${*} " == *" --redeem-completed-task "* ]] && api_mode="redeem"
        set +e
        "$@"
        api_mode_status=$?
        set -e
        if [[ "$api_mode" == redeem && ( "$api_mode_status" -eq 10 || "$api_mode_status" -eq 11 ) ]]; then
            exit 0
        fi
        exit "$api_mode_status"
    fi

    # The read-only API precheck avoids stopping the DLL when today's task is
    # already complete. It never opens Chromium and does not grant connection
    # authorization; every real browser run still uses the handoff below.
    if [[ -e "$dll_unsafe_file" ]]; then
        echo "[!] 检测到持久 DLL_UNSAFE 状态，拒绝网页挂机授权。"
        exit 1
    fi
    if [[ "$#" -ge 2 && ( "$1" == */python* || "$1" == python* ) ]]; then
        task_args=("$@")
        pc_script_index=-1
        for index in "${!task_args[@]}"; do
            if [[ "${task_args[$index]}" == */pc_login.py || "${task_args[$index]}" == pc_login.py ]]; then
                pc_script_index="$index"
            fi
        done
        if [[ "$pc_script_index" -ge 0 ]] && [[ ! " ${task_args[*]} " =~ [[:space:]]--config-redeem[[:space:]] ]] \
            && [[ ! " ${task_args[*]} " =~ [[:space:]]--precheck-complete[[:space:]] ]] \
            && [[ ! " ${task_args[*]} " =~ [[:space:]]--redeem-completed-task[[:space:]] ]]; then
            # Only append the private precheck flag when the script is the final
            # argument; custom commands and explicit modes pass through intact.
            last_index=$(($# - 1))
            if [[ "$pc_script_index" -eq "$last_index" ]]; then
                set +e
                "$@" --precheck-complete
                precheck_status=$?
                set -e
                if [[ "$precheck_status" -eq 10 ]]; then
                    echo "[*] 只读预检查确认任务完成，启动独立的正式积分兑换阶段。"
                    set +e
                    "$@" --redeem-completed-task
                    redeem_status=$?
                    set -e
                    if [[ "$redeem_status" -eq 10 ]]; then
                        exit 0
                    elif [[ "$redeem_status" -eq 11 ]]; then
                        echo "[*] 兑换前进度已变化或无法确认，继续执行正常网页任务授权。"
                    else
                        echo "[!] 正式积分兑换阶段异常退出 ($redeem_status)，拒绝继续网页任务。"
                        exit "$redeem_status"
                    fi
                elif [[ "$precheck_status" -ne 11 && "$precheck_status" -ne 0 ]]; then
                    echo "[!] 只读完成状态预检查异常退出 ($precheck_status)，拒绝继续任务。"
                    exit "$precheck_status"
                else
                    echo "[*] 完成状态预检查未确认已完成，继续执行授权交接。"
                fi
            fi
        fi
    fi

    owner_pid="$$"
    owner_start="$(pc_task_proc_start_ticks "$owner_pid" || true)"
    if [[ ! "$owner_start" =~ ^[0-9]+$ ]]; then
        echo "[!] 无法确认任务包装进程身份，拒绝启动网页任务。"
        exit 1
    fi
    browser_root="$(pc_task_browser_root "$owner_pid" "$owner_start")"
    started_at="$(date +%s)"
    exec {state_lock_fd}>"$state_lock_file"
    if ! flock "$state_lock_fd"; then
        exec {state_lock_fd}>&-
        echo "[!] 无法锁定任务状态文件，拒绝创建 active 标记。"
        exit 1
    fi
    if [[ -e "$active_file" ]]; then
        flock -u "$state_lock_fd"
        exec {state_lock_fd}>&-
        echo "[!] 创建 active 标记前发现并发任务状态，拒绝覆盖。"
        exit 1
    fi
    pc_task_write_marker "$active_file" starting "$owner_pid" "$owner_start" 0 0 0 "$started_at"
    flock -u "$state_lock_fd"
    exec {state_lock_fd}>&-

    requested_signal=""
    trap 'requested_signal=TERM' TERM
    trap 'requested_signal=INT' INT

    gate_script=''
    read -r -d '' gate_script <<'GATE' || true
set -u
owner_pid="$1"; owner_start="$2"; active_file="$3"; ack_file="$4"; unsafe_file="$5"; guard_file="$6"; browser_root="$7"
shift 7
unset CTYUN_PC_TASK_AUTHORIZED
source "$guard_file" || exit 125
while :; do
    [[ -e "$unsafe_file" ]] && exit 120
    state="$(pc_task_field "$active_file" state 2>/dev/null || true)"
    marker_owner="$(pc_task_field "$active_file" owner_pid 2>/dev/null || true)"
    marker_owner_start="$(pc_task_field "$active_file" owner_start 2>/dev/null || true)"
    marker_pgid="$(pc_task_field "$active_file" task_pgid 2>/dev/null || true)"
    own_pgid="$(ps -o pgid= -p "$$" 2>/dev/null | tr -d '[:space:]')"
    ack_owner="$(pc_task_field "$ack_file" owner_pid 2>/dev/null || true)"
    ack_pgid="$(pc_task_field "$ack_file" task_pgid 2>/dev/null || true)"
    if [[ "$state" == running && "$marker_owner" == "$owner_pid" && "$marker_owner_start" == "$owner_start" \
        && "$marker_pgid" == "$own_pgid" && "$ack_owner" == "$owner_pid" && "$ack_pgid" == "$own_pgid" ]]; then
        umask 077
        mkdir -m 700 "$browser_root" 2>/dev/null || exit 124
        mkdir -m 700 "$browser_root/home" "$browser_root/profile" || exit 124
        export HOME="$browser_root/home"
        export CTYUN_PC_BROWSER_PROFILE_ROOT="$browser_root"
        export CTYUN_PC_BROWSER_PROFILE="$browser_root/profile"
        export CTYUN_PC_TASK_AUTHORIZED=1
        exec "$@"
    fi
    pc_task_pid_matches_start "$owner_pid" "$owner_start" || exit 121
    sleep 0.2
done
GATE

    setsid bash -c "$gate_script" ctyun-pc-task-gate \
        "$owner_pid" "$owner_start" "$active_file" "$dll_ack_file" "$dll_unsafe_file" "$guard_file" "$browser_root" "$@" <&0 9>&- &
    task_pid=$!
    task_pgid=""
    task_start=""
    for _ in $(seq 1 100); do
        task_pgid="$(ps -o pgid= -p "$task_pid" 2>/dev/null | tr -d '[:space:]')"
        task_start="$(pc_task_proc_start_ticks "$task_pid" 2>/dev/null || true)"
        if [[ "$task_pgid" =~ ^[0-9]+$ && "$task_start" =~ ^[0-9]+$ ]]; then
            break
        fi
        kill -0 "$task_pid" 2>/dev/null || break
        sleep 0.05
    done
    if [[ ! "$task_pgid" =~ ^[0-9]+$ || ! "$task_start" =~ ^[0-9]+$ ]]; then
        pc_task_write_marker "$active_file" unsafe "$owner_pid" "$owner_start" \
            "${task_pgid:-0}" "$task_pid" "${task_start:-0}" "$started_at" || true
        echo "[!] 无法持久化挂机进程组身份，保留 unsafe 标记并拒绝授权。"
        exit 1
    fi
    pc_task_write_marker "$active_file" running "$owner_pid" "$owner_start" \
        "$task_pgid" "$task_pid" "$task_start" "$started_at"

    mark_unsafe() {
        if ! pc_task_write_marker "$active_file" unsafe "$owner_pid" "$owner_start" \
            "$task_pgid" "$task_pid" "$task_start" "$started_at"; then
            pc_write_dll_unsafe "$dll_unsafe_file" "无法写入挂机 unsafe 标记" "$task_pgid" "$task_pid" || true
        fi
        rm -f "$dll_ack_file"
    }

    wait_for_session_gone() {
        local timeout_seconds="$1" status deadline
        deadline=$((SECONDS + timeout_seconds))
        while (( SECONDS < deadline )); do
            status="$(pc_task_resources_status "$task_pgid" "$browser_root")"
            [[ "$status" == gone ]] && return 0
            [[ "$status" == unknown ]] && return 1
            sleep 0.2
        done
        [[ "$(pc_task_resources_status "$task_pgid" "$browser_root")" == gone ]]
    }

    finish_task() {
        local graceful_seconds="$1" status
        if ! pc_task_write_marker "$active_file" stopping "$owner_pid" "$owner_start" \
            "$task_pgid" "$task_pid" "$task_start" "$started_at"; then
            pc_write_dll_unsafe "$dll_unsafe_file" "无法撤销挂机授权" "$task_pgid" "$task_pid" || true
        fi
        rm -f "$dll_ack_file"
        status="$(pc_task_resources_status "$task_pgid" "$browser_root")"
        if [[ "$status" == live ]]; then
            kill -TERM "$task_pid" 2>/dev/null || true
            if ! wait_for_session_gone "$graceful_seconds"; then
                status="$(pc_task_resources_status "$task_pgid" "$browser_root")"
                if [[ "$status" == live ]]; then
                    echo "[!] 挂机进程未响应 TERM，向任务进程组发送 TERM。"
                    kill -TERM -- "-$task_pgid" 2>/dev/null || true
                    pc_task_signal_browser_resources TERM "$browser_root"
                    wait_for_session_gone 5 || true
                    status="$(pc_task_resources_status "$task_pgid" "$browser_root")"
                fi
                if [[ "$status" == live ]]; then
                    echo "[!] 挂机进程组仍存活，发送 KILL 并再次确认。"
                    kill -KILL -- "-$task_pgid" 2>/dev/null || true
                    pc_task_signal_browser_resources KILL "$browser_root"
                    wait_for_session_gone 5 || true
                    status="$(pc_task_resources_status "$task_pgid" "$browser_root")"
                fi
            fi
        fi
        status="$(pc_task_resources_status "$task_pgid" "$browser_root")"
        if [[ "$status" == gone ]]; then
            rm -rf -- "$browser_root"
            rm -f "$active_file" "$dll_ack_file"
            return 0
        fi
        echo "[!] 无法确认挂机进程组、会话及专属 Chromium profile 资源完全退出 ($status)；保持 unsafe 状态，DLL 不得恢复。"
        mark_unsafe
        return 1
    }

    handoff_deadline=$((SECONDS + handoff_timeout))
    handoff_ok=0
    while (( SECONDS < handoff_deadline )); do
        if [[ -n "$requested_signal" ]]; then
            break
        fi
        if [[ -e "$dll_unsafe_file" ]] || [[ "$(pc_task_field "$active_file" state 2>/dev/null || true)" == unsafe ]]; then
            echo "[!] DLL 或任务状态进入 unsafe，取消网页任务。"
            break
        fi
        ack_owner="$(pc_task_field "$dll_ack_file" owner_pid 2>/dev/null || true)"
        ack_pgid="$(pc_task_field "$dll_ack_file" task_pgid 2>/dev/null || true)"
        if [[ "$ack_owner" == "$owner_pid" && "$ack_pgid" == "$task_pgid" ]]; then
            handoff_ok=1
            break
        fi
        if ! kill -0 "$task_pid" 2>/dev/null; then
            break
        fi
        if [[ -e "$pause_file" ]]; then
            echo "[*] 检测到维护暂停标记，取消尚未授权的网页任务。"
            break
        fi
        sleep 0.2
    done

    if [[ "$handoff_ok" -ne 1 ]]; then
        if [[ -n "$requested_signal" ]]; then
            echo "[*] 收到 $requested_signal，向挂机任务转发信号并等待清理。"
            finish_task 30 || true
            if [[ "$requested_signal" == INT ]]; then exit 130; else exit 143; fi
        fi
        echo "[!] 未在限定时间内完成 DLL 交接，取消网页任务。"
        finish_task 10 || true
        exit 1
    fi

    set +e
    wait "$task_pid"
    task_status=$?
    set -e
    if [[ -n "$requested_signal" ]]; then
        finish_task 30 || true
        if [[ "$requested_signal" == INT ]]; then exit 130; else exit 143; fi
    fi
    finish_task 10 || exit 1
    exit "$task_status"
fi

exec "$@"

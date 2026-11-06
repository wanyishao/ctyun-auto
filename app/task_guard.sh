#!/bin/bash

# Shared fail-closed helpers for the PC task handoff. Marker fields are plain
# key=value lines and are written atomically by run_task.sh / entrypoint.sh.
pc_task_field() {
    local file="$1" key="$2"
    [ -r "$file" ] || return 1
    awk -F= -v wanted="$key" '$1 == wanted { sub(/^[^=]*=/, ""); print; exit }' "$file"
}

pc_task_proc_start_ticks() {
    local pid="$1" stat_line
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    [ -r "/proc/$pid/stat" ] || return 1
    IFS= read -r stat_line < "/proc/$pid/stat" || return 1
    # Strip pid and parenthesized comm; starttime is field 20 in the remainder.
    stat_line="${stat_line##*) }"
    set -- $stat_line
    [ "$#" -ge 20 ] || return 1
    [[ "${20}" =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "${20}"
}

pc_task_pid_matches_start() {
    local pid="$1" expected_start="$2" actual_start state stat_line
    [[ "$pid" =~ ^[0-9]+$ && "$expected_start" =~ ^[0-9]+$ ]] || return 1
    actual_start=$(pc_task_proc_start_ticks "$pid") || return 1
    [ "$actual_start" = "$expected_start" ] || return 1
    IFS= read -r stat_line < "/proc/$pid/stat" || return 1
    stat_line="${stat_line##*) }"
    set -- $stat_line
    state="${1:-}"
    [[ "$state" != Z && "$state" != X ]]
}

pc_task_browser_root() {
    local owner_pid="$1" owner_start="$2"
    [[ "$owner_pid" =~ ^[0-9]+$ && "$owner_start" =~ ^[0-9]+$ ]] || return 1
    printf '/tmp/ctyun-pc-browser-%s-%s\n' "$owner_pid" "$owner_start"
}

# Returns live, gone, or unknown. A task's isolated session ID equals its PGID;
# this checks that group/session plus Chromium's unique profile and HOME paths,
# because crashpad helpers can create independent sessions.
pc_task_resources_status() {
    local pgid="$1" browser_root="${2:-}" table scoped_status
    local cmdline_file process_pid stat_line state matched arg
    local -a process_args
    [[ "$pgid" =~ ^[0-9]+$ && "$pgid" -gt 1 ]] || { printf 'unknown\n'; return; }
    if ! table=$(ps -eo pid=,pgid=,sid=,stat=,comm= 2>/dev/null); then
        printf 'unknown\n'
        return
    fi
    scoped_status=$(printf '%s\n' "$table" | awk -v wanted="$pgid" '
        ($2 == wanted || $3 == wanted) && $4 !~ /^[ZX]/ {
            live++
            if (tolower($5) ~ /^(chrome|chromium|chrome-sandbox)$/) browser++
        }
        END { if (live || browser) print "live"; else print "gone" }
    ')
    if [ "$scoped_status" = live ]; then
        printf 'live\n'
        return
    fi

    # Chromium crashpad handlers can create independent sessions. Their
    # command lines carry this task's unique profile or HOME path.
    if [[ "$browser_root" =~ ^/tmp/ctyun-pc-browser-[0-9]+-[0-9]+$ ]]; then
        for cmdline_file in /proc/[0-9]*/cmdline; do
            [ -r "$cmdline_file" ] || continue
            process_args=()
            mapfile -d '' -t process_args < "$cmdline_file" 2>/dev/null || continue
            matched=0
            for arg in "${process_args[@]}"; do
                if [[ "$arg" == *"$browser_root"* ]]; then
                    matched=1
                    break
                fi
            done
            [[ "$matched" -eq 1 ]] || continue
            process_pid="${cmdline_file#/proc/}"
            process_pid="${process_pid%/cmdline}"
            IFS= read -r stat_line < "/proc/$process_pid/stat" 2>/dev/null || continue
            state="${stat_line##*) }"
            state="${state%% *}"
            [[ "$state" == Z || "$state" == X ]] && continue
            printf 'live\n'
            return
        done
    fi
    printf 'gone\n'
}

pc_task_signal_browser_resources() {
    local signal_name="$1" browser_root="$2" cmdline_file process_pid stat_line state matched arg
    local -a process_args
    [[ "$signal_name" == TERM || "$signal_name" == KILL ]] || return 1
    [[ "$browser_root" =~ ^/tmp/ctyun-pc-browser-[0-9]+-[0-9]+$ ]] || return 1
    for cmdline_file in /proc/[0-9]*/cmdline; do
        [ -r "$cmdline_file" ] || continue
        process_args=()
        mapfile -d '' -t process_args < "$cmdline_file" 2>/dev/null || continue
        matched=0
        for arg in "${process_args[@]}"; do
            if [[ "$arg" == *"$browser_root"* ]]; then
                matched=1
                break
            fi
        done
        [[ "$matched" -eq 1 ]] || continue
        process_pid="${cmdline_file#/proc/}"
        process_pid="${process_pid%/cmdline}"
        IFS= read -r stat_line < "/proc/$process_pid/stat" 2>/dev/null || continue
        state="${stat_line##*) }"
        state="${state%% *}"
        [[ "$state" == Z || "$state" == X ]] && continue
        kill -"$signal_name" "$process_pid" 2>/dev/null || true
    done
}

pc_task_write_marker() {
    local file="$1" state="$2" owner="$3" owner_start="$4" pgid="$5" task_pid="$6" task_start="$7" started="$8"
    local temp="${file}.tmp.$$"
    umask 077
    {
        printf 'version=1\n'
        printf 'state=%s\n' "$state"
        printf 'owner_pid=%s\n' "$owner"
        printf 'owner_start=%s\n' "$owner_start"
        printf 'task_pgid=%s\n' "$pgid"
        printf 'task_pid=%s\n' "$task_pid"
        printf 'task_start=%s\n' "$task_start"
        printf 'started_at=%s\n' "$started"
    } > "$temp" || return 1
    chmod 0600 "$temp" || return 1
    mv -f "$temp" "$file"
}

_pc_task_resolve_active_locked() {
    local file="$1" ack_file="$2"
    [ -f "$file" ] || return 1
    local state owner_pid owner_start task_pgid task_pid task_start started resources
    state=$(pc_task_field "$file" state 2>/dev/null || true)
    owner_pid=$(pc_task_field "$file" owner_pid 2>/dev/null || true)
    owner_start=$(pc_task_field "$file" owner_start 2>/dev/null || true)
    task_pgid=$(pc_task_field "$file" task_pgid 2>/dev/null || true)
    task_pid=$(pc_task_field "$file" task_pid 2>/dev/null || true)
    task_start=$(pc_task_field "$file" task_start 2>/dev/null || true)
    started=$(pc_task_field "$file" started_at 2>/dev/null || true)

    if [ "$state" = unsafe ]; then
        rm -f "$ack_file"
        printf 'unsafe:%s\n' "${task_pgid:-unknown}"
        return 0
    fi
    if pc_task_pid_matches_start "$owner_pid" "$owner_start"; then
        if [ "$state" != running ] || [[ ! "$task_pgid" =~ ^[0-9]+$ ]] \
            || ! [[ "$task_pid" =~ ^[0-9]+$ && "$task_start" =~ ^[0-9]+$ ]]; then
            printf 'starting:%s\n' "$owner_pid"
        else
            printf '%s\n' "$owner_pid"
        fi
        return 0
    fi

    local browser_root
    browser_root=$(pc_task_browser_root "$owner_pid" "$owner_start" 2>/dev/null || true)
    resources=$(pc_task_resources_status "$task_pgid" "$browser_root")
    if [ "$resources" = gone ]; then
        # A dead wrapper is insufficient evidence on its own. Clear the marker
        # only after its stored process group/session and browser descendants
        # have disappeared.
        [ -z "$browser_root" ] || rm -rf -- "$browser_root"
        rm -f "$file" "$ack_file"
        return 1
    fi
    if [[ "$owner_pid" =~ ^[0-9]+$ && "$owner_start" =~ ^[0-9]+$ \
        && "$task_pgid" =~ ^[0-9]+$ && "$task_pid" =~ ^[0-9]+$ ]]; then
        pc_task_write_marker "$file" unsafe "$owner_pid" "$owner_start" \
            "$task_pgid" "$task_pid" "${task_start:-0}" "${started:-unknown}" || true
    fi
    rm -f "$ack_file"
    printf 'unsafe:%s\n' "${task_pgid:-unknown}"
    return 0
}

pc_task_resolve_active() {
    local file="$1" ack_file="$2" state_lock="${PC_TASK_STATE_LOCK_FILE:-/tmp/ctyun_pc_task_state.lock}"
    local lock_fd resolve_status=0
    if ! exec {lock_fd}>"$state_lock"; then
        printf 'unsafe:state-lock\n'
        return 0
    fi
    if ! flock "$lock_fd"; then
        exec {lock_fd}>&-
        printf 'unsafe:state-lock\n'
        return 0
    fi
    _pc_task_resolve_active_locked "$file" "$ack_file" || resolve_status=$?
    flock -u "$lock_fd"
    exec {lock_fd}>&-
    return "$resolve_status"
}

pc_dll_launch_allowed() {
    [ ! -e "$1" ]
}

pc_write_dll_unsafe() {
    local file="$1" reason="$2" pgid="$3" pid="$4"
    local directory temp
    directory="$(dirname "$file")"
    mkdir -p "$directory"
    temp="${file}.tmp.$$"
    umask 077
    printf 'timestamp=%s\nreason=%s\npgid=%s\npid=%s\n' \
        "$(date +%s)" "$reason" "$pgid" "$pid" > "$temp" || return 1
    chmod 0600 "$temp" || return 1
    mv -f "$temp" "$file"
}

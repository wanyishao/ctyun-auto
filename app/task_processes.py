"""Small /proc helpers for deciding whether a task session still owns processes."""


def process_in_task_scope(
    stat_line: str,
    task_pgid: int,
    task_sid: int,
    cmdline: str = "",
    browser_profile_root: str = "",
) -> bool:
    """Match the task PGID/SID or its unique Chromium profile/home marker."""
    try:
        fields = stat_line[stat_line.rfind(")") + 2 :].split()
        if len(fields) < 4 or fields[0] in {"Z", "X"}:
            return False
        process_pgid = int(fields[2])
        process_sid = int(fields[3])
    except (ValueError, IndexError):
        return False
    return (
        process_pgid == task_pgid
        or process_sid == task_sid
        or bool(browser_profile_root and browser_profile_root in cmdline)
    )

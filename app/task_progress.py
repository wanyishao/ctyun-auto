"""Pure helpers for the server-side one-hour task progress field."""

import datetime
from typing import Callable, Optional


def extract_current_progress(
    response_data: object, task_name: str = "使用1小时"
) -> Optional[int]:
    """Return None when the task or its progress field is absent/invalid."""
    if not isinstance(response_data, dict) or response_data.get("code") != 0:
        return None
    tasks = response_data.get("data")
    if not isinstance(tasks, list):
        return None
    for task in tasks:
        if not isinstance(task, dict) or task.get("taskDefName") != task_name:
            continue
        value = task.get("currentProgress")
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def log_progress_change(
    current: Optional[int],
    previous: Optional[int],
    *,
    target: int = 3600,
    timestamp: Optional[str] = None,
    output: Callable[[str], object] = print,
) -> Optional[int]:
    """Log each distinct server progress value and return it as the new baseline."""
    if current is None or current == previous:
        return previous
    logged_at = timestamp or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    output(f"[{logged_at}] 服务端 currentProgress: {current}/{target}")
    return current

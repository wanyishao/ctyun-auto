"""
云电脑首页登录脚本。
"""

import argparse
import calendar
import datetime
import hashlib
import json
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional

import requests
from DrissionPage import ChromiumOptions, ChromiumPage
from desktop_diagnostics import (
    CONNECTED,
    DesktopCdpMonitor,
    NETWORK_RECONNECT_DELAYS_SECONDS,
    NETWORK_RETRYABLE,
    PREEMPTED,
    UNKNOWN,
    classify_desktop_disconnect,
    confirm_canvas_loss,
    is_desktop_websocket_url,
    next_network_reconnect_delay,
    page_reports_reconnecting,
    sanitize_url,
)
from task_progress import extract_current_progress, log_progress_change
from task_processes import process_in_task_scope

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

LOGIN_URL = "https://pc.ctyun.cn/#/login"
DESKTOP_URL = "https://pc.ctyun.cn/#/desktop-list"
DESKTOP_DETAIL_URL_KEY = "/desktop?id="
REWARD_LIST_URL = (
    "https://desk.ctyun.cn/selforder/api/selforder/prod/get"
    "?prodId=17000000&prodCode=POINTS"
)
PLACE_ORDER_URL = "https://desk.ctyun.cn/selforder/api/selforder/paas/placeOrder"
POINTS_TASK_LIST_URL = (
    "https://desk.ctyun.cn/selforder/api/marketing/userPoints/getTaskList"
)
USER_POINTS_URL = (
    "https://desk.ctyun.cn/selforder/api/marketing/userPoints/getUserPoints"
)
TASK_PROGRESS_TARGET = 3600
DEFAULT_PC_MAX_RUNTIME_SECONDS = 7200
PC_CANVAS_LOSS_CONFIRMATIONS = 3
DESKTOP_INITIAL_RECONNECT_GRACE_SECONDS = 45
CTG_API_VERSION = "204000100"
CTG_WEB_DEVICE_TYPE = "60"
CTG_WEB_APP_MODEL = "2"
RESTART_AT_FILE = "/tmp/ctyun_restart_at"
MAX_DESKTOP_OPEN_ATTEMPTS = 1
MAX_DESKTOP_NETWORK_RECONNECTS = len(NETWORK_RECONNECT_DELAYS_SECONDS)


class AuthenticationExpiredError(RuntimeError):
    """任务接口拒绝了已经过期的登录态。"""


class DesktopConnectionFailure(RuntimeError):
    """网页桌面断线分类及诊断已完成，需要退出当前浏览器会话。"""

    def __init__(self, classification: str, diagnostic_path: Optional[str] = None):
        super().__init__(classification)
        self.classification = classification
        self.diagnostic_path = diagnostic_path


class PcMaxRuntimeExpired(RuntimeError):
    """The configured safe runtime for one PC task has elapsed."""


class PcTaskAuthorizationLost(RuntimeError):
    """The wrapper no longer owns a valid DLL-to-browser handoff."""


class BrowserCleanupFailure(RuntimeError):
    """Chromium did not confirm orderly shutdown; reconnect is unsafe."""


def get_pc_max_runtime_seconds() -> int:
    raw_value = os.getenv("PC_MAX_RUNTIME_SECONDS", str(DEFAULT_PC_MAX_RUNTIME_SECONDS))
    try:
        value = int(raw_value)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    print(
        "[!] PC_MAX_RUNTIME_SECONDS 必须是正整数；"
        f"使用默认值 {DEFAULT_PC_MAX_RUNTIME_SECONDS} 秒。"
    )
    return DEFAULT_PC_MAX_RUNTIME_SECONDS


def is_automatic_pc_trigger() -> bool:
    """Cron and INIT_RUN may retry ambiguous/preemption desktop failures."""
    return os.getenv("PC_AUTO_RETRY") == "1"


def pc_navigation_options() -> Dict[str, int]:
    """Keep scheduled page loads bounded so outer retries can make progress."""
    if is_automatic_pc_trigger():
        # DrissionPage retry counts are additional attempts; disable them here
        # so the outer scheduled retry policy owns all reconnection attempts.
        return {"timeout": 15, "retry": 0, "interval": 1}
    return {"timeout": 30}


def ensure_pc_runtime(deadline: Optional[float]) -> None:
    if (
        os.getenv("CTYUN_PC_TASK_AUTHORIZED") == "1"
        and not pc_task_authorization_valid()
    ):
        raise PcTaskAuthorizationLost(
            "挂机父任务或 DLL 交接授权已失效，正在关闭浏览器。"
        )
    if deadline is not None and time.monotonic() >= deadline:
        raise PcMaxRuntimeExpired("网页挂机达到 PC_MAX_RUNTIME_SECONDS，安全退出。")


def wait_for_task_child_processes(timeout_seconds: float = 10.0) -> None:
    """Wait for live processes in this task's PGID or session, like task_guard.sh."""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return
    task_pid = os.getpid()
    task_pgid = os.getpgrp()
    task_sid = os.getsid(task_pid)
    browser_profile_root = os.getenv("CTYUN_PC_BROWSER_PROFILE_ROOT", "")
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = []
        for entry in proc_root.iterdir():
            if not entry.name.isdigit() or int(entry.name) == task_pid:
                continue
            try:
                stat = (entry / "stat").read_text(encoding="utf-8")
                cmdline = ""
                if browser_profile_root:
                    cmdline = (entry / "cmdline").read_bytes().replace(
                        b"\0", b" "
                    ).decode("utf-8", errors="replace")
                if process_in_task_scope(
                    stat, task_pgid, task_sid, cmdline, browser_profile_root
                ):
                    remaining.append(int(entry.name))
            except (OSError, ValueError):
                continue
        if not remaining:
            return
        if time.monotonic() >= deadline:
            raise BrowserCleanupFailure(
                "Chromium 退出后仍有任务进程组/会话进程 "
                f"{remaining[:8]}；禁止进入下一轮连接。"
            )
        time.sleep(0.1)


def attach_desktop_cdp_monitor(page: ChromiumPage, monitor: DesktopCdpMonitor) -> bool:
    """Subscribe to read-only CDP Network events before desktop navigation."""
    try:
        driver = getattr(page, "driver", None)
        set_callback = getattr(driver, "set_callback", None)
        if not callable(set_callback):
            print("[!] DrissionPage 未提供原始 CDP 事件回调，继续使用页面状态诊断。")
            return False

        for method in monitor.EVENT_METHODS:
            def callback(_method=method, **params):
                monitor.record(_method, **params)

            set_callback(method, callback)
        page.run_cdp("Network.enable")
        print("[*] 已启用只读 CDP WebSocket/网络事件监听。")
        return True
    except Exception as error:
        print(f"[!] 无法启用 CDP 网络事件监听：{error}")
        return False


def get_pc_pause_file(running_in_docker: bool) -> str:
    """获取人工暂停云电脑任务的标记文件路径。"""
    return os.getenv(
        "PC_TASK_PAUSE_FILE",
        "/app/data/.pause_pc_login" if running_in_docker else "./.pause_pc_login",
    )


def pc_task_pause_requested(running_in_docker: bool) -> bool:
    return os.path.exists(get_pc_pause_file(running_in_docker))


def pc_task_authorization_valid() -> bool:
    """Require a live wrapper, matching task process group, and DLL handoff ack."""
    if os.getenv("CTYUN_PC_TASK_AUTHORIZED") != "1":
        return False
    owner_pid = str(os.getppid())
    active_file = os.getenv("PC_TASK_ACTIVE_FILE", "/tmp/ctyun_pc_task_active")
    dll_ack_file = os.getenv("PC_DLL_STOPPED_FILE", "/tmp/ctyun_dll_stopped")
    dll_unsafe_file = os.getenv("PC_DLL_UNSAFE_FILE", "/app/data/.ctyun_dll_unsafe")

    def read_fields(path: str) -> dict:
        values = {}
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        return values

    def process_start_ticks(pid: int) -> Optional[str]:
        try:
            stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            fields = stat_line[stat_line.rfind(")") + 2 :].split()
            return fields[19] if len(fields) >= 20 else None
        except (OSError, ValueError):
            return None

    try:
        if Path(dll_unsafe_file).exists():
            return False
        marker = read_fields(active_file)
        ack = read_fields(dll_ack_file)
        owner_start = marker.get("owner_start", "")
        parent_start = process_start_ticks(os.getppid())
        return bool(
            marker.get("state") == "running"
            and marker.get("owner_pid") == owner_pid
            and owner_start
            and owner_start == parent_start
            and marker.get("task_pgid", "").isdigit()
            and int(marker["task_pgid"]) == os.getpgrp()
            and ack.get("owner_pid") == owner_pid
            and ack.get("task_pgid") == marker.get("task_pgid")
        )
    except (OSError, ValueError, KeyError):
        return False


def init_browser_options(running_in_docker: bool) -> ChromiumOptions:
    """初始化 Chromium 启动参数。"""
    options = ChromiumOptions()
    options.set_argument("--no-sandbox")
    options.set_argument("--disable-gpu")
    options.set_argument("--disable-dev-shm-usage")
    options.set_argument("--window-size=1920,1080")
    browser_profile = os.getenv("CTYUN_PC_BROWSER_PROFILE")
    if browser_profile:
        profile_path = Path(browser_profile)
        profile_path.mkdir(parents=True, mode=0o700, exist_ok=True)
        options.set_user_data_path(str(profile_path))
    if running_in_docker:
        options.headless()
    return options


def get_auth_data_file(username: str, running_in_docker: bool) -> str:
    """构造账号专属 authData 文件路径。"""
    if running_in_docker:
        return f"/app/data/ctyun_authData_{username}_.json"
    return f"./ctyun_authData_{username}_.json"


def save_auth_data(page: ChromiumPage, file_path: str) -> None:
    """保存 localStorage.authData 到本地。"""
    try:
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        auth_data = read_auth_data(page)
        if not auth_data:
            print(f"[-] 未获取到 authData，跳过保存: {file_path}")
            return
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(auth_data, f, ensure_ascii=False, indent=4)
        print("[*] authData 已保存")
    except Exception as e:
        print(f"[!] 保存 authData 失败: {e}")


def load_auth_data_from_file(file_path: str) -> dict:
    """从本地读取 authData。"""
    if not os.path.exists(file_path):
        return {}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            auth_data = json.load(f)
        if isinstance(auth_data, dict):
            return auth_data
        return {}
    except Exception as e:
        print(f"[!] 读取 authData 文件失败: {e}")
        return {}


def get_device_code(username: str, running_in_docker: bool) -> str:
    """读取或输入 web_device_code。"""
    env_device = os.getenv("DEVICECODE")
    if env_device:
        return env_device.strip()

    if os.getenv("RUNNING_IN_DOCKER") == "true":
        file_path = f"/app/data/.devicecode_{username}"
    else:
        file_path = f"./.devicecode_{username}"
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            value = f.read().strip()
            if value:
                return value
    device_code = ""
    while not device_code:
        device_code = input("请输入 web_device_code: ").strip()
        if not device_code:
            print("[-] web_device_code 不能为空，请重新输入。")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(device_code)
    print(f"[*] web_device_code 已保存: {file_path}")
    return device_code


def set_web_device_code(page: ChromiumPage, device_code: str) -> None:
    """写入 localStorage.web_device_code。"""
    script = f"localStorage.setItem('web_device_code', {json.dumps(device_code)});"
    page.run_js(script)
    print("[*] 已写入 localStorage.web_device_code")


def read_page_device_code(page: ChromiumPage, fallback: str) -> str:
    """Use the device code retained by the authenticated browser session."""
    try:
        value = page.run_js("return localStorage.getItem('web_device_code') || ''; ")
    except Exception:
        return fallback
    if not isinstance(value, str) or not value.strip():
        return fallback
    value = value.strip()
    if value != fallback:
        print("[*] 网页登录后设备标识已变化；进度 API 将使用当前页面标识。")
    return value


def get_auth_expired_at_ms(hours: int = 72) -> int:
    """生成 authExpiredAt 时间戳（毫秒）。"""
    return int((time.time() + hours * 3600) * 1000)


def inject_auth_data_if_exists(page: ChromiumPage, auth_data_file: str) -> None:
    """若本地 authData 文件存在则注入 localStorage.authData。"""
    auth_data = load_auth_data_from_file(auth_data_file)
    if not auth_data:
        return
    auth_expired_at = str(get_auth_expired_at_ms(hours=72))
    page.run_js(
        f"localStorage.setItem('authExpiredAt', {json.dumps(auth_expired_at)});"
    )
    auth_data_text = json.dumps(auth_data, ensure_ascii=False, separators=(",", ":"))
    page.run_js(f"localStorage.setItem('authData', {json.dumps(auth_data_text)});")
    print("[*] 已注入本地 localStorage.authData。")


def inject_local_storage_session(
    page: ChromiumPage, device_code: str, auth_data_file: str
) -> None:
    """注入 web_device_code、authData"""
    set_web_device_code(page, device_code)
    inject_auth_data_if_exists(page, auth_data_file)


def first_available(page: ChromiumPage, selectors: list[str], timeout: float = 2):
    """按顺序查找首个可用元素。"""
    for selector in selectors:
        ele = page.ele(selector, timeout=timeout)
        if ele:
            return ele
    return None


def fill_credentials(
    page: ChromiumPage,
    username: str,
    password: str,
    selector_timeout: float = 60,
) -> None:
    """填写账号与密码。"""
    account_input = first_available(
        page,
        [
            'css:input[placeholder*="手机号"]',
            'css:input[placeholder*="账号"]',
            'css:input[type="text"]',
        ],
        timeout=selector_timeout,
    )
    password_input = first_available(
        page,
        [
            'css:input[placeholder*="密码"]',
            'css:input[type="password"]',
        ],
        timeout=min(10, selector_timeout),
    )

    if not account_input or not password_input:
        raise RuntimeError("未找到账号或密码输入框。")

    account_input.clear()
    account_input.input(username)
    password_input.clear()
    password_input.input(password)


def fill_captcha_if_possible(page: ChromiumPage) -> bool:
    """识别并填写图形验证码。"""
    captcha_img = page.ele("css:img.code-img", timeout=2)
    captcha_input = first_available(
        page,
        [
            'css:input[placeholder*="请输入验证码"]',
            'xpath://input[contains(@placeholder,"请输入验证码")]',
        ],
        timeout=1,
    )

    if not captcha_img or not captcha_input:
        return False

    try:
        image_bytes = captcha_img.get_screenshot(as_bytes=True)
        captcha_code = get_bytes_numeric_captcha(image_bytes).strip()
        print(f"[*] 图形验证码识别结果: {captcha_code}")
        if not captcha_code:
            return False
        time.sleep(1)
        captcha_input.clear()
        captcha_input.input(captcha_code)
        print(f"[*] 已填写图形验证码: {captcha_code}")
        time.sleep(2)
        return True
    except Exception as e:
        print(f"[!] 图形验证码识别失败: {e}")
        return False


def click_login_button(page: ChromiumPage) -> None:
    """点击登录按钮。"""
    login_btn = page.ele("css:button.btn-submit-pc", timeout=5)
    if not login_btn:
        raise RuntimeError("未找到登录按钮 button.btn-submit-pc。")
    login_btn.click()


def get_latest_toast(page: ChromiumPage, timeout: float = 4) -> str:
    """读取最新 toast 文本。"""
    end_time = time.time() + timeout
    while time.time() < end_time:
        toast_eles = page.eles("css:.el-message__content")
        text_list = [
            ele.text.strip()
            for ele in toast_eles
            if ele and ele.text and ele.text.strip()
        ]
        if text_list:
            return text_list[-1]
        time.sleep(0.2)
    return ""


def refresh_captcha_image(page: ChromiumPage) -> None:
    """点击验证码图片以刷新验证码。"""
    captcha_img = page.ele("css:img.code-img", timeout=1)
    if captcha_img:
        captcha_img.click()


def read_auth_data(page: ChromiumPage) -> dict:
    """读取 localStorage.authData。"""
    raw_data = page.run_js("return localStorage.getItem('authData');")
    if not raw_data:
        return {}
    if isinstance(raw_data, dict):
        return raw_data
    if isinstance(raw_data, str):
        try:
            return json.loads(raw_data)
        except json.JSONDecodeError:
            return {}
    return {}


def get_current_page_url(page: ChromiumPage) -> str:
    """Read the SPA URL without DrissionPage's implicit doc-loaded wait."""
    try:
        value = page.run_js("return window.location.href;", timeout=3)
        if isinstance(value, str) and value:
            return value
    except Exception:
        pass
    try:
        target_id = getattr(page, "_target_id", None)
        if target_id:
            result = page.run_cdp("Target.getTargetInfo", targetId=target_id)
            target_info = result.get("targetInfo", {}) if isinstance(result, dict) else {}
            value = target_info.get("url", "")
            if isinstance(value, str):
                return value
    except Exception:
        pass
    return str(getattr(page, "_url", "") or "")


def is_login_success(page: ChromiumPage) -> bool:
    """判断是否已登录成功。"""
    current_url = get_current_page_url(page)
    if DESKTOP_URL in current_url or "/desktop-list" in current_url:
        return True
    if "/login" in current_url:
        return False
    time.sleep(1)
    auth_data = read_auth_data(page)
    return bool(auth_data.get("logined"))


def wait_desktop_list_refresh_done(page: ChromiumPage, timeout: int = 60) -> None:
    """等待 desktop-list 刷新动画结束。"""
    end_time = time.time() + timeout
    seen_loading = False
    while time.time() < end_time:
        current_url = get_current_page_url(page)
        date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\r[*] {date} 页面状态检测中...", end="")
        if "/login" in current_url or DESKTOP_DETAIL_URL_KEY in current_url:
            return

        loading_ele = page.ele("css:.rotate-animtion", timeout=0.3)
        if loading_ele:
            seen_loading = True
            time.sleep(0.2)
            continue

        if seen_loading:
            # 连续未检测到刷新动画，视为加载完成
            time.sleep(0.8)
            if not page.ele("css:.rotate-animtion", timeout=0.2):
                return

        # 刷新动画可能很快结束，出现关键元素时也可提前退出
        if page.ele("css:div.empty-desc", timeout=0.2):
            return
        if page.ele("css:div.desktopcom-enter", timeout=0.2):
            return
        time.sleep(1)
    print("\r[*] desktop-list 刷新超时。")


def get_desktop_state(page: ChromiumPage) -> str:
    """识别 desktop-list 的状态。"""
    current_url = get_current_page_url(page)
    if is_desktop_detail_url(current_url):
        return "desktop_entered_auto"
    if "/login" in current_url:
        return "auth_expired"

    empty_desc = page.ele("css:div.empty-desc", timeout=0.5)
    if empty_desc:
        return "no_desktop"

    enter_buttons = page.eles("css:div.desktopcom-enter")
    has_cloud_pc = False
    has_cloud_phone = False
    for btn in enter_buttons:
        text = (btn.text or "").strip()
        if "进入AI云电脑" in text:
            has_cloud_pc = True
        if "进入AI云手机" in text:
            has_cloud_phone = True

    if has_cloud_pc:
        return "has_pc_button"
    if has_cloud_phone:
        return "only_phone"
    return "unknown"


def is_desktop_detail_url(url: str) -> bool:
    """兼容旧版 /desktop?id= 和新版 Vue hash 路由 #/desktop。"""
    if DESKTOP_DETAIL_URL_KEY in (url or ""):
        return True
    route = (url or "").partition("#")[2].split("?", 1)[0].rstrip("/")
    return route == "/desktop" or route.startswith("/desktop/")


def click_enter_ai_pc(page: ChromiumPage) -> bool:
    """点击“进入AI云电脑”按钮。"""
    enter_buttons = page.eles("css:div.desktopcom-enter")
    for btn in enter_buttons:
        text = (btn.text or "").strip()
        if "进入AI云电脑" in text:
            btn.click()
            return True
    return False


def click_visible_text(page: ChromiumPage, text: str) -> bool:
    """点击页面中可见且包含指定文字的最内层元素。"""
    script = f"""
        const label = {json.dumps(text, ensure_ascii=False)};
        const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();
        const candidates = Array.from(document.querySelectorAll('body *'));
        const target = candidates.find(element => {{
            if (!element.getClientRects().length) return false;
            const elementText = normalize(element.innerText || element.textContent);
            if (!elementText.includes(label)) return false;
            return !Array.from(element.children).some(child =>
                normalize(child.innerText || child.textContent).includes(label)
            );
        }});
        if (!target) return false;
        target.click();
        return true;
    """
    try:
        return bool(page.run_js(script))
    except Exception:
        return False


def click_control_center_icon(page: ChromiumPage) -> bool:
    """点击新版工具栏中仅有 title、没有可见文字的控制中心图标。"""
    try:
        return bool(
            page.run_js(
                """const element = Array.from(document.querySelectorAll('[title]'))
                    .find(item => item.getAttribute('title') === '控制中心'
                        && item.getClientRects().length);
                if (!element) return false;
                element.click();
                return true;"""
            )
        )
    except Exception:
        return False


def get_points_page_hints(page: ChromiumPage) -> str:
    """提取与连接状态和积分入口相关的可见页面文字。"""
    try:
        lines = page.run_js(
            """return (document.body ? document.body.innerText : '')
                .split('\\n')
                .map(line => line.trim())
                .filter(line => /积分|控制中心|连接|断开|重连|超时|退出|加载失败|网络异常/.test(line))
                .slice(0, 12);"""
        )
        if isinstance(lines, list) and lines:
            return " | ".join(str(line) for line in lines)
    except Exception:
        pass
    return "未发现连接状态或积分中心相关文字"


def get_desktop_page_text(page: ChromiumPage) -> str:
    """读取云电脑页面可见文字，用于判断连接状态。"""
    try:
        value = page.run_js(
            "return document.body ? document.body.innerText : '';", timeout=3
        )
        return str(value or "")
    except Exception:
        return ""


def get_desktop_canvas_state(page: ChromiumPage) -> dict:
    """读取远程桌面画布和工具栏的可见状态。"""
    try:
        value = page.run_js(
            """const canvas = document.querySelector('canvas.canvas.pixelated');
            const bounds = canvas ? canvas.getBoundingClientRect() : null;
            const titles = Array.from(document.querySelectorAll('[title]'))
                .map(element => element.getAttribute('title'));
            const toolbarCount = ['控制中心', '网络状态', '全屏']
                .filter(title => titles.includes(title)).length;
            const width = bounds ? Math.round(bounds.width) : 0;
            const height = bounds ? Math.round(bounds.height) : 0;
            return {
                present: !!canvas,
                width,
                height,
                toolbar_count: toolbarCount,
                connected: !!canvas && width >= 640 && height >= 360
                    && toolbarCount >= 2
            };""",
            timeout=3,
        )
        if isinstance(value, dict):
            return value
        return {"present": bool(value), "connected": bool(value)}
    except Exception as error:
        return {"present": False, "connected": False, "error": str(error)[:300]}


_diagnostic_cleanup_lock = threading.Lock()


def prune_desktop_diagnostics(
    directory: Path, max_files: int, max_age_days: int
) -> None:
    """Bound diagnostic storage after asynchronous screenshot writes finish."""
    expiration = time.time() - max_age_days * 86400
    with _diagnostic_cleanup_lock:
        diagnostic_files = []
        for path in directory.glob("desktop-disconnect-*"):
            if path.suffix not in {".json", ".png"} or not path.is_file():
                continue
            try:
                modified = path.stat().st_mtime
            except OSError:
                continue
            if modified < expiration:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            diagnostic_files.append((modified, path))

        diagnostic_files.sort(key=lambda item: item[0])
        excess = max(0, len(diagnostic_files) - max_files)
        for _, path in diagnostic_files[:excess]:
            try:
                path.unlink()
            except OSError:
                pass


def save_desktop_disconnect_diagnostic(
    page: ChromiumPage,
    monitor: Optional[DesktopCdpMonitor],
    classification: str,
    reason: str,
    running_in_docker: bool,
) -> Optional[str]:
    """Write a private diagnostic immediately; screenshot capture is best effort."""
    now = datetime.datetime.now().astimezone()
    file_stamp = now.strftime("%Y%m%dT%H%M%S_%f")
    directory = Path(
        "/app/data/desktop-diagnostics"
        if running_in_docker
        else "./data/desktop-diagnostics"
    )
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError as error:
        print(f"[!] 无法创建断线诊断目录：{error}")
        return None

    try:
        max_files = max(4, min(200, int(os.getenv("PC_DIAGNOSTIC_MAX_FILES", "40"))))
    except ValueError:
        max_files = 40
    try:
        max_age_days = max(1, min(90, int(os.getenv("PC_DIAGNOSTIC_MAX_AGE_DAYS", "7"))))
    except ValueError:
        max_age_days = 7
    prune_desktop_diagnostics(directory, max_files, max_age_days)

    try:
        page_url = sanitize_url(get_current_page_url(page))
    except Exception:
        page_url = "<unavailable>"
    try:
        page_text = get_desktop_page_text(page)
    except Exception:
        page_text = ""
    hints = get_points_page_hints(page)
    page_text = re.sub(r"(?<!\d)\d{11}(?!\d)", "[账号已隐藏]", page_text)
    hints = re.sub(r"(?<!\d)\d{11}(?!\d)", "[账号已隐藏]", hints)
    canvas_state = get_desktop_canvas_state(page)
    network_events = monitor.snapshot() if monitor else []
    screenshot_path = directory / f"desktop-disconnect-{file_stamp}.png"
    diagnostic_path = directory / f"desktop-disconnect-{file_stamp}.json"
    diagnostic = {
        "timestamp": now.isoformat(timespec="milliseconds"),
        "classification": classification,
        "reason": reason,
        "url": page_url,
        "page_hints": hints,
        "page_text_excerpt": page_text[:1500],
        "canvas": canvas_state,
        "network_events": network_events,
        "screenshot": str(screenshot_path),
        "screenshot_status": "best_effort_async",
    }
    try:
        descriptor = os.open(
            diagnostic_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(json.dumps(diagnostic, ensure_ascii=False, indent=2))
        os.chmod(diagnostic_path, 0o600)
        prune_desktop_diagnostics(directory, max_files, max_age_days)
    except OSError as error:
        print(f"[!] 无法写入断线诊断 JSON：{error}")
        return None

    def capture_screenshot() -> None:
        try:
            page.get_screenshot(
                path=str(directory),
                name=f"desktop-disconnect-{file_stamp}",
                full_page=True,
            )
            if screenshot_path.exists():
                os.chmod(screenshot_path, 0o600)
                if screenshot_path.stat().st_size > 4 * 1024 * 1024:
                    screenshot_path.unlink()
                    print("[!] 断线截图超过 4 MiB，已删除。")
            prune_desktop_diagnostics(directory, max_files, max_age_days)
        except Exception as error:
            print(f"[!] 后台断线截图未能完成（不影响连接释放）：{error}")

    threading.Thread(
        target=capture_screenshot,
        name="ctyun-disconnect-screenshot",
        daemon=True,
    ).start()
    print(f"[*] 断线诊断已保存：{diagnostic_path}")
    print(f"[*] 已异步请求断线截图：{screenshot_path}")
    return str(diagnostic_path)


def wait_desktop_opened(
    page: ChromiumPage,
    timeout: int = 270,
    pause_file: Optional[str] = None,
    monitor: Optional[DesktopCdpMonitor] = None,
    running_in_docker: bool = False,
    runtime_deadline: Optional[float] = None,
) -> str:
    """Wait for the canvas and classify a failed desktop connection."""
    remaining = max(0, runtime_deadline - time.monotonic()) if runtime_deadline else timeout
    end_time = time.time() + min(timeout, remaining)
    reconnect_deadline: Optional[float] = None
    while time.time() < end_time:
        ensure_pc_runtime(runtime_deadline)
        if pause_file and os.path.exists(pause_file):
            print("[*] 检测到人工暂停标记，停止等待桌面连接。")
            return UNKNOWN

        current_url = get_current_page_url(page)
        if "/login" in current_url:
            path = save_desktop_disconnect_diagnostic(
                page,
                monitor,
                UNKNOWN,
                "等待桌面画布期间页面被重定向到登录页",
                running_in_docker,
            )
            raise DesktopConnectionFailure(UNKNOWN, path)

        if is_desktop_detail_url(current_url):
            page_text = get_desktop_page_text(page)
            canvas_state = get_desktop_canvas_state(page)
            canvas_present = bool(canvas_state.get("connected"))
            if canvas_present:
                if monitor:
                    monitor.mark_canvas_connected()
                print("[*] 已检测到云电脑远程桌面画布。")
                return CONNECTED

            network_events = monitor.events_for_classification() if monitor else []
            classification = classify_desktop_disconnect(
                page_text=page_text,
                canvas_present=False,
                network_events=network_events,
            )
            # A conflict prompt or confirmed desktop-tunnel transport failure
            # is actionable immediately. Generic close events are handled as
            # UNKNOWN below so the website cannot keep reconnecting itself.
            if classification in {PREEMPTED, NETWORK_RETRYABLE}:
                reason = (
                    "页面出现明确的账号/会话冲突提示"
                    if classification == PREEMPTED
                    else "CDP 或页面提示确认了桌面连接网络故障"
                )
                path = save_desktop_disconnect_diagnostic(
                    page, monitor, classification, reason, running_in_docker
                )
                print(f"[!] 网页桌面断线分类：{classification}。")
                if classification == PREEMPTED:
                    if is_automatic_pc_trigger():
                        print("[!] 检测到客户端连接冲突，关闭当前网页后由自动任务有限重试。")
                    else:
                        print("[!] 检测到客户端连接冲突，立即退出，不再重连。")
                raise DesktopConnectionFailure(classification, path)

            if page_reports_reconnecting(page_text) and reconnect_deadline is None:
                reconnect_deadline = (
                    time.monotonic() + DESKTOP_INITIAL_RECONNECT_GRACE_SECONDS
                )
                print(
                    "[*] 网页正在自动恢复初始桌面连接；保留当前会话最多 "
                    f"{DESKTOP_INITIAL_RECONNECT_GRACE_SECONDS} 秒并持续检测画布。"
                )

            observed_generic_close = any(
                event.get("desktop_connection")
                and (
                    event.get("event") == "Network.webSocketClosed"
                    or (
                        event.get("event") == "Network.webSocketFrameReceived"
                        and event.get("opcode") == 8
                    )
                )
                for event in network_events
            )
            if (
                classification == UNKNOWN
                and observed_generic_close
                and reconnect_deadline is None
            ):
                reason = (
                    "当前桌面 WebSocket 已关闭但没有明确 close 原因或冲突提示；"
                    "按 UNKNOWN 停止网页自身自动重连"
                )
                path = save_desktop_disconnect_diagnostic(
                    page, monitor, UNKNOWN, reason, running_in_docker
                )
                if is_automatic_pc_trigger():
                    print("[!] 仅观察到通用 WebSocket 关闭，关闭当前网页后由自动任务有限重试。")
                else:
                    print("[!] 仅观察到通用 WebSocket 关闭，分类为 UNKNOWN，立即退出。")
                raise DesktopConnectionFailure(UNKNOWN, path)

            if reconnect_deadline is not None:
                grace_remaining = reconnect_deadline - time.monotonic()
                if grace_remaining > 0:
                    time.sleep(min(0.5, grace_remaining))
                    continue
                reason = (
                    "网页自动恢复初始桌面连接超过 "
                    f"{DESKTOP_INITIAL_RECONNECT_GRACE_SECONDS} 秒；"
                    "画布仍未出现，且没有明确冲突或桌面网络证据"
                )
                path = save_desktop_disconnect_diagnostic(
                    page, monitor, UNKNOWN, reason, running_in_docker
                )
                if is_automatic_pc_trigger():
                    print("[!] 初始桌面连接恢复等待超时，关闭当前网页后由自动任务有限重试。")
                else:
                    print("[!] 初始桌面连接恢复等待超时，分类为 UNKNOWN，保守退出。")
                raise DesktopConnectionFailure(UNKNOWN, path)
        time.sleep(min(0.5, max(0.05, end_time - time.time())))

    ensure_pc_runtime(runtime_deadline)
    print("[*] 等待云电脑连接完成超时。")
    page_text = get_desktop_page_text(page)
    classification = classify_desktop_disconnect(
        page_text=page_text,
        canvas_present=False,
        network_events=monitor.events_for_classification() if monitor else [],
    )
    reason = "连接等待超时；没有画布且没有可确认的桌面网络故障"
    if classification == NETWORK_RETRYABLE:
        reason = "CDP/页面提示确认桌面 DNS/TCP/socket/timeout 故障"
    elif classification == PREEMPTED:
        reason = "页面出现明确的账号/会话冲突提示"
    path = save_desktop_disconnect_diagnostic(
        page, monitor, classification, reason, running_in_docker
    )
    print(f"[!] 网页桌面断线分类：{classification}。")
    if classification == PREEMPTED:
        if is_automatic_pc_trigger():
            print("[!] 检测到客户端连接冲突，关闭当前网页后由自动任务有限重试。")
        else:
            print("[!] 检测到客户端连接冲突，立即退出，不再重连。")
    elif classification == UNKNOWN:
        if is_automatic_pc_trigger():
            print("[!] 画布未建立且没有明确故障证据，关闭当前网页后由自动任务有限重试。")
        else:
            print("[!] 画布未建立且没有明确故障证据，分类为 UNKNOWN，立即退出。")
    raise DesktopConnectionFailure(classification, path)


def has_connected_desktop_canvas(page: ChromiumPage) -> bool:
    """检查新版远程桌面画布和工具栏是否都已加载。"""
    return bool(get_desktop_canvas_state(page).get("connected"))


def open_points_center_and_print(
    page: ChromiumPage,
    timeout: int = 60,
    pause_file: Optional[str] = None,
    runtime_deadline: Optional[float] = None,
    entry_timeout: int = 120,
) -> Optional[int]:
    """打开积分中心并输出积分详情。"""
    try:
        if pause_file and os.path.exists(pause_file):
            print("[*] 检测到人工暂停标记，取消打开积分中心。")
            return None
        clicked = click_visible_text(page, "积分中心")
        if not clicked:
            # 工具栏可能在进入云电脑后延迟加载；最多等待 120 秒。
            end_time = time.time() + entry_timeout
            control_center_opened = False
            while time.time() < end_time:
                if pause_file and os.path.exists(pause_file):
                    print("[*] 检测到人工暂停标记，取消打开积分中心。")
                    return None
                if runtime_deadline is not None and time.monotonic() >= runtime_deadline:
                    return None
                if click_visible_text(page, "积分中心"):
                    clicked = True
                    break

                # 新版工具栏将积分中心收在“控制中心”面板内。
                if not control_center_opened and click_control_center_icon(page):
                    control_center_opened = True
                    end_time = time.time() + 15
                    continue
                time.sleep(0.5)

        if not clicked:
            print(
                "\r[-] 未找到积分中心入口。"
                f" 页面提示: {get_points_page_hints(page)}"
            )
            return None
        initial_wait = (
            min(5, max(0, runtime_deadline - time.monotonic()))
            if runtime_deadline
            else 5
        )
        time.sleep(initial_wait)
        end_time = time.time() + min(
            timeout,
            max(0, runtime_deadline - time.monotonic()) if runtime_deadline else timeout,
        )
        while time.time() < end_time:
            if pause_file and os.path.exists(pause_file):
                print("[*] 检测到人工暂停标记，停止等待积分中心。")
                return None
            if runtime_deadline is not None and time.monotonic() >= runtime_deadline:
                return None
            if page.ele('css:iframe[src*="points.html"]', timeout=0.5):
                break
            time.sleep(0.3)

        iframe_ele = page.ele('css:iframe[src*="points.html"]', timeout=1)
        if not iframe_ele:
            print("\r[-] 未找到积分中心 iframe。")
            return None

        frame = page.get_frame(iframe_ele)
        if not frame:
            print("\r[-] 无法切换到积分中心 iframe。")
            return None

        frame_wait = (
            min(5, max(0, runtime_deadline - time.monotonic()))
            if runtime_deadline
            else 5
        )
        time.sleep(frame_wait)
        general_points = ""
        root_element = None
        root_end_time = time.time() + min(
            60,
            max(0, runtime_deadline - time.monotonic()) if runtime_deadline else 60,
        )
        while time.time() < root_end_time and not root_element:
            if pause_file and os.path.exists(pause_file):
                print("[*] 检测到人工暂停标记，停止读取积分中心。")
                return None
            if runtime_deadline is not None and time.monotonic() >= runtime_deadline:
                return None
            try:
                root_element = frame.ele("tag:div@class:points-list", timeout=1)
            except Exception:
                time.sleep(0.2)

        if not root_element:
            print("[*] 积分中心页面加载过久")

        if root_element:
            # @@ 表示同时满足多个属性匹配，定位同时拥有 flex 和 flex-column 类的 div 区块
            block_elements = root_element.eles("tag:div@@class:flex@@class:flex-column")

            for block in block_elements:
                title_element = block.ele("tag:p@class:text-title")
                desc_element = block.ele("tag:p@class:text-desc")

                # 安全提取文本内容，避免因元素不存在而引发 AttributeError
                value_text = title_element.text.strip() if title_element else ""
                name_text = desc_element.text.strip() if desc_element else ""

                # 执行匹配逻辑：精确匹配，或包含“通用积分”且排除“云智手机”
                if name_text == "通用积分" or (
                    "通用积分" in name_text and "云智手机" not in name_text
                ):
                    general_points = value_text
                    break

        if not general_points:
            print("\r[-] 未读取到通用积分。")
            return None

        print(f"\r[*] 目前积分: {general_points}")
        return int(general_points)
    except Exception as e:
        print(f"[-] 无法获取积分中心数据：{e}")
        return None


def capture_page_task_api_request(
    page: ChromiumPage,
    pause_file: str,
    runtime_deadline: Optional[float],
    timeout: int = 12,
) -> tuple[Optional[Dict[str, str]], Optional[int]]:
    """Capture the page's own read-only task API request after auth rejection."""
    try:
        page.listen.start(POINTS_TASK_LIST_URL)
    except Exception as error:
        print(f"[!] 无法监听网页任务 API 请求：{type(error).__name__}")
        return None, None

    open_points_center_and_print(
        page,
        timeout=min(15, timeout),
        pause_file=pause_file,
        runtime_deadline=runtime_deadline,
        entry_timeout=20 if is_automatic_pc_trigger() else 120,
    )

    end_time = time.monotonic() + timeout
    packet = None
    while time.monotonic() < end_time:
        ensure_pc_runtime(runtime_deadline)
        if os.path.exists(pause_file):
            print("[*] 捕获任务 API 请求时检测到维护暂停，停止等待。")
            return None, None
        try:
            packet = page.listen.wait(timeout=min(1, max(0.1, end_time - time.monotonic())))
        except Exception:
            packet = None
        if packet:
            break

    if not packet:
        print("[!] 积分中心未产生可捕获的任务 API 请求。")
        return None, None

    try:
        headers = clean_headers(packet.request.headers)
    except Exception:
        headers = None

    progress = None
    try:
        body = packet.response.body
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        if isinstance(body, str):
            body = json.loads(body)
        progress = extract_current_progress(body)
    except Exception:
        progress = None

    if headers:
        print("[*] 已捕获网页自身生成的任务 API 请求头（不记录敏感值）。")
    if progress is not None:
        print(f"[*] 积分中心返回 currentProgress: {progress}/{TASK_PROGRESS_TARGET}")
    return headers, progress


def wait_for_points_with_points(
    page: ChromiumPage,
    step: int = 10,
    running_in_docker: bool = False,
    config_redeem_only: bool = False,
    device_code: Optional[str] = None,
    monitor: Optional[DesktopCdpMonitor] = None,
    runtime_deadline: Optional[float] = None,
    task_auth_data: Optional[dict] = None,
) -> None:
    """进入云电脑后挂机等待积分，结束前打印积分详情。"""
    print("[*] 已进入云电脑")
    next_api_poll_at = 0.0
    redeem_config_checked = False
    current_points: Optional[int] = None
    last_progress: Optional[int] = None
    canvas_missing_count = 0
    captured_api_headers: Optional[Dict[str, str]] = None
    url = POINTS_TASK_LIST_URL
    pause_file = get_pc_pause_file(running_in_docker)
    request_device_code = device_code or get_device_code(
        os.getenv("APP_USER") or "", running_in_docker
    )
    api_auth_data = (
        task_auth_data if isinstance(task_auth_data, dict) else read_auth_data(page)
    )
    api_auth_source = "prechecked" if task_auth_data is not None else "page"
    if pc_task_pause_requested(running_in_docker):
        print("[*] 检测到人工暂停标记，安全断开当前挂机会话。")
        sys.exit(0)

    while True:
        ensure_pc_runtime(runtime_deadline)
        if pc_task_pause_requested(running_in_docker):
            print("[*] 检测到人工暂停标记，安全断开当前挂机会话。")
            sys.exit(0)

        current_time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if monitor:
            page_text = get_desktop_page_text(page)
            canvas_present = has_connected_desktop_canvas(page)
            classification = classify_desktop_disconnect(
                page_text=page_text,
                canvas_present=canvas_present,
                network_events=monitor.events_for_classification(
                    since_canvas_connected=True
                ),
            )
            if classification != CONNECTED:
                loss_confirmed, canvas_missing_count = confirm_canvas_loss(
                    classification,
                    canvas_missing_count,
                    PC_CANVAS_LOSS_CONFIRMATIONS,
                )
                if not loss_confirmed:
                    print(
                        "[*] 远程桌面画布短暂不可见，等待连续确认 "
                        f"({canvas_missing_count}/{PC_CANVAS_LOSS_CONFIRMATIONS})。"
                    )
                    time.sleep(1)
                    continue
                reason = (
                    "页面出现明确的账号/会话冲突提示"
                    if classification == PREEMPTED
                    else "已连接期间桌面画布消失，且 CDP 未证明该故障属于桌面网络连接"
                )
                if classification == NETWORK_RETRYABLE:
                    reason = "已连接期间 CDP 确认桌面 WebSocket DNS/TCP/socket/timeout 故障"
                diagnostic_path = save_desktop_disconnect_diagnostic(
                    page,
                    monitor,
                    classification,
                    reason,
                    running_in_docker,
                )
                if classification == PREEMPTED:
                    if is_automatic_pc_trigger():
                        print("[!] 检测到客户端连接冲突，关闭当前网页后由自动任务有限重试。")
                    else:
                        print("[!] 检测到客户端连接冲突，立即退出，不再重连。")
                elif classification == UNKNOWN:
                    if is_automatic_pc_trigger():
                        print("[!] 画布消失且没有明确故障证据，关闭当前网页后由自动任务有限重试。")
                    else:
                        print("[!] 画布消失但没有明确故障证据，分类为 UNKNOWN，保守退出。")
                raise DesktopConnectionFailure(classification, diagnostic_path)
            canvas_missing_count = 0

        now_monotonic = time.monotonic()
        if now_monotonic < next_api_poll_at:
            time.sleep(min(1.0, max(0.1, next_api_poll_at - now_monotonic)))
            continue

        # Task and points endpoints are polled directly. Their failures repeat
        # only those API requests; no browser refresh or desktop reconnect occurs.
        try:
            if captured_api_headers:
                headers = dict(captured_api_headers)
            else:
                if not isinstance(api_auth_data, dict) or not api_auth_data.get("secretKey"):
                    raise AuthenticationExpiredError("当前页面缺少有效 authData")
                headers = build_task_api_headers(api_auth_data, request_device_code)
        except AuthenticationExpiredError as error:
            if api_auth_source == "prechecked":
                page_auth_data = read_auth_data(page)
                if (
                    isinstance(page_auth_data, dict)
                    and page_auth_data.get("secretKey")
                    and page_auth_data != api_auth_data
                ):
                    print(
                        "[*] 预检查登录态在桌面登录后失效，"
                        "仅切换到当前网页 authData 重试任务 API。"
                    )
                    api_auth_data = page_auth_data
                    api_auth_source = "page"
                    headers = (
                        dict(captured_api_headers)
                        if captured_api_headers
                        else build_task_api_headers(
                            api_auth_data, request_device_code
                        )
                    )
                else:
                    print(
                        f"[-] {current_time_str} {error}；"
                        "无法取得可用 authData，结束本次任务。"
                    )
                    sys.exit(2)
            else:
                print(
                    f"[-] {current_time_str} {error}；"
                    "无法取得可用 authData，结束本次任务。"
                )
                sys.exit(2)

        if not redeem_config_checked:
            config = ensure_redeem_config(
                page, clean_headers(headers), running_in_docker, config_redeem_only
            )
            redeem_config_checked = True
            enabled = config.get("enabled")

            if config_redeem_only:
                if not config or "enabled" not in config:
                    print("[!] 未完成兑换配置。")
                    sys.exit(1)
                print("[*] 兑换配置检查完成。")
                print("[*] 兑换配置流程结束。")
                sys.exit(0)

            if not enabled:
                print("[*] 自动兑换配置已禁用，继续执行挂机任务 。\n")
            else:
                print("[*] 已开启积分兑换，继续执行挂机任务 。\n")

        current_progress: Optional[int] = None
        api_error: Optional[AuthenticationExpiredError] = None
        try:
            current_progress = fetch_current_progress(url, headers)
        except AuthenticationExpiredError as error:
            api_error = error
            if api_auth_source == "prechecked":
                page_auth_data = read_auth_data(page)
                if (
                    isinstance(page_auth_data, dict)
                    and page_auth_data.get("secretKey")
                    and page_auth_data != api_auth_data
                ):
                    print(
                        "[*] 预检查登录态在桌面登录后被服务端拒绝，"
                        "仅切换到当前网页 authData 重试任务 API。"
                    )
                    api_auth_data = page_auth_data
                    api_auth_source = "page"
                    headers = build_task_api_headers(
                        api_auth_data, request_device_code
                    )
                    try:
                        current_progress = fetch_current_progress(url, headers)
                    except AuthenticationExpiredError as page_error:
                        api_error = page_error

            if current_progress is None and api_error is not None:
                # A UI login can be valid while a locally reconstructed CTG
                # signature is rejected. Capture the request the web client
                # itself generates; this is read-only and does not reconnect
                # or refresh the desktop page.
                page_headers, packet_progress = capture_page_task_api_request(
                    page,
                    pause_file,
                    runtime_deadline,
                    timeout=12,
                )
                if page_headers:
                    captured_api_headers = page_headers
                if packet_progress is not None:
                    current_progress = packet_progress
                elif page_headers:
                    retry_headers = dict(page_headers)
                    try:
                        current_progress = fetch_current_progress(
                            url, retry_headers
                        )
                    except AuthenticationExpiredError as captured_error:
                        api_error = captured_error
                if current_progress is None and not page_headers:
                    print(
                        f"[-] {current_time_str} {api_error}；"
                        "网页没有提供可用任务 API 请求，无法确认挂机完成。"
                    )
                    sys.exit(2)
                if current_progress is None and page_headers:
                    print(
                        f"[-] {current_time_str} {api_error}；"
                        "网页任务 API 请求头也无法确认进度，结束本次任务。"
                    )
                    sys.exit(2)
            elif current_progress is None and api_error is not None:
                print(
                    f"[-] {current_time_str} {api_error}；无法确认挂机完成。"
                )
                sys.exit(2)
        next_api_poll_at = time.monotonic() + max(1, step)

        if current_progress is not None:
            try:
                current_progress = int(current_progress)
            except (TypeError, ValueError):
                print(f"[-] {current_time_str} 服务端返回了无效的挂机进度。")
                current_progress = None

        if current_progress is not None:
            last_progress = log_progress_change(
                current_progress,
                last_progress,
                target=TASK_PROGRESS_TARGET,
                timestamp=current_time_str,
            )
            if current_progress >= TASK_PROGRESS_TARGET:
                print(f"\r[-] {current_time_str} 挂机任务完成。")
                current_points = fetch_general_points_with_retry(headers)
                if current_points is None:
                    print("[*] 积分 API 余额不可用，尝试保留的积分中心页面兼容读取。")
                    current_points = open_points_center_and_print(
                        page,
                        pause_file=pause_file,
                        runtime_deadline=runtime_deadline,
                    )
                auto_redeem_reward_after_hang(
                    page, headers, running_in_docker, current_points
                )
                return

        # Progress is used only to confirm completion. No stall-triggered page
        # refresh, desktop reconnect, or UI action is performed.


def build_task_api_headers(auth_data: dict, device_code: str) -> Dict[str, str]:
    """按云电脑 Web 客户端协议为任务接口生成 CTG 签名请求头。"""
    timestamp = int(time.time() * 1000)
    request_id = timestamp + 1
    device_type = CTG_WEB_DEVICE_TYPE
    tenant_id = str(auth_data.get("tenantId") or 0)
    user_id = str(auth_data.get("userId") or 0)
    version = CTG_API_VERSION
    secret_key = str(auth_data.get("secretKey") or "")
    signature_source = (
        f"{device_type}{request_id}{tenant_id}{timestamp}"
        f"{user_id}{version}{secret_key}"
    )
    signature = hashlib.md5(signature_source.encode("utf-8")).hexdigest().upper()

    return {
        "accept": "application/json, text/plain, */*",
        "ctg-appchannel": str(auth_data.get("appChannel") or "1"),
        "ctg-appmodel": CTG_WEB_APP_MODEL,
        "ctg-authenticate": "1",
        "ctg-device-manu": "Xiaomi",
        "ctg-device-model": "xiaomicc",
        "ctg-devicecode": device_code,
        "ctg-devicetype": device_type,
        "ctg-requestid": str(request_id),
        "ctg-signaturestr": signature,
        "ctg-softwarecode": "web_client",
        "ctg-tenantid": tenant_id,
        "ctg-timestamp": str(timestamp),
        "ctg-userid": user_id,
        "ctg-version": version,
        "from": "App-web",
        "referer": "https://desk.ctyun.cn/selforder/points.html",
        "x-lang": "zh-CN",
        "user-agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "Chrome/143.0.0.0 Safari/537.36"
        ),
    }


def build_page_task_api_headers(
    page: ChromiumPage, device_code: str
) -> Dict[str, str]:
    """从当前页面 authData 生成带新时间戳/签名的任务接口请求头。"""
    auth_data = read_auth_data(page)
    if not isinstance(auth_data, dict) or not auth_data.get("secretKey"):
        raise AuthenticationExpiredError("当前页面缺少有效 authData")
    return build_task_api_headers(auth_data, device_code)


def fetch_signed_task_progress(
    auth_data: dict, device_code: str, log_errors: bool = False
) -> tuple[Optional[int], Dict[str, str]]:
    """直接通过已保存的 authData 查询任务进度，无需打开云桌面。"""
    headers = build_task_api_headers(auth_data, device_code)
    progress = fetch_current_progress(
        POINTS_TASK_LIST_URL, headers, log_errors=log_errors
    )
    return progress, headers


def fetch_general_points(headers: Dict[str, str]) -> Optional[int]:
    """通过积分中心 API 获取通用积分余额。"""
    try:
        response = requests.get(USER_POINTS_URL, headers=clean_headers(headers), timeout=5)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("code") != 0:
            print("[!] 积分 API 未返回成功状态；下次只重试该 API。")
            return None
        points_list = result.get("data")
        if not isinstance(points_list, list):
            print("[!] 积分 API 返回数据格式无效；下次只重试该 API。")
            return None
        for item in points_list:
            if isinstance(item, dict) and str(item.get("pointType", "")) == "1":
                if item.get("points") is None:
                    return None
                return int(item["points"])
        print("[!] 积分 API 未返回通用积分余额；下次只重试该 API。")
        return None
    except (requests.RequestException, TypeError, ValueError) as error:
        print(f"[!] 获取通用积分失败：{error}；下次只重试该 API。")
        return None


def fetch_general_points_with_retry(
    headers: Dict[str, str], attempts: int = 3, delay: int = 2
) -> Optional[int]:
    """Retry only the points API; never refresh or reopen the desktop page."""
    for attempt in range(1, attempts + 1):
        points = fetch_general_points(headers)
        if points is not None:
            return points
        if attempt < attempts:
            print(f"[*] 积分 API 重试 ({attempt}/{attempts - 1})。")
            time.sleep(delay)
    return None


def fetch_current_progress(
    url: str, headers: Dict[str, str], log_errors: bool = True
) -> Optional[int]:
    """
    向指定的 URL 发起 GET 请求，并直接解析提取 currentProgress 的值。
    Args:
        url (str): 接口的目标 URL。
        headers (Dict[str, str]): 请求头字典。

    Returns:
        Optional[Any]: 成功提取到进度值则返回该值；如果请求失败或数据不存在则返回 None。
    """
    try:
        headers = clean_headers(headers)

        response = requests.get(url, headers=headers, timeout=5)

        response.raise_for_status()

        data = response.json()
        if isinstance(data, dict) and data.get("code") == 40010:
            raise AuthenticationExpiredError(
                "当前登录信息已过期，请重新登录"
            )
        progress = extract_current_progress(data)
        if progress is None and log_errors:
            current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{current_time}] 任务列表中缺少“使用1小时”任务或 currentProgress；仅重试任务 API。")
        return progress

    except (requests.RequestException, TypeError, ValueError) as error:
        if log_errors:
            current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{current_time}] 获取或解析数据失败: {error}")
        return None


def get_redeem_config_path(running_in_docker: bool) -> str:
    """兑换配置路径，仅保存容器内。"""
    if running_in_docker:
        return "/app/redeem_config.json"
    return "./redeem_config.json"


def load_redeem_config(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[!] 读取兑换配置失败: {e}")
        return {}


def save_redeem_config(path: str, config: dict) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        print(f"[*] 兑换配置已保存: {path}")
    except Exception as e:
        print(f"[!] 保存兑换配置失败: {e}")


def clean_headers(headers: Dict[str, str]) -> Dict[str, str]:
    clean = {}
    for k, v in dict(headers).items():
        if not str(k).startswith(":"):
            clean[str(k)] = v
    return clean


def parse_general_points(points_text: str) -> int:
    digits = "".join(ch for ch in str(points_text) if ch.isdigit())
    return int(digits) if digits else 0


def parse_desktops_from_session(page: ChromiumPage) -> list[dict]:
    raw = page.run_js("return sessionStorage.getItem('desktops');")
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return data if isinstance(data, list) else []
    except Exception:
        return []


def fetch_redeemable_rewards(headers: Dict[str, str]) -> list[dict]:
    try:
        response = requests.get(REWARD_LIST_URL, headers=headers, timeout=15)
        response.raise_for_status()
        result = response.json()
    except Exception as e:
        print(f"[!] 获取兑换奖励列表失败: {e}")
        return []

    if result.get("code") == 40010:
        print("[!] 当前登录信息已过期，请重新登录后再试兑换。")
        return []

    rewards: list[dict] = []
    for mall in result.get("data", []):
        for series in mall.get("series", []):
            if series.get("expireDate") is not None:
                continue
            for sku in series.get("sku", []):
                if sku.get("expireDate") is not None:
                    continue
                prod_id = int(sku.get("prodId", 0))
                prod_name = str(sku.get("prodName", "")).strip()
                cost_points = int(sku.get("costPoints", 0))
                description = str(sku.get("description", "")).strip()
                prod_type = str(sku.get("prodType", "")).strip()
                rewards.append(
                    {
                        "prodId": prod_id,
                        "prodName": prod_name,
                        "costPoints": cost_points,
                        "description": description,
                        "prodType": prod_type,
                    }
                )
    return rewards


def _input_index(max_index: int, prompt: str) -> int:
    while True:
        raw = input(prompt).strip()
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= max_index:
                return idx - 1
        print(f"[-] 请输入 1 到 {max_index} 的数字。")


def _input_non_negative_int(prompt: str, default_value: int) -> int:
    while True:
        raw = input(prompt).strip()
        if raw == "":
            return default_value
        if raw.isdigit():
            return int(raw)
        print("[-] 请输入大于等于 0 的整数。")


def _input_positive_int(prompt: str, default_value: int) -> int:
    while True:
        raw = input(prompt).strip()
        if raw == "":
            return default_value
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
        print("[-] 请输入大于 0 的整数。")


def _input_month_days(prompt: str) -> list[int]:
    while True:
        raw = input(prompt).strip()
        if raw == "":
            print("[-] 不能为空，请输入如 1,15,28 或 -1")
            continue
        values = []
        valid = True
        for item in raw.split(","):
            item = item.strip()
            if item == "-1":
                values.append(-1)
                continue
            if not item.isdigit():
                valid = False
                break
            day = int(item)
            if day < 1 or day > 31:
                valid = False
                break
            values.append(day)
        if not valid or not values:
            print("[-] 格式错误，请输入 1-31 或 -1，并用逗号分隔，如 1,15,28,-1")
            continue
        return sorted(set(values))


def prompt_redeem_schedule() -> dict:
    print("\n兑换时间设置：")
    print("1. 每日兑换（默认）")
    print("2. 每隔N日兑换")
    print("3. 每月几号兑换（逗号分隔，支持 -1 表示月末最后一天）")
    while True:
        choice = input("请选择时间策略 [1/2/3，默认1]: ").strip()
        if choice in ("", "1"):
            return {"scheduleType": "daily"}
        if choice == "2":
            interval_days = _input_positive_int("请输入间隔天数N [默认1]: ", 1)
            return {"scheduleType": "interval_days", "intervalDays": interval_days}
        if choice == "3":
            month_days = _input_month_days("请输入每月兑换日期（如 1,15,28,-1）: ")
            return {"scheduleType": "monthly_days", "monthlyDays": month_days}
        print("[-] 请输入 1、2 或 3。")


def should_redeem_today(config: dict, today: datetime.date) -> tuple[bool, str]:
    schedule_type = str(config.get("scheduleType") or "daily").strip()
    last_redeem_date = str(config.get("lastRedeemDate") or "").strip()
    today_str = today.isoformat()

    if last_redeem_date == today_str:
        return False, f"今天({today_str})已兑换过，跳过。"

    if schedule_type == "daily":
        return True, "每日兑换策略，允许执行。"

    if schedule_type == "interval_days":
        interval_days = int(config.get("intervalDays", 1) or 1)
        if interval_days < 1:
            interval_days = 1
        if not last_redeem_date:
            return True, "间隔兑换策略首次执行。"
        try:
            last_day = datetime.date.fromisoformat(last_redeem_date)
        except ValueError:
            return True, "上次兑换日期格式异常，允许执行。"
        passed_days = (today - last_day).days
        if passed_days >= interval_days:
            return True, f"已间隔 {passed_days} 天，满足每隔 {interval_days} 天兑换。"
        return False, f"距上次仅 {passed_days} 天，未到每隔 {interval_days} 天。"

    if schedule_type == "monthly_days":
        month_days = config.get("monthlyDays", [])
        if not isinstance(month_days, list):
            return False, "每月兑换日期配置错误，跳过。"
        try:
            allow_month_end = False
            allowed_days = set()
            for day in month_days:
                day_num = int(day)
                if day_num == -1:
                    allow_month_end = True
                elif 1 <= day_num <= 31:
                    allowed_days.add(day_num)
        except Exception:
            return False, "每月兑换日期配置错误，跳过。"
        allowed_days = sorted(allowed_days)
        if not allowed_days and not allow_month_end:
            return False, "每月兑换日期为空，跳过。"
        last_day = calendar.monthrange(today.year, today.month)[1]
        if allow_month_end and today.day == last_day:
            return True, f"今天是 {today.day} 号（本月最后一天），命中每月兑换日。"
        if today.day in allowed_days:
            return True, f"今天是 {today.day} 号，命中每月兑换日。"
        display_days = [*allowed_days]
        if allow_month_end:
            display_days.append(-1)
        return False, f"今天是 {today.day} 号，不在每月兑换日 {display_days} 中。"

    return True, "未知策略，按每日策略执行。"


def prompt_and_create_redeem_config(
    page: ChromiumPage, headers: Dict[str, str], running_in_docker: bool
) -> dict:
    if not sys.stdin.isatty():
        print("[*] 检测到后台启动模式，跳过兑换配置交互。")
        print(
            "[*] 如需配置兑换，请执行: /app/run_task.sh pc "
            "/usr/bin/python3 -u /app/pc_login.py --config-redeem"
        )
        return {}

    while True:
        print("\n\n=== 自动兑换配置 ===")
        enable_input = input("是否启用自动兑换奖励? [Y/n]: ").strip().lower()
        if enable_input in ("", "y", "yes"):
            enable_redeem = True
            break
        if enable_input in ("n", "no"):
            enable_redeem = False
            break
        print("[-] 请输入 y 或 n。")

    if not enable_redeem:
        disabled_config = {"enabled": False}
        save_redeem_config(get_redeem_config_path(running_in_docker), disabled_config)
        print("[*] 已设置为不启用自动兑换。")
        return disabled_config

    desktops = parse_desktops_from_session(page)
    if not desktops:
        print("[!] sessionStorage 未读取到 desktops，无法配置自动兑换。")
        return {}

    rewards = fetch_redeemable_rewards(headers)
    if not rewards:
        print("[!] 未获取到可兑换奖励，无法配置自动兑换。")
        return {}

    print("\n=== 自动兑换配置（首次）===")
    print("可选设备：")
    for idx, item in enumerate(desktops, start=1):
        print(
            f"{idx}. {item.get('objName', '未知设备')} "
            f"(desktopId={item.get('desktopId', '')})"
        )
    desktop_idx = _input_index(len(desktops), "请选择设备序号: ")
    desktop = desktops[desktop_idx]

    print("\n可选奖励：")
    for idx, reward in enumerate(rewards, start=1):
        print(
            f"{idx}. {reward['prodName']} {reward['costPoints']}积分 (prodId={reward['prodId']}, "
            f"prodType={reward['prodType']})"
        )
    reward_idx = _input_index(len(rewards), "请选择奖励序号: ")
    reward = rewards[reward_idx]

    print("\n兑换次数设置：")
    max_redeem_times = _input_non_negative_int(
        "每次挂机完成最多兑换次数 [默认0=按积分尽量兑换]: ", 0
    )
    schedule_config = prompt_redeem_schedule()

    config = {
        "enabled": True,
        "desktopId": str(desktop.get("desktopId", "")).strip(),
        "prodId": int(reward["prodId"]),
        "prodName": reward["prodName"],
        "prodType": reward["prodType"],
        "costPoints": int(reward["costPoints"]),
        "maxRedeemTimes": int(max_redeem_times),
        "lastRedeemDate": "",
    }
    config.update(schedule_config)
    save_redeem_config(get_redeem_config_path(running_in_docker), config)
    return config


def ensure_redeem_config(
    page: ChromiumPage,
    headers: Dict[str, str],
    running_in_docker: bool,
    config_redeem_only: bool = False,
) -> dict:
    path = get_redeem_config_path(running_in_docker)
    config = load_redeem_config(path)
    if config_redeem_only and config:
        file_path = Path(path)
        file_path.unlink()
        return prompt_and_create_redeem_config(page, headers, running_in_docker)
    if config and "enabled" in config:
        return config
    return prompt_and_create_redeem_config(page, headers, running_in_docker)


def build_place_order_payload(
    sku_prod_id: int, desktop_id: int, prod_type: str, cost_points: int, times: int
) -> dict:
    return {
        "busiChannel": "010",
        "orderType": 1,
        "pointType": 1,
        "points": int(cost_points) * int(times),
        "sku": [
            {
                "execSort": idx + 1,
                "prodId": int(sku_prod_id),
                "prodType": prod_type,
                "attrs": [{"attrKey": "bindDesktopId", "attrVal": int(desktop_id)}],
            }
            for idx in range(times)
        ],
    }


def try_redeem_reward_once(
    headers: Dict[str, str], payload: dict, times: int, cost_points: int
) -> bool:
    try:
        response = requests.post(
            PLACE_ORDER_URL, json=payload, headers=headers, timeout=20
        )
        data = response.json()
    except Exception as e:
        print(f"[!] 兑换请求失败: {e}")
        return False

    code = data.get("code")
    if code == 0:
        print(f"[*] 兑换成功：本次兑换 {times} 次，共消耗 {times * cost_points} 积分。")
        return True
    if code == 40010:
        print("[!] 兑换失败：当前登录信息已过期，请重新登录。")
        return False
    if code == 30010:
        print("[!] 兑换失败：资源施工中，请稍后再试。")
        return False
    print(f"[!] 兑换失败：code={code}, msg={data.get('msg', '未知错误')}")
    return False


def auto_redeem_reward_after_hang(
    page: Optional[ChromiumPage],
    request_headers: Dict[str, str],
    running_in_docker: bool,
    current_points: Optional[int],
) -> None:
    headers = clean_headers(request_headers)
    config_path = get_redeem_config_path(running_in_docker)
    if page is None:
        # A completed task can be handled without opening a desktop. In this
        # path only an existing non-interactive redemption config is honored.
        config = load_redeem_config(config_path)
    else:
        config = ensure_redeem_config(page, headers, running_in_docker)
    if not config:
        return

    try:
        enabled = config.get("enabled")
        if not enabled:
            print("[*] 自动兑换配置已禁用，跳过。")
            return
        desktop_id = int(str(config.get("desktopId", "")).strip())
        prod_id = int(config.get("prodId"))
        prod_type = str(config.get("prodType", "")).strip()
        cost_points = int(config.get("costPoints", 0))
        max_redeem_times = int(config.get("maxRedeemTimes", 1))
        prod_name = str(config.get("prodName", "")).strip()
    except Exception:
        print("[!] 自动兑换配置格式错误，跳过。")
        return

    if cost_points <= 0:
        print("[!] 配置中的 costPoints 异常，跳过兑换。")
        return

    if current_points is None:
        print("[!] 无法读取积分余额，跳过自动兑换；不会猜测兑换次数。")
        return
    if current_points <= 0:
        print("[*] 当前积分为 0，跳过兑换。")
        return

    today = datetime.date.today()
    can_redeem_today, reason = should_redeem_today(config, today)
    if not can_redeem_today:
        print(f"[*] 兑换计划未执行：{reason}")
        return
    print(f"[*] 兑换计划命中：{reason}")

    can_redeem_times = current_points // cost_points
    if can_redeem_times <= 0:
        print(
            f"[*] 当前积分 {current_points} 小于单次兑换所需 {cost_points}，本次不兑换。"
        )
        return
    final_times = (
        can_redeem_times
        if max_redeem_times == 0
        else min(can_redeem_times, max_redeem_times)
    )

    print(
        f"[*] 准备自动兑换({prod_name})：prodId={prod_id}, desktopId={desktop_id}, "
        f"prodType={prod_type}, costPoints={cost_points}, 尝试={final_times}次。"
    )
    restart_at = int(time.time()) + 120
    attempt_times = final_times
    while attempt_times > 0:
        payload = build_place_order_payload(
            prod_id, desktop_id, prod_type, cost_points, attempt_times
        )
        if try_redeem_reward_once(headers, payload, attempt_times, cost_points):
            config["lastRedeemDate"] = today.isoformat()
            save_redeem_config(config_path, config)
            if running_in_docker:
                try:
                    with open(RESTART_AT_FILE, "w", encoding="utf-8") as f:
                        f.write(str(restart_at))
                    print("[*] 已设置 CtYun.dll 在 2 分钟后自动重启。")
                except Exception as e:
                    print(f"[!] 写入重启计划失败: {e}")
            return
        attempt_times -= 1

    print("[!] 自动兑换未成功，可稍后重试。")


def execute_login(
    page: ChromiumPage,
    username: str,
    password: str,
    max_retries: int = 6,
) -> bool:
    """执行云电脑登录流程。"""
    automatic_trigger = is_automatic_pc_trigger()
    navigation_timeout = 15 if automatic_trigger else 30
    # 最大重试次数设置
    for attempt_ in range(1, max_retries + 1):
        try:
            page.get(LOGIN_URL, **pc_navigation_options())

            # Scheduled runs keep one navigation round short so the outer retry
            # policy can recover instead of spending minutes in page-load retries.
            is_loaded = page.wait.doc_loaded(timeout=navigation_timeout)

            if not is_loaded:
                raise TimeoutError(
                    f"[*] 等待页面加载响应超时 ({navigation_timeout}秒)"
                )

            # 填写账号密码
            fill_credentials(
                page,
                username,
                password,
                selector_timeout=5 if automatic_trigger else 60,
            )
            break

        except Exception:
            # save_screenshot(page)
            if attempt_ < max_retries:
                print("[*] 等待 3 秒后进行下一次进入...")
                time.sleep(3)
            else:
                print(f"[-] 已达到最大重试次数 ({max_retries} 次)，网页加载失败。")
                sys.exit(1)

    for attempt in range(1, max_retries + 1):
        try:
            print(f"[*] 登录尝试 {attempt}/{max_retries}")
            if is_login_success(page):
                return True

            fill_captcha_if_possible(page)
            click_login_button(page)

            toast_text = get_latest_toast(
                page, timeout=10 if automatic_trigger else 20
            )
            if toast_text:
                print(f"[*] 登录提示: {toast_text}")

            if "用户名或密码错误" in toast_text:
                return False
            if "图形验证码错误" in toast_text:
                refresh_captcha_image(page)
                continue
            if "请输入图形验证码" in toast_text:
                continue

            if is_login_success(page):
                return True
        except Exception:
            if attempt < max_retries:
                print("[*] 等待 3 秒后进行下一次重试登录...")
                time.sleep(3)
            else:
                print(f"[-] 已达到最大重试次数 ({max_retries} 次)")
                sys.exit(1)

    return False


class NumericOcrSolver:
    """单例数字 OCR 识别器。"""

    _instance: Optional["NumericOcrSolver"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "NumericOcrSolver":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init_engine()
        return cls._instance

    def _init_engine(self) -> None:
        import ddddocr

        self.ocr = ddddocr.DdddOcr(show_ad=False)
        self.ocr.set_ranges(0)

    def solve(self, image_data: bytes) -> str:
        try:
            return self.ocr.classification(image_data)
        except Exception:
            return ""


def get_bytes_numeric_captcha(image_bytes: bytes) -> str:
    solver = NumericOcrSolver()
    return solver.solve(image_bytes)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="天翼云电脑挂机与积分兑换脚本")
    parser.add_argument(
        "--config-redeem",
        action="store_true",
        help="仅执行兑换配置交互，不进入挂机流程",
    )
    parser.add_argument(
        "--precheck-complete",
        action="store_true",
        help="只读查询服务端挂机进度；返回状态 10 表示已完成",
    )
    parser.add_argument(
        "--redeem-completed-task",
        action="store_true",
        help="重新确认挂机已完成后，执行已配置的自动积分兑换",
    )
    return parser.parse_args()


def run_browser_task_attempt(
    username: str,
    password: str,
    auth_data_file: str,
    device_code: str,
    running_in_docker: bool,
    config_redeem_only: bool,
    pause_file: str,
    runtime_deadline: Optional[float] = None,
    task_auth_data: Optional[dict] = None,
) -> None:
    """Run one isolated browser session; quitting it stops page auto-reconnect."""
    ensure_pc_runtime(runtime_deadline)
    if os.path.exists(pause_file):
        print("[*] 检测到维护暂停标记，在启动浏览器前退出。")
        return
    options = init_browser_options(running_in_docker)
    page: Optional[ChromiumPage] = None
    monitor = DesktopCdpMonitor()
    try:
        page = ChromiumPage(addr_or_opts=options)
        if is_automatic_pc_trigger():
            page.set.timeouts(base=3, page_load=15, script=3)
        # Inject the saved session before opening the desktop list.
        page.get(LOGIN_URL, **pc_navigation_options())
        attach_desktop_cdp_monitor(page, monitor)
        inject_local_storage_session(page, device_code, auth_data_file)
        # The page may enter the desktop automatically during this refresh.
        # Subscribe before navigation so CDP sees the WebSocket creation event.
        monitor.begin_desktop_attempt()
        ensure_pc_runtime(runtime_deadline)
        if os.path.exists(pause_file):
            print("[*] 检测到维护暂停标记，取消登录态刷新。")
            return
        page.get(LOGIN_URL, **pc_navigation_options())
        time.sleep(1 if is_automatic_pc_trigger() else 2)

        relogin_attempts = 0
        max_relogin_attempts = 3
        unknown_attempts = 0
        desktop_open_attempts = 0
        desktop_opened = False

        while True:
            ensure_pc_runtime(runtime_deadline)
            if os.path.exists(pause_file):
                print("[*] 检测到云电脑挂机暂停标记，结束云电脑连接任务。")
                return

            ensure_pc_runtime(runtime_deadline)
            if os.path.exists(pause_file):
                print("[*] 检测到维护暂停标记，取消网页桌面导航。")
                return
            page.get(DESKTOP_URL, **pc_navigation_options())
            time.sleep(1)
            wait_desktop_list_refresh_done(
                page, timeout=30 if is_automatic_pc_trigger() else 60
            )
            state = get_desktop_state(page)
            print(f"\r[*] desktop-list 状态: {state}")

            if state == "auth_expired" or state == "unknown":
                if relogin_attempts >= max_relogin_attempts:
                    print("[!] 重登次数已达上限。")
                    sys.exit(1)
                relogin_attempts += 1
                print(
                    f"[*] 检测到未登录或登录态过期，开始账号密码重登 ({relogin_attempts}/{max_relogin_attempts})"
                )
                login_retries = 3 if is_automatic_pc_trigger() else 6
                if not execute_login(
                    page, username, password, max_retries=login_retries
                ):
                    print("[!] 重新登录失败。")
                    sys.exit(1)
                save_auth_data(page, auth_data_file)
                continue

            if state == "no_desktop":
                print("[*] 当前账号无云电脑资源，任务结束。")
                return

            if state == "only_phone":
                print("[*] 当前账号仅有云手机资源，任务结束。")
                return

            if state == "has_pc_button":
                if os.path.exists(pause_file):
                    print("[*] 检测到云电脑挂机暂停标记，取消桌面连接。")
                    return
                if desktop_open_attempts >= MAX_DESKTOP_OPEN_ATTEMPTS:
                    print("[!] 当前网页会话的桌面进入次数已达上限。")
                    sys.exit(1)
                desktop_open_attempts += 1
                print(
                    "[*] 检测到“进入AI云电脑”按钮，尝试连接云电脑 "
                    f"({desktop_open_attempts}/{MAX_DESKTOP_OPEN_ATTEMPTS})。"
                )
                monitor.begin_desktop_attempt()
                ensure_pc_runtime(runtime_deadline)
                if os.path.exists(pause_file):
                    print("[*] 检测到维护暂停标记，取消桌面连接。")
                    return
                if not click_enter_ai_pc(page):
                    print("[!] 未能点击“进入AI云电脑”按钮。")
                    sys.exit(1)
                wait_result = wait_desktop_opened(
                    page,
                    timeout=240,
                    pause_file=pause_file,
                    monitor=monitor,
                    running_in_docker=running_in_docker,
                    runtime_deadline=runtime_deadline,
                )
                if wait_result != CONNECTED:
                    if os.path.exists(pause_file):
                        return
                    raise DesktopConnectionFailure(wait_result)
                desktop_opened = True
                break

            if state == "desktop_entered_auto":
                if desktop_open_attempts >= MAX_DESKTOP_OPEN_ATTEMPTS:
                    print("[!] 当前网页会话的桌面进入次数已达上限。")
                    sys.exit(1)
                desktop_open_attempts += 1
                print(
                    "[*] 页面已跳转，正在等待云电脑连接 "
                    f"({desktop_open_attempts}/{MAX_DESKTOP_OPEN_ATTEMPTS})。"
                )
                wait_result = wait_desktop_opened(
                    page,
                    timeout=240,
                    pause_file=pause_file,
                    monitor=monitor,
                    running_in_docker=running_in_docker,
                    runtime_deadline=runtime_deadline,
                )
                if wait_result != CONNECTED:
                    if os.path.exists(pause_file):
                        return
                    raise DesktopConnectionFailure(wait_result)
                desktop_opened = True
                break

            unknown_attempts += 1
            if unknown_attempts >= 3:
                print("[!] 无法识别 desktop-list 页面状态，任务结束。")
                sys.exit(1)
            print(f"[-] 未识别到明确状态，重试中 ({unknown_attempts}/3)")
            time.sleep(2)

        if not desktop_opened:
            print("[!] 未进入云电脑页面。")
            sys.exit(1)

        auth_data = read_auth_data(page)
        mobile = auth_data.get("mobilephone") if auth_data else None
        if mobile:
            print("[*] 登录成功。")
        else:
            print("[-] 登录成功，但未能读取 authData.mobilephone。")

        active_device_code = read_page_device_code(page, device_code)

        wait_for_points_with_points(
            page,
            running_in_docker=running_in_docker,
            config_redeem_only=config_redeem_only,
            device_code=active_device_code,
            monitor=monitor,
            runtime_deadline=runtime_deadline,
            task_auth_data=task_auth_data,
        )
    except (PcMaxRuntimeExpired, PcTaskAuthorizationLost, DesktopConnectionFailure):
        raise
    except SystemExit as error:
        if not is_automatic_pc_trigger() or error.code != 1:
            raise
        reason = "Cron/INIT_RUN 浏览器流程以失败状态退出"
        diagnostic_path = (
            save_desktop_disconnect_diagnostic(
                page, monitor, UNKNOWN, reason, running_in_docker
            )
            if page is not None
            else None
        )
        print("[!] 自动触发的网页流程失败，归类为 UNKNOWN，由外层有限重试。")
        raise DesktopConnectionFailure(UNKNOWN, diagnostic_path) from error
    except Exception as error:
        if not is_automatic_pc_trigger():
            raise
        reason = f"Cron/INIT_RUN 浏览器流程异常（{type(error).__name__}）"
        diagnostic_path = (
            save_desktop_disconnect_diagnostic(
                page, monitor, UNKNOWN, reason, running_in_docker
            )
            if page is not None
            else None
        )
        print("[!] 自动触发的网页流程异常，归类为 UNKNOWN，由外层有限重试。")
        raise DesktopConnectionFailure(UNKNOWN, diagnostic_path) from error
    finally:
        # Closing the Chromium process cancels the website's own reconnect loop.
        if page is not None:
            try:
                page.quit()
            except Exception as error:
                raise BrowserCleanupFailure(
                    f"Chromium 未能确认关闭；禁止发起下一轮网页连接：{error}"
                ) from error
            wait_for_task_child_processes()


def fetch_signed_progress_with_retry(
    auth_data: dict,
    device_code: str,
    attempts: int = 3,
    delay: int = 2,
) -> tuple[Optional[int], Optional[Dict[str, str]]]:
    """Retry only the task API when checking completion before a reconnect."""
    headers: Optional[Dict[str, str]] = None
    for attempt in range(1, attempts + 1):
        try:
            progress, headers = fetch_signed_task_progress(
                auth_data, device_code, log_errors=True
            )
        except AuthenticationExpiredError:
            raise
        if progress is not None:
            return progress, headers
        if attempt < attempts:
            print(f"[*] 完成状态 API 重试 ({attempt}/{attempts - 1})。")
            time.sleep(delay)
    return None, headers


def get_saved_task_progress(
    running_in_docker: bool,
) -> tuple[Optional[int], Optional[Dict[str, str]]]:
    """Read saved session task progress without opening a browser or mutating state."""
    username = os.getenv("APP_USER")
    if not username:
        print("[*] 完成状态预检查缺少 APP_USER，稍后走正常授权流程。")
        return None, None

    auth_data = load_auth_data_from_file(
        get_auth_data_file(username, running_in_docker)
    )
    if not auth_data:
        print("[*] 没有已保存的 authData，稍后走正常授权流程。")
        return None, None

    device_code = os.getenv("DEVICECODE", "").strip()
    if not device_code:
        device_code_file = (
            f"/app/data/.devicecode_{username}"
            if running_in_docker
            else f"./.devicecode_{username}"
        )
        try:
            device_code = Path(device_code_file).read_text(encoding="utf-8").strip()
        except OSError:
            device_code = ""
    if not device_code:
        print("[*] 没有已保存的 DEVICECODE，稍后走正常授权流程。")
        return None, None

    try:
        current_progress, headers = fetch_signed_progress_with_retry(
            auth_data, device_code
        )
    except AuthenticationExpiredError as error:
        print(f"[*] 完成状态预检查不可用：{error}；稍后走正常授权流程。")
        return None, None
    if current_progress is None:
        print("[*] 暂时无法确认任务进度，稍后走正常授权流程。")
        return None, None

    return current_progress, headers


def precheck_completed_task(running_in_docker: bool) -> bool:
    """Pure read-only completion check used before stopping the DLL."""
    current_progress, _headers = get_saved_task_progress(running_in_docker)
    if current_progress is None:
        print("[*] 暂时无法确认任务进度，稍后走正常授权流程。")
        return False

    print(
        f"[*] 挂机只读预检查 currentProgress: "
        f"{current_progress}/{TASK_PROGRESS_TARGET}"
    )
    if current_progress < TASK_PROGRESS_TARGET:
        return False

    print("[*] 服务端已确认挂机任务完成；只读预检查未执行积分查询或兑换。")
    return True


def redeem_completed_task(running_in_docker: bool) -> bool:
    """Recheck completion, then run the explicitly requested redemption phase."""
    current_progress, headers = get_saved_task_progress(running_in_docker)
    if current_progress is None:
        print("[!] 兑换前无法确认服务端任务进度，跳过正式兑换阶段。")
        return False
    print(
        f"[*] 正式兑换前确认 currentProgress: "
        f"{current_progress}/{TASK_PROGRESS_TARGET}"
    )
    if current_progress < TASK_PROGRESS_TARGET:
        print("[*] 服务端任务当前未完成，跳过正式兑换阶段。")
        return False

    print("[*] 服务端任务已完成，开始执行已配置的正式积分兑换阶段。")
    request_headers = headers or {}
    current_points = fetch_general_points_with_retry(request_headers)
    auto_redeem_reward_after_hang(
        None, request_headers, running_in_docker, current_points
    )
    return True


def graceful_shutdown_signal(signum, _frame) -> None:
    signal_name = signal.Signals(signum).name
    print(f"[*] 收到 {signal_name}，开始关闭浏览器并清理任务。")
    raise SystemExit(128 + signum)


def main(
    config_redeem_only: bool = False,
    precheck_complete: bool = False,
    redeem_completed_task_only: bool = False,
) -> None:
    signal.signal(signal.SIGTERM, graceful_shutdown_signal)
    signal.signal(signal.SIGINT, graceful_shutdown_signal)

    if precheck_complete:
        running_in_docker = os.getenv("RUNNING_IN_DOCKER") == "true"
        sys.exit(10 if precheck_completed_task(running_in_docker) else 11)

    if redeem_completed_task_only:
        running_in_docker = os.getenv("RUNNING_IN_DOCKER") == "true"
        sys.exit(10 if redeem_completed_task(running_in_docker) else 11)

    if not pc_task_authorization_valid():
        print("[!] 网页任务必须通过 /app/run_task.sh pc 启动并完成 DLL 交接。")
        sys.exit(2)

    username = os.getenv("APP_USER")
    password = os.getenv("APP_PASSWORD")
    running_in_docker = os.getenv("RUNNING_IN_DOCKER") == "true"
    if not username or not password:
        print("[!] 缺少环境变量 APP_USER 或 APP_PASSWORD。")
        sys.exit(1)

    pause_file = get_pc_pause_file(running_in_docker)
    if pc_task_pause_requested(running_in_docker):
        print("[*] 检测到维护暂停标记，跳过本次网页任务。")
        return

    runtime_deadline = time.monotonic() + get_pc_max_runtime_seconds()
    auth_data_file = get_auth_data_file(username, running_in_docker)
    device_code = get_device_code(username, running_in_docker)
    task_auth_data: Optional[dict] = None

    if not config_redeem_only:
        saved_auth_data = load_auth_data_from_file(auth_data_file)
        if saved_auth_data:
            try:
                initial_progress, initial_headers = fetch_signed_progress_with_retry(
                    saved_auth_data, device_code
                )
            except AuthenticationExpiredError as error:
                print(f"[!] {error}。继续由现有网页登录流程刷新登录态。")
                initial_progress, initial_headers = None, None
            if initial_progress is not None and initial_progress >= TASK_PROGRESS_TARGET:
                print("[*] 服务端 currentProgress 已达到 3600，跳过网页连接。")
                headers = initial_headers or build_task_api_headers(saved_auth_data, device_code)
                current_points = fetch_general_points_with_retry(headers)
                auto_redeem_reward_after_hang(
                    None, headers, running_in_docker, current_points
                )
                return
            if initial_progress is not None:
                # Preserve the credentials just validated by the read-only API
                # precheck in case the browser later rotates its local session.
                task_auth_data = saved_auth_data
        else:
            print("[*] 本地没有可用于完成状态预检的 authData，继续浏览器登录流程。")

    reconnects_used = 0
    reconnect_pending = False
    while True:
        try:
            ensure_pc_runtime(runtime_deadline)
            if pc_task_pause_requested(running_in_docker):
                print("[*] 检测到维护暂停标记，停止或取消网页挂机。")
                return

            if reconnect_pending:
                saved_auth_data = load_auth_data_from_file(auth_data_file)
                if not saved_auth_data:
                    print("[!] 重连前缺少 authData，无法确认任务是否完成；停止重连。")
                    return
                progress_auth_expired = False
                try:
                    current_progress, current_headers = fetch_signed_progress_with_retry(
                        saved_auth_data, device_code
                    )
                except AuthenticationExpiredError as error:
                    if not is_automatic_pc_trigger():
                        print(f"[!] 重连前任务 API 登录态失效：{error}；停止重连。")
                        return
                    # The next bounded scheduled attempt reloads the page and
                    # lets the existing login flow refresh its saved session.
                    print(
                        f"[!] 重连前任务 API 登录态失效：{error}；"
                        "Cron/INIT_RUN 将在剩余重试预算内继续，由网页登录流程刷新会话。"
                    )
                    progress_auth_expired = True
                    current_progress, current_headers = None, None
                    task_auth_data = None
                if current_progress is None and not progress_auth_expired:
                    print("[!] 重连前无法确认服务端任务状态；仅重试 API 后仍不可用，停止重连。")
                    return
                if (
                    current_progress is not None
                    and current_progress >= TASK_PROGRESS_TARGET
                ):
                    print("[*] 重连前确认 currentProgress 已达到 3600，停止网页连接。")
                    headers = current_headers or build_task_api_headers(saved_auth_data, device_code)
                    current_points = fetch_general_points_with_retry(headers)
                    auto_redeem_reward_after_hang(
                        None, headers, running_in_docker, current_points
                    )
                    return
                if current_progress is not None:
                    task_auth_data = saved_auth_data
                if pc_task_pause_requested(running_in_docker):
                    print("[*] 重连前检测到维护暂停标记，取消网页连接。")
                    return
                reconnect_pending = False

            run_browser_task_attempt(
                username=username,
                password=password,
                auth_data_file=auth_data_file,
                device_code=device_code,
                running_in_docker=running_in_docker,
                config_redeem_only=config_redeem_only,
                pause_file=pause_file,
                runtime_deadline=runtime_deadline,
                task_auth_data=task_auth_data,
            )
            return
        except PcMaxRuntimeExpired as error:
            print(f"[*] {error}")
            return
        except PcTaskAuthorizationLost as error:
            print(f"[!] {error}")
            return
        except DesktopConnectionFailure as error:
            automatic_trigger = is_automatic_pc_trigger()
            delay = next_network_reconnect_delay(
                reconnects_used,
                NETWORK_RETRYABLE if automatic_trigger else error.classification,
            )
            if delay is None:
                if automatic_trigger:
                    print(
                        "[!] Cron/INIT_RUN 桌面连接重试次数已达上限（"
                        f"{MAX_DESKTOP_NETWORK_RECONNECTS} 次），任务未完成。"
                    )
                elif error.classification == NETWORK_RETRYABLE:
                    print(
                        "[!] 桌面网络故障重连次数已达上限（"
                        f"{MAX_DESKTOP_NETWORK_RECONNECTS}次），任务结束。"
                    )
                elif error.classification == PREEMPTED:
                    print("[!] 检测到客户端连接冲突，任务立即退出，不重连。")
                else:
                    print("[!] 断线原因 UNKNOWN，保守退出，不重连。")
                sys.exit(1)

            reconnects_used += 1
            reconnect_pending = True
            if automatic_trigger:
                print(
                    f"[*] Cron/INIT_RUN 任务遇到 {error.classification}，"
                    f"第 {reconnects_used}/{MAX_DESKTOP_NETWORK_RECONNECTS} 次重连将在 {delay} 秒后开始。"
                )
            else:
                print(
                    f"[*] 已确认是当前网页桌面连接的网络故障；"
                    f"第 {reconnects_used}/{MAX_DESKTOP_NETWORK_RECONNECTS} 次重连将在 {delay} 秒后开始。"
                )
            # run_browser_task_attempt has quit Chromium before returning here.
            remaining_delay = delay
            while remaining_delay > 0:
                try:
                    ensure_pc_runtime(runtime_deadline)
                except PcMaxRuntimeExpired as runtime_error:
                    print(f"[*] {runtime_error}")
                    return
                except PcTaskAuthorizationLost as authorization_error:
                    print(f"[!] {authorization_error}")
                    return
                if pc_task_pause_requested(running_in_docker):
                    print("[*] 等待重连时检测到维护暂停标记，取消重连。")
                    return
                sleep_for = min(1, remaining_delay)
                time.sleep(sleep_for)
                remaining_delay -= sleep_for
        except BrowserCleanupFailure as error:
            print(f"[!] {error}")
            sys.exit(1)
        except Exception as error:
            print(f"[!] 执行异常: {error}")
            sys.exit(1)


def save_screenshot(page: ChromiumPage) -> None:
    file_name = f"{os.getenv('APP_USER')}_{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    if os.getenv("RUNNING_IN_DOCKER") == "true":
        path = "/app/data"
    else:
        path = "./"
    page.get_screenshot(path=path, name=file_name, full_page=True)


if __name__ == "__main__":
    args = parse_args()
    if not args.precheck_complete:
        print("[*] 开始进行云电脑挂机")
    main(
        config_redeem_only=args.config_redeem,
        precheck_complete=args.precheck_complete,
        redeem_completed_task_only=args.redeem_completed_task,
    )

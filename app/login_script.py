"""
天翼云电脑
"""

import atexit
import datetime
import json
import os
import random
import sys
import threading
import time
from typing import Optional, Union

import ddddocr
from DrissionPage import ChromiumOptions, ChromiumPage

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

PRESET_MESSAGES = [
    "今天北京天气怎么样？（简短回答）",
    "给我讲一个冷笑话。（简短回答）",
    "来一首古诗。（简短回答）",
    "空腹可以吃饭吗？（简短回答）",
    "推荐一部人生必看电影。（简短回答）",
]

CHAT_URL = "https://eaichat.ctyun.cn/chat/#/aichat"
CHAT_INPUT_SELECTORS = (
    "css:textarea[placeholder*='输入']",
    "css:textarea[placeholder*='问题']",
    "css:div.input-box.input-wrap",
    "css:[contenteditable='true'][role='textbox']",
    "css:[contenteditable='true']",
    "css:[role='textbox']",
    "css:textarea",
    "css:input[placeholder*='输入']",
)
CHAT_SEND_SELECTORS = (
    "css:div.send-button",
    "css:button[aria-label*='发送']",
    "css:button[title*='发送']",
    "css:[role='button'][aria-label*='发送']",
    "css:button[type='submit']",
)
CHAT_REPLY_SELECTORS = (
    "css:div.markdown-content",
    "css:[data-role='assistant']",
    "css:[data-message-role='assistant']",
    "css:[class*='markdown']",
    "css:[class*='markdown-content']",
    "css:[class*='answer-content']",
    "css:[class*='assistant-content']",
)


# ==========================================
# Cookie 持久化辅助函数
# ==========================================
def save_cookies(page: ChromiumPage, file_path: str) -> None:
    """获取当前页面的 Cookie 并持久化保存到本地文件。"""
    try:
        # 确保目录存在
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        cookies = page.cookies()
        if not cookies:
            print(f"[-] 抓取到的 Cookie 为空，已取消保存操作: {file_path}")
            return
        # 原因：确保获取到的 Cookie 具备业务层面的真实登录凭证，避免保存无用的访客 Cookie
        has_yl_token = False

        if isinstance(cookies, list):
            has_yl_token = any(cookie.get("name") == "YL-Token" for cookie in cookies)
        elif isinstance(cookies, dict):
            has_yl_token = "YL-Token" in cookies
        if not has_yl_token:
            print(f"[-] Cookie 中缺失关键凭证 'YL-Token'，已取消保存操作: {file_path}")
            return
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=4)
        print(f"[*] Cookie 已成功保存至: {file_path}")

    except Exception as e:
        print(f"[!] 保存 Cookie 失败: {e}")


def load_cookies(page: ChromiumPage, file_path: str) -> bool:
    """从本地文件读取 Cookie 并加载到浏览器中。"""
    if not os.path.exists(file_path):
        print(f"[-] 未发现本地 Cookie 缓存文件: {file_path}")
        return False
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        page.set.cookies(cookies)
        print(f"[*] 本地 Cookie 加载完成: {file_path}")
        return True
    except Exception as e:
        print(f"[!] 加载 Cookie 失败: {e}")
        return False


# ==========================================
# 浏览器初始化与核心功能函数
# ==========================================


def init_browser_options() -> ChromiumOptions:
    """初始化并配置 Chromium 浏览器的启动参数。"""
    options = ChromiumOptions()
    options.set_argument("--no-sandbox")
    options.set_argument("--disable-gpu")
    options.set_argument("--disable-dev-shm-usage")
    options.headless()
    return options


def fill_credentials(page: ChromiumPage, username: str, password: str) -> None:
    """在页面上填写账号与密码信息。"""
    print("正在输入账号信息...")
    account_input = page.ele('css:input[type="text"]')
    account_input.clear()
    account_input.input(username)

    print("正在输入密码信息...")
    password_input = page.ele('css:input[type="password"]')
    password_input.clear()
    password_input.input(password)


def handle_captcha(page: ChromiumPage) -> None:
    """检测页面是否存在图形验证码容器，若存在则提取图片并填充识别结果。"""
    print("正在检测图形验证码容器...")
    captcha_container = page.ele("css:.fgt-capt-ct", timeout=2)

    if not captcha_container:
        print("当前无需处理图形验证码。")
        return

    print("检测到图形验证码，开始提取并识别...")
    pic_ele = captcha_container.ele("css:img")
    pic_bytes = pic_ele.get_screenshot(as_bytes=True)

    ocr_result = get_bytes_numeric_captcha(pic_bytes)
    print(f"OCR 识别结果为: {ocr_result}")

    input_ele = captcha_container.ele('css:input[placeholder="输入图形验证码"]')
    input_ele.clear()
    input_ele.input(ocr_result)


def analyze_login_response(response_body: Union[dict, str, None]) -> int:
    """分析登录接口的返回体，提取并映射为内部状态码。"""
    if not response_body or not isinstance(response_body, dict):
        return 0

    code = response_body.get("code")
    msg = response_body.get("msg", "")

    if code == 51040 and "用户名或密码错误" in msg:
        return 1
    elif code == 51030:
        return 2
    elif code == 51040 and "图形验证码" in msg:
        return 3
    return -1


def save_screenshot(page: ChromiumPage) -> None:
    file_name = f"{os.getenv('APP_USER')}_{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    if os.getenv("RUNNING_IN_DOCKER") == "true":
        path = "/app/data"
    else:
        path = "./"
    page.get_screenshot(path=path, name=file_name, full_page=True)


def execute_login_with_listener(
    page: ChromiumPage,
    target_url: str,
    username: str,
    password: str,
) -> Optional[bool]:
    """执行完整的账号密码登录流程。"""
    print("\n--- 开始账密登录流程 ---")
    print(f"访问登录页面: {target_url}")
    page.get(target_url)
    page.wait.load_start()

    fill_credentials(page, username, password)

    handle_captcha(page)

    if not page.wait.ele_displayed("css:button.lgm-submit-ct", timeout=5):
        raise RuntimeError("页面未渲染出登录按钮")

    # 确认显示后，重新提取元素对象
    login_button = page.ele("css:button.lgm-submit-ct")

    page.listen.start("api/auth/iam/login")
    login_button.click()

    print("已点击登录，等待接口返回...")
    packet = page.listen.wait(timeout=5)
    page.listen.stop()

    if not packet:
        raise RuntimeError("未捕获到登录接口数据包，检查是否已重定向。")

    status_code = analyze_login_response(packet.response.body)

    if status_code == 0:
        print("登录成功")
        return True
    elif status_code == 1:
        print("登录失败：用户名或密码错误。")
        return False
    elif status_code in [2, 3]:
        print(f"登录受阻（状态码 {status_code}），准备重试...")
        time.sleep(1)
        raise RuntimeError("登录受阻，准备重试。")
    else:
        print(f"未知响应: {packet.response.body}")
        return False


def display_user_info(page: ChromiumPage) -> None:
    """
    提取并输出当前登录的用户信息（手机号掩码）。
    """
    user_selector = "css:div.username span.txt"

    try:
        if page.wait.ele_displayed(user_selector, timeout=2):
            username_text = page.ele(user_selector).text
            if username_text:
                print(f"[*] 登录成功，当前登录用户: {username_text}")
                return
    except Exception:
        pass

    # 该元素只用于日志输出，天翼更新页面结构后缺失时不应阻断任务。
    print("[*] 已进入 AI 聊天页面。")


def find_displayed_element(page: ChromiumPage, selectors, timeout: float = 10):
    """依次查找可见元素，兼容新版页面替换输入框或按钮标签的情况。"""
    deadline = time.monotonic() + timeout
    while True:
        for selector in selectors:
            try:
                elements = page.eles(selector, timeout=0.2) or []
            except Exception:
                continue
            for element in elements:
                try:
                    if element.states.is_displayed:
                        return element
                except Exception:
                    continue

        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


def get_chat_replies(page: ChromiumPage) -> list[str]:
    """读取当前页面里可见的 AI 回复文本，优先使用语义明确的容器。"""
    for selector in CHAT_REPLY_SELECTORS:
        try:
            elements = page.eles(selector, timeout=0.2) or []
            texts = []
            for element in elements:
                try:
                    text = (element.text or "").strip()
                    if text and element.states.is_displayed:
                        texts.append(text)
                except Exception:
                    continue
            if texts:
                return texts
        except Exception:
            continue
    return []


def is_chat_reply_generating(page: ChromiumPage) -> bool:
    """新版页面可能会显示停止生成按钮；存在时不要过早判定回复结束。"""
    stop_selectors = (
        "css:button[aria-label*='停止']",
        "css:button[title*='停止']",
        "css:[class*='stop-generat']",
        "css:[class*='stop-button']",
    )
    for selector in stop_selectors:
        try:
            if page.wait.ele_displayed(selector, timeout=0.1):
                return True
        except Exception:
            continue
    return False


def click_chat_send_button(page: ChromiumPage, input_box) -> None:
    """点击发送按钮；在新版按钮没有可识别标签时用 Enter 提交。"""
    button = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        button = find_displayed_element(page, CHAT_SEND_SELECTORS, timeout=0.5)
        if button and button.attr("disabled") is None:
            button.click()
            return
        time.sleep(0.2)

    if not button:
        # 后续页面可能改了按钮 class/标签，按可访问名称和 class 再扫描一次。
        try:
            candidates = page.eles(
                "css:button, [role='button'], [class*='send']", timeout=0.5
            ) or []
        except Exception:
            candidates = []
        for candidate in candidates:
            try:
                label = " ".join(
                    str(value or "")
                    for value in (
                        candidate.text,
                        candidate.attr("aria-label"),
                        candidate.attr("title"),
                        candidate.attr("class"),
                    )
                ).lower()
                if (
                    ("发送" in label or "send" in label)
                    and candidate.states.is_displayed
                    and candidate.attr("disabled") is None
                ):
                    button = candidate
                    break
            except Exception:
                continue

    if button:
        if button.attr("disabled") is not None:
            raise RuntimeError("AI 聊天发送按钮仍处于禁用状态")
        button.click()
        return

    print("[-] 未找到发送按钮，尝试使用 Enter 提交。")
    page.actions.click(input_box).key_down("ENTER").key_up("ENTER")


def chat_and_earn_points(page: ChromiumPage) -> None:
    """在登录成功后，跳转至聊天页面发送预置话语，并通过 DOM 提取稳定文字。"""
    if not (page.url or "").startswith(CHAT_URL.split("#", 1)[0]):
        print(f"\n正在跳转至 AI 聊天页面: {CHAT_URL}")
        page.get(CHAT_URL)

    print("等待聊天输入框加载...")
    input_box = find_displayed_element(page, CHAT_INPUT_SELECTORS, timeout=20)
    if not input_box:
        raise RuntimeError("未找到聊天输入框")

    # 在确认核心页面元素加载完毕后，立刻提取并输出用户信息
    display_user_info(page)
    previous_replies = get_chat_replies(page)
    message = random.choice(PRESET_MESSAGES)
    print(f"准备发送信息: {message}")
    input_box.clear()
    input_box.input(message)
    time.sleep(1)
    click_chat_send_button(page, input_box)
    print("信息已提交，正在等待 AI 回复生成...\n")

    previous_text = ""
    stable_count = 0
    latest_reply = ""
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        current_replies = get_chat_replies(page)
        if current_replies:
            current_text = current_replies[-1]
            is_new_reply = (
                len(current_replies) > len(previous_replies)
                or not previous_replies
                or current_text != previous_replies[-1]
            )
            if is_new_reply and current_text:
                latest_reply = current_text
                if current_text == previous_text:
                    stable_count += 1
                else:
                    previous_text = current_text
                    stable_count = 0

                if stable_count >= 4 and not is_chat_reply_generating(page):
                    break
        time.sleep(1)

    if not latest_reply:
        raise RuntimeError("[!] 已提交消息，但未能定位到 AI 回复。")
    if stable_count < 4:
        raise RuntimeError("[!] AI 回复在等待超时前仍未完成。")

    print("=== AI 助手回复 ===")
    print(latest_reply)
    print("\n===================\n")
    print("[*] 积分任务完成。")


# ==========================================
# 主流程控制
# ==========================================


def main() -> None:
    login_url = (
        "https://desk.ctyun.cn/cloudB/dy/iam/api/auth/iam/cas/login?"
        "service=https%3A%2F%2Feaichat.ctyun.cn%3A443%2Fchat%2F%23%2Faichat&consent=false"
    )

    my_username = os.getenv("APP_USER")
    my_password = os.getenv("APP_PASSWORD")

    if not my_username or not my_password:
        print("错误：未检测到 APP_USER 或 APP_PASSWORD 环境变量。")
        sys.exit(1)

    # 动态构造 Cookie 文件路径，包含手机号
    # 格式：/app/data/ctyun_cookies_xxx_.json
    if os.getenv("RUNNING_IN_DOCKER") == "true":
        cookie_file = f"/app/data/ctyun_cookies_{my_username}_.json"
    else:
        cookie_file = f"./ctyun_cookies_{my_username}_.json"

    browser_options = init_browser_options()
    page = ChromiumPage(addr_or_opts=browser_options)
    atexit.register(page.quit)
    attempt = 0
    max_retries = 3
    while attempt < max_retries:
        print(f"--- 对话尝试: {attempt + 1}/{max_retries} ---")
        try:
            is_logged_in = False

            # === 使用动态路径进行持久化验证 ===
            print(f"正在建立域名上下文环境，准备使用账号 {my_username} 的缓存...")
            page.get(CHAT_URL)
            time.sleep(1)

            if attempt <= 0:
                if load_cookies(page, cookie_file):
                    print("正在验证 Cookie 是否有效...")
                    page.get(CHAT_URL)
                    if find_displayed_element(page, CHAT_INPUT_SELECTORS, timeout=10):
                        print(f"[*] 账号 {my_username} 免密登录成功！")
                        is_logged_in = True
                    else:
                        print("[-] Cookie 已失效，准备进行账密登录...")

            # === 登录流程 (如果 Cookie 无效) ===
            if not is_logged_in:
                is_success = execute_login_with_listener(
                    page, login_url, my_username, my_password
                )
                if is_success:
                    # 登录成功后保存到对应手机号的文件中
                    time.sleep(5)
                    save_cookies(page, cookie_file)
                    is_logged_in = True
                else:
                    print("[!] 自动化登录未能成功执行。")
                    sys.exit(1)

            # === 执行互动获取积分 ===
            if is_logged_in:
                chat_and_earn_points(page)
                print("\n对话任务已完成")
            break

        except Exception as e:
            attempt += 1
            print(f"[!] 执行过程中发生异常: {e}")
            time.sleep(5)

    if attempt >= max_retries:
        print("[!] AI 对话任务重试次数已用尽。")
        sys.exit(1)


# ==========================================
# OCR 模块封装
# ==========================================


class NumericOcrSolver:
    """使用单例模式封装的数字 OCR 识别器。"""

    _instance: Optional["NumericOcrSolver"] = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls) -> "NumericOcrSolver":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init_engine()
        return cls._instance

    def _init_engine(self) -> None:
        self.ocr = ddddocr.DdddOcr(show_ad=False)
        self.ocr.set_ranges(0)

    def solve(self, image_data: bytes) -> str:
        try:
            return self.ocr.classification(image_data)
        except Exception as e:
            return f"Error: {str(e)}"


def get_bytes_numeric_captcha(image_bytes: bytes) -> str:
    solver = NumericOcrSolver()
    return solver.solve(image_bytes)


if __name__ == "__main__":
    main()

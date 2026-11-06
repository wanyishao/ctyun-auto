# 1. 使用指定的 Ctyun 镜像作为基础镜像
FROM su3817807/ctyun:latest

# 2. 切换到 root 用户以安装系统级依赖
USER root

# 3. 设置时区为亚洲/上海，确保 Cron 定时任务按北京时间执行
ENV TZ=Asia/Shanghai
ENV RUNNING_IN_DOCKER=true
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 4. 安装 Python3、包管理器、Chromium、中文字体、Cron 以及图像处理底层库
RUN apt-get update && apt-get install --no-install-recommends -y \
    python3 \
    python3-pip \
    chromium \
    fonts-wqy-zenhei \
    cron \
    util-linux \
    libgl1 \
    libglib2.0-0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 5. 安装 Python 依赖
RUN pip3 install --no-cache-dir DrissionPage==4.0.4.19 ddddocr --break-system-packages

# 6. 将 Python 脚本和启动脚本复制到工作目录
COPY login_script.py /app/login_script.py
COPY pc_login.py /app/pc_login.py
COPY desktop_diagnostics.py /app/desktop_diagnostics.py
COPY task_progress.py /app/task_progress.py
COPY task_processes.py /app/task_processes.py
COPY redeem_config.json /app/redeem_config.json
COPY task_guard.sh /app/task_guard.sh
COPY run_task.sh /app/run_task.sh
COPY entrypoint.sh /app/entrypoint.sh

# 7. 修复 Windows 环境下可能产生的 CRLF 换行符问题，并赋予执行权限
RUN python3 -c 'from pathlib import Path; files = [Path(p) for p in ("/app/entrypoint.sh", "/app/run_task.sh", "/app/task_guard.sh")]; [p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n")) for p in files]' \
    && chmod +x /app/entrypoint.sh /app/run_task.sh /app/task_guard.sh

# 8. 使用我们的双进程启动脚本接管入口
ENTRYPOINT ["/app/entrypoint.sh"]

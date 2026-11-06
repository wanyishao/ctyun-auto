# 天翼云电脑保活与自动获取积分

本项目用于在 Docker 容器中保活天翼云电脑使其长期开机（保活不会中断正常使用），并自动完成每日积分任务（每天可获取 300 积分）。

相比于原版，当前 Fork 版本进行了**彻底的容器化重构**。抛弃了原有的宿主机部署脚本，全面采用 `docker-compose` 结合环境变量进行动态配置，并通过 GitHub Actions 自动构建并托管镜像至GitHub。

## ✨ 核心特性

- 🐳 **纯净部署**：无需在宿主机克隆仓库或运行任何 Bash 脚本，仅需一个 `docker-compose.yml` 即可一键启动。
- ⚙️ **动态配置**：使用环境变量灵活配置账号、密码以及 Cron 定时任务频率。
- 🤖 **自动任务**：全自动执行 AI 对话积分任务与云电脑挂机积分任务。
- ☁️ **多架构镜像**：GitHub Actions 自动构建并推送 `linux/amd64` 与 `linux/arm64` 镜像。

## 🚀 快速开始

### 1. 准备环境

请确保您的服务器已安装 [Docker](https://docs.docker.com/engine/install/) 和 [Docker Compose](https://docs.docker.com/compose/install/)。

### 2. 下载配置文件

创建一个空目录并下载 `docker-compose.yml` 文件：

```bash
mkdir ctyun-auto && cd ctyun-auto
wget https://raw.githubusercontent.com/wanyishao/ctyun-auto/main/docker-compose.yml

```

### 3. 配置账号与任务时间

打开 `docker-compose.yml`，在 `environment` 中填写天翼云账号和密码，并按需调整任务时间：

```yaml
    environment:
      APP_USER: "您的手机号"
      APP_PASSWORD: "您的密码"
      CRON_LOGIN: "0 3,20 * * *"
      CRON_PC: "0 4,6 * * *"
      PC_MAX_RUNTIME_SECONDS: "7200"
      INIT_RUN: "false"

```

### 4. 启动容器

直接在后台启动容器：

```bash
docker-compose pull
docker-compose up -d

```

> **首次运行风控提醒**：如果账号触发了短信验证码风控，请通过 `docker logs -f ctyun_sign_auto` 查看日志，可能需要您暂时使用交互模式进入容器手动处理。

## 🛠️ 进阶操作与维护

### 配置自动兑换奖励

容器正常运行后，如果您希望积攒的积分能自动兑换指定的云电脑配置时长或奖励，请在宿主机执行以下命令进入交互配置引导：

```bash
docker exec -it ctyun-auto /app/run_task.sh pc /usr/bin/python3 -u /app/pc_login.py --config-redeem

```

根据终端提示，依次选择“要应用配置的设备”、“兑换的商品”以及“兑换策略”（推荐选择按每月特定日期兑换）。
镜像已包含`/app/redeem_config.json`，默认内容为禁用自动兑换，因此容器首次启动不会缺少配置。若要在容器重建后保留交互配置，请在宿主机的`data`目录创建`redeem_config.json`，并在`docker-compose.yml`中启用可选挂载：

```yaml
    volumes:
      - ./data:/app/data
      - ./data/redeem_config.json:/app/redeem_config.json

```

### 常用管理命令

```bash
# 查看实时运行日志
docker-compose logs -f

# 停止容器
docker-compose stop

# 重启容器
docker-compose restart

# 更新镜像并重新创建容器 (当远端有更新时)
docker-compose pull
docker-compose up -d

```

### 云电脑挂机与正在使用的客户端

`CRON_PC` 配置的时间应是用户通常不使用客户端的空闲时段。任务统一经 `/app/run_task.sh` 启动：守护进程先完整停止 CtYun.dll 并写入授权确认，之后网页才允许建立远程桌面连接；网页任务、Chromium 和子进程退出后才恢复 DLL。客户端是否在线不通过积分进度推测。

`PC_MAX_RUNTIME_SECONDS` 控制一次网页任务的最长运行时间，默认 7200 秒。人工维护暂停标记仍可选用：创建 `./data/.pause_pc_login` 可阻止新任务并请求正在运行的网页任务安全退出；维护完成后删除该文件即可恢复任务。没有标记时无需手动操作。

自动挂机按 `CRON_PC` 配置的计划时间触发。将 `INIT_RUN=true` 可在容器启动时立即各执行一次 AI 对话和云电脑挂机；默认值为 `false`。Cron 和 `INIT_RUN` 触发的挂机遇到可信网络故障、挤占、`UNKNOWN` 断线或浏览器流程异常时，都会在关闭旧 Chromium、复查完成进度后最多重连 3 次，间隔 10、30、60 秒；重连时若进度 API 明确返回登录态过期，自动任务会在剩余重试预算内继续并由网页登录流程刷新会话。点击进入桌面后，若网页显示“正在重连”，先保留当前连接最多 45 秒，给网页自身恢复时间；仍无画布时再关闭旧浏览器并进入有限重试。自动模式的页面导航单次限制为 15 秒，桌面列表等待限制为 30 秒。手动任务只对可信桌面网络故障重连。仅服务端 `currentProgress >= 3600` 视为挂机完成，每次任务最长运行时间由 `PC_MAX_RUNTIME_SECONDS` 限制，默认 7200 秒。

启动网页任务前会先用已保存的登录态查询 `currentProgress`；服务端已达到 `3600` 时直接处理现有积分兑换配置，不暂停 DLL 或启动浏览器。若任务进程身份、浏览器进程组或 DLL 停止状态无法确认，系统会保留 `active` 或 `data/.ctyun_dll_unsafe` 标记并拒绝新的连接。此类标记需要先检查对应进程确已退出，再由维护者清理；程序不会自行覆盖或解除 unsafe 状态。

需要立即手动挂机时，使用与 Cron 相同的安全入口：

```bash
docker exec -d ctyun-auto /app/run_task.sh pc /usr/bin/python3 -u /app/pc_login.py
```

可查看任务窗口是否占用：

```bash
docker exec ctyun-auto sh -c 'if [ -f /tmp/ctyun_pc_task_active ]; then echo "自动挂机运行中"; else echo "自动挂机未运行"; fi'
```

如果进程清理无法确认，守护进程会保持 DLL 停止并拒绝新的网页连接；此时先检查日志与进程，再由维护人员清理 `/tmp/ctyun_pc_task_active`。

该状态只表示自动挂机进程，不代表用户客户端在线状态。Cron、手动挂机、`INIT_RUN` 和兑换配置入口共用任务锁与 DLL 交接。

## 📂 数据持久化说明

配置的 `./data` 目录会被映射到容器内的 `/app/data`。该目录会自动存储以下信息：

* `ctyun_authData_*.json` / `ctyun_cookies_*.json`：登录凭证缓存（避免频繁账密登录触发风控）。
* `.devicecode_*`：设备标识码。
* 错误时的截图留存（方便排查问题）。

桌面断线诊断保存在 `data/desktop-diagnostics`：目录权限为 `0700`，文件权限为 `0600`；默认最多保留 40 个 JSON/截图文件，并在新诊断写入时清理超过 7 天的文件。可通过 `PC_DIAGNOSTIC_MAX_FILES` 和 `PC_DIAGNOSTIC_MAX_AGE_DAYS` 调整上限与保留期。

请妥善保管 `data` 目录中的文件。

## 鸣谢与来源说明

* 本项目使用的基础保活程序来源于 [leleji/CtYun](https://github.com/leleji/CtYun)。
* 验证码识别 API 方案采用 [ddddocr](https://github.com/sml2h3/ddddocr)。
* 原自动化脚本逻辑基于 [liuzhijie443/ctyun-auto](https://github.com/liuzhijie443/ctyun-auto) 修改。

# 天翼云电脑保活与自动获取积分

本项目用于在 Docker 容器中保活天翼云电脑使其长期开机（保活不会中断正常使用），并自动完成每日积分任务（每天可获取 300 积分）。

相比于原版，当前 Fork 版本进行了**彻底的容器化重构**。抛弃了原有的宿主机部署脚本，全面采用 `docker-compose` 结合环境变量进行动态配置，并通过 GitHub Actions 自动构建并托管镜像至GitHub。

## ✨ 核心特性

- 🐳 **纯净部署**：无需在宿主机克隆仓库或运行任何 Bash 脚本，仅需一个 `docker-compose.yml` 即可一键启动。
- ⚙️ **动态配置**：使用环境变量灵活配置账号、密码以及 Cron 定时任务频率。
- 🤖 **自动任务**：全自动执行 AI 对话积分任务与云电脑挂机积分任务。
- ☁️ **云端构建**：源码修改后通过 GitHub Actions 自动构建最新镜像并推送到 GHCR。

## 🚀 快速开始

### 1. 准备环境

请确保您的服务器已安装 [Docker](https://docs.docker.com/engine/install/) 和 [Docker Compose](https://docs.docker.com/compose/install/)。

### 2. 下载配置文件

创建一个空目录并下载 `docker-compose.yml` 文件：

```bash
mkdir ctyun-auto && cd ctyun-auto
wget https://raw.githubusercontent.com/wanyishao/ctyun-auto/main/docker-compose.yml

```

### 3. 修改配置

使用您喜欢的编辑器打开 `docker-compose.yml`，修改 `environment` 下的环境变量，填入您的天翼云账号和密码：

```yaml
    environment:
      - APP_USER=您的手机号
      - APP_PASSWORD=您的密码
      # 默认定时任务配置，可根据需要修改
      - CRON_LOGIN=0 3,20 * * *
      # AI 对话任务：每天 03:00 和 20:00 执行
      - CRON_PC=0 4,6 * * *
      # 挂机任务：每天 04:00 和 06:00 执行
      - INIT_RUN=false
      # 若设为 true，容器首次启动时会无视 cron 立即执行一次任务

```

### 4. 启动容器

直接在后台启动容器：

```bash
docker-compose up -d

```

> **首次运行风控提醒**：如果账号触发了短信验证码风控，请通过 `docker logs -f ctyun_sign_auto` 查看日志，可能需要您暂时使用交互模式进入容器手动处理。

## 🛠️ 进阶操作与维护

### 配置自动兑换奖励

容器正常运行后，如果您希望积攒的积分能自动兑换指定的云电脑配置时长或奖励，请在宿主机执行以下命令进入交互配置引导：

```bash
docker exec -it ctyun-auto python3 /app/pc_login.py --config-redeem

```

根据终端提示，依次选择“要应用配置的设备”、“兑换的商品”以及“兑换策略”（推荐选择按每月特定日期兑换）。
配置文件位于`/app/redeem_config.json`，默认不启用，若需要持久化自动兑换配置，在本地创建`redeem_config.json`文件，并修改`docker-compose.yml`文件，添加`- ./data/redeem_config.json:/app/redeem_config.json`。

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

## 📂 数据持久化说明

配置的 `./data` 目录会被映射到容器内的 `/app/data`。该目录会自动存储以下信息：

* `ctyun_authData_*.json` / `ctyun_cookies_*.json`：登录凭证缓存（避免频繁账密登录触发风控）。
* `.devicecode_*`：设备标识码。
* 错误时的截图留存（方便排查问题）。

请妥善保管 `data` 目录中的文件。

## 鸣谢与来源说明

* 本项目使用的基础保活程序来源于 [leleji/CtYun](https://github.com/leleji/CtYun)。
* 验证码识别 API 方案采用 [ddddocr](https://github.com/sml2h3/ddddocr)。
* 原自动化脚本逻辑基于 [liuzhijie443/ctyun-auto](https://github.com/liuzhijie443/ctyun-auto) 修改。

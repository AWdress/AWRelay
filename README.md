<div align="center">
  <img src="logo.png" alt="AWRelay" width="180">

  <h1>AWRelay</h1>

  <p>轻量、自托管的 Telegram 私聊消息中转机器人</p>
</div>

---

## 简介

AWRelay 是一个基于 [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) 的私聊转发机器人。用户私聊机器人发送的消息会被转发到一个开启了「话题」功能的 Telegram 超级群组，每位用户对应群组中的一个独立话题。管理员在话题内直接发送消息即可将内容回传给对应用户，无需其他操作。

转发基于 `copy_message` 实现，消息不显示「转发自」标签，不受用户隐私设置影响。机器人内置人机验证、广告关键词过滤、黑名单与限流，可在运行时通过控制面板动态调整。

## 功能特性

- 话题式收件箱：每位用户在群组中对应一个独立话题，话题以用户名命名，首条消息包含用户 ID 与用户名，方便识别。
- 双向通信：用户私聊消息转发到对应话题；管理员在话题内直接发消息即可回传给该用户，无需回复特定消息。
- 人机验证：陌生用户首次私聊需完成数学题验证，验证状态持久化保存，可在控制面板开关。
- 广告过滤：基于关键词命中拦截垃圾消息，关键词列表支持在控制面板动态增删。
- 黑名单：在话题中回复某条转发消息执行 `/ban` 即可拉黑对应用户。
- 限流保护：针对单个用户采用滑动窗口限流，防止短时间刷屏。
- 相册聚合：用户一次发送的多张图片会被聚合为一组整体转发。
- 启动提醒：机器人上线后向群组发送一条包含运行信息的通知。
- 自动清理：定期清理过期消息映射，控制数据库体积。
- 单实例锁：防止同一 Token 被多个进程同时轮询导致消息重复。

## 群组要求

话题模式需要一个满足以下条件的 Telegram 群组：

1. 必须是**超级群组**（Supergroup）
2. 群组设置中开启「话题」（Topics）功能
3. bot 已加入群组并被设置为**管理员**，且勾选「管理话题」权限

## 管理员指令

以下指令在群组内使用：

| 指令 | 说明 |
| --- | --- |
| `/menu` | 打开控制面板，管理广告拦截、人机验证开关与关键词列表 |
| `/ban` | 回复某条转发消息以拉黑该用户 |
| `/unban` | 回复某条转发消息以解除拉黑 |
| `/cancel` | 取消当前正在进行的关键词输入操作 |
| `/help` | 显示帮助信息 |

在用户对应的话题内直接发送消息（无需回复），bot 会将内容转发给该用户。

## 配置项

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `BOT_TOKEN` | 是 | 机器人的 API Token，从 [@BotFather](https://t.me/BotFather) 获取 |
| `ADMIN_ID` | 是 | 管理员本人的 Telegram 数字 ID（用于指令权限校验），从 [@userinfobot](https://t.me/userinfobot) 获取 |
| `GROUP_ID` | 是 | 话题群组的 Chat ID（负数，如 `-1001234567890`），bot 加入群组后可通过 [@userinfobot](https://t.me/userinfobot) 在群内发消息获取 |
| `PROXY_URL` | 否 | 代理地址，支持 `http://` 与 `socks5://`，能直连 Telegram 时留空 |

## 部署方式

### Docker Compose（推荐）

仓库提供 `docker-compose.yml`，通过 GitHub Actions 自动构建并发布多架构镜像（`linux/amd64` 与 `linux/arm64`）至 Docker Hub。

编辑 `docker-compose.yml`，将 `environment` 中的占位值替换为实际配置：

```yaml
environment:
  - BOT_TOKEN=你的BotToken
  - ADMIN_ID=你的Telegram数字ID
  - GROUP_ID=-1001234567890
  - PROXY_URL=
```

拉取镜像并启动：

```bash
docker compose pull
docker compose up -d
docker compose logs -f
```

数据库与日志持久化在挂载目录（默认 `/data/AWRelay/data`），容器重建不会丢失数据。

### 本地运行

```bash
pip install -r requirements.txt
cp .env.example .env   # 填入 BOT_TOKEN、ADMIN_ID 和 GROUP_ID
python main.py
```

## 关于代理

容器内 `127.0.0.1` 指向容器自身而非宿主机，需注意：

- Linux 服务器：`docker-compose.yml` 已采用 `network_mode: host`，可直接填写 `http://127.0.0.1:7890`。
- Docker Desktop（Windows / macOS）：`host` 网络模式不生效，改用 `http://host.docker.internal:7890`。
- 本地运行：直接填写本机代理地址即可。

## 关于图标

`logo.png` 可作为机器人头像。Telegram 不支持通过 API 设置头像，请在 [@BotFather](https://t.me/BotFather) 中对机器人执行 `/setuserpic` 手动上传。

## 项目结构

```
main.py             机器人主逻辑、消息处理与指令注册
database.py         SQLite 数据访问层，采用 WAL 模式与单连接加线程锁
entrypoint.sh       容器启动脚本，校正数据目录权限后降权运行
Dockerfile          镜像构建定义，以非特权用户运行
docker-compose.yml  容器编排配置
requirements.txt    Python 依赖清单
.env.example        本地运行的配置模板
logo.png            项目图标
```

## 许可证

本项目以仓库根目录的许可证文件为准。

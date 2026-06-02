# AWRelay

一个轻量的 Telegram 私聊转发机器人：用户私聊机器人 → 自动转发给管理员；管理员**回复**该消息 → 自动回传给用户。支持黑名单、广告关键词过滤、人机验证与限流。

## 功能

- 🔁 **双向转发**：用户消息转发给管理员，管理员回复回传用户（基于 `copy_message`，不显示"转发自"，且不受隐私设置限制）
- 🤖 **人机验证**：陌生用户首次私聊需完成一道数学题（内联按钮）才能放行，验证状态持久化，可在面板开关
- 🛡️ **广告过滤**：基于关键词拦截，关键词可在面板动态增删
- 🚫 **黑名单**：回复某条转发消息 `/ban` 即可拉黑发送者
- ⏳ **限流**：单用户滑动窗口限流，防止刷屏
- 🖼️ **相册聚合**：用户一次发送多张图片只发一次来源提示，整组一并转发
- 🧹 **自动清理**：定期清理过期消息映射，控制数据库体积

## 管理员指令

| 指令 | 说明 |
| --- | --- |
| `/menu` | 打开控制面板（广告拦截 / 人机验证 / 关键词管理） |
| `/ban` | 回复某条转发消息以拉黑该用户 |
| `/unban` | 回复某条转发消息以解除拉黑 |
| `/cancel` | 取消当前的关键词输入状态 |
| `/help` | 显示帮助 |

回复用户：在管理员对话中，直接**回复**收到的转发消息即可将内容回传给对应用户。

## 配置

复制 `.env.example` 为 `.env` 并填写：

```bash
cp .env.example .env
```

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `BOT_TOKEN` | 是 | 从 [@BotFather](https://t.me/BotFather) 获取 |
| `ADMIN_ID` | 是 | 管理员的 Telegram 数字 ID（从 [@userinfobot](https://t.me/userinfobot) 获取） |
| `PROXY_URL` | 否 | 代理地址，支持 `http://` / `socks5://` |

> ⚠️ `.env` 已被 `.gitignore` 与 `.dockerignore` 排除，切勿提交或打包进镜像。

## 运行

### 本地运行

```bash
pip install -r requirements.txt
python main.py
```

### Docker Compose（推荐）

```bash
# 确保已创建并填好 .env
docker compose pull           # 拉取最新镜像
docker compose up -d          # 启动
docker compose logs -f        # 查看日志
docker compose down           # 停止
```

数据（数据库 + 日志）持久化在 `./data` 目录。

> 代理提示：容器内 `PROXY_URL` 不能填 `127.0.0.1`。Docker Desktop 用 `host.docker.internal`，Linux 用宿主机局域网 IP。详见 `.env.example`。

## 项目结构

```
main.py             # 机器人主逻辑与 handler 注册
database.py         # SQLite 数据访问层（WAL + 单连接 + 线程锁）
requirements.txt    # 依赖
Dockerfile          # 镜像构建（非 root 运行）
docker-compose.yml  # 编排
.env.example        # 配置模板
```

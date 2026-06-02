#!/bin/sh
set -e

# 宿主机 bind mount（./data:/app/data）默认属主为 root，容器内非 root 用户无写权限，
# 会导致写日志 / SQLite 数据库时报 PermissionError。
# 这里以 root 启动，先确保数据目录存在并修正属主，再降权到 appuser 运行，使镜像开箱即用。
if [ "$(id -u)" = "0" ]; then
    # 即使宿主机挂载的目录为空或结构异常，也先把所需目录建好（应用启动时会自动创建日志/数据库文件）
    mkdir -p /app/data
    chown -R appuser:appuser /app/data 2>/dev/null || true
    exec gosu appuser "$@"
fi

# 非 root 启动（如 compose 指定了 user）则直接运行
exec "$@"

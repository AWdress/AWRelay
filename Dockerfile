FROM python:3.11-slim

WORKDIR /app

# 设置时区为亚洲/上海，并让 Python 日志实时输出（不缓冲），便于 docker logs 查看
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# slim 镜像默认不含时区数据，需安装 tzdata，否则日志时间会是 UTC
# gosu 用于在 entrypoint 中安全地从 root 降权到 appuser
RUN apt-get update && apt-get install -y --no-install-recommends tzdata gosu \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 创建运行用户并准备数据目录
RUN useradd -m appuser && mkdir -p /app/data && chown -R appuser:appuser /app \
    && chmod +x /app/entrypoint.sh

# 数据库文件持久化目录
VOLUME /app/data

# 以 root 启动 entrypoint，运行时修正 bind mount 数据目录属主后降权到 appuser，
# 这样无论宿主机 ./data 属主是谁，镜像都能开箱即用。
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "main.py"]

FROM python:3.11-slim

WORKDIR /app

# 设置时区为亚洲/上海，并让 Python 日志实时输出（不缓冲），便于 docker logs 查看
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# slim 镜像默认不含时区数据，需安装 tzdata，否则日志时间会是 UTC
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 创建非 root 用户运行，并赋予数据目录写权限
# 注意：若部署到 Linux 服务器且 ./data 属主为 root，需 chown 宿主目录给 UID 1000，否则容器内无法写入
RUN useradd -m appuser && mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser

# 数据库文件持久化目录
VOLUME /app/data

CMD ["python", "main.py"]

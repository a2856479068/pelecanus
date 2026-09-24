FROM python:3.13-slim

WORKDIR /app

# PORT 可由 Zeabur 等托管平台覆盖；服务会始终监听容器所有网络接口。
ENV HOST=0.0.0.0 \
    PORT=8765 \
    DATA_DIR=/data \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY app/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/server.py app/codex_runner.py app/local_runtime.py app/entrypoint.py ./
COPY app/web ./web

# 显式建组：useradd 只认 --uid，组号会另外分配（10001 超出系统组区间，实际会落到 101），
# 那样镜像里的 chown 和入口脚本的 chown 就会给同一个目录两个不同的组。
RUN groupadd --system --gid 10001 monitor \
    && useradd --system --uid 10001 --gid 10001 monitor \
    && mkdir /data \
    && chown monitor:monitor /data

# 入口脚本需要 root 权限处理托管平台挂载的持久卷；它会在启动服务前降权到 monitor。
USER root

VOLUME ["/data"]
EXPOSE 8765

# 平台把持久卷挂到 /data 时会带来自己的属主，入口脚本负责在必要时修好它再降权。
ENTRYPOINT ["python", "-u", "/app/entrypoint.py"]
CMD ["python", "-u", "server.py"]

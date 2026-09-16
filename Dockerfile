FROM python:3.13-slim

WORKDIR /app

# PORT 可由 Zeabur 等托管平台覆盖；服务会始终监听容器所有网络接口。
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    HOST=0.0.0.0 \
    PORT=8765 \
    DATA_DIR=/data \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY app/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /opt/playwright

COPY app/server.py app/visual_review.py app/render_frames.py app/entrypoint.py ./
COPY app/web ./web

RUN useradd --system --uid 10001 monitor \
    && mkdir /data \
    && chown monitor:monitor /data

USER monitor

VOLUME ["/data"]
EXPOSE 8765

# 平台把持久卷挂到 /data 时会带来自己的属主，入口脚本负责在必要时修好它再降权。
ENTRYPOINT ["python", "-u", "entrypoint.py"]
CMD ["python", "-u", "server.py"]

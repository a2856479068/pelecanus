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

COPY app/server.py app/visual_review.py app/render_frames.py ./
COPY app/web ./web

RUN useradd --system --uid 10001 monitor \
    && mkdir /data \
    && chown monitor:monitor /data

USER monitor

VOLUME ["/data"]
EXPOSE 8765

CMD ["python", "-u", "server.py"]

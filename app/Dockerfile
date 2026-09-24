FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py codex_runner.py local_runtime.py ./
COPY web ./web
RUN useradd --system --uid 10001 monitor && mkdir /data && chown monitor:monitor /data
USER monitor
ENV HOST=0.0.0.0 PORT=8765 DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8765
CMD ["python", "-u", "server.py"]

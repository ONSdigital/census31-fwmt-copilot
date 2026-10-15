FROM dev-workspace:base

COPY --from=ghcr.io/jdx/mise:2026.10.4 /usr/local/bin/mise /usr/local/bin/mise
COPY agent-workspace/task-queue/task_queue.py /app/task-queue/task_queue.py
COPY agent-workspace/worker/worker.py /app/worker/worker.py
COPY agent-workspace/api/app.py /app/api/app.py
COPY agent-workspace/api/command_processor.py /app/api/command_processor.py
COPY agent-workspace/api/requirements.txt /app/api/requirements.txt

ENV MISE_DATA_DIR=/mise \
    MISE_CACHE_DIR=/mise/cache \
    PATH="/opt/agent-api/bin:/mise/shims:$PATH" \
    PYTHONPATH=/app:/app/task-queue

RUN microdnf install -y python3-pip \
    && microdnf clean all \
    && python3 -m venv /opt/agent-api \
    && /opt/agent-api/bin/pip install --no-cache-dir -r /app/api/requirements.txt
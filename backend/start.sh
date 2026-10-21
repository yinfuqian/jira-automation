#!/bin/bash
# start 脚本负责后端或客户端的启动、构建与运行环境准备。
if [ -d ".venv" ]; then
  source .venv/bin/activate
elif [ -d "venv" ]; then
  source venv/bin/activate
fi
gunicorn \
  -w "${WEB_CONCURRENCY:-4}" \
  -k "${GUNICORN_WORKER_CLASS:-gthread}" \
  --threads "${WEB_THREADS:-8}" \
  --timeout "${GUNICORN_TIMEOUT:-300}" \
  --bind "0.0.0.0:${API_PORT:-5000}" \
  'app:create_app()'

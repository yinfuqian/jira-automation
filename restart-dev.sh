#!/bin/bash
set -euo pipefail

# 开发环境重启：使用 docker-compose-dev.yaml 和 .env，默认重建 API、Celery Worker 与 Beat。
# 必须用 up --force-recreate 而不是 compose restart：restart 不会重新读取 .env。
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose-dev.yaml}"
ENV_FILE="${ENV_FILE:-.env}"
export COMPOSE_FILE ENV_FILE
SERVICES=("$@")

if [ "${#SERVICES[@]}" -eq 0 ]; then
  SERVICES=(api worker beat)
fi

docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d --force-recreate "${SERVICES[@]}"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps "${SERVICES[@]}"

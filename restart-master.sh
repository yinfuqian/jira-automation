#!/bin/bash
set -euo pipefail

# Master 生产环境重启：使用 docker-compose-prod.yaml 和 .env.production，默认重建 API、Celery Worker 与 Beat。
# 必须用 up --force-recreate 而不是 compose restart：restart 不会重新读取 .env.production。
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose-prod.yaml}"
ENV_FILE="${ENV_FILE:-.env.production}"
export COMPOSE_FILE ENV_FILE
SERVICES=("$@")

if [ "${#SERVICES[@]}" -eq 0 ]; then
  SERVICES=(api worker beat)
fi

docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d --force-recreate "${SERVICES[@]}"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps "${SERVICES[@]}"

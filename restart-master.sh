#!/bin/bash
set -euo pipefail

# 兼容入口：Master 生产环境重启，等价于 DEPLOY_ENV=prod bash restart.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export DEPLOY_ENV=prod
exec bash "${SCRIPT_DIR}/restart.sh" "$@"

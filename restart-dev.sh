#!/bin/bash
set -euo pipefail

# 兼容入口：开发环境重启，等价于 DEPLOY_ENV=dev bash restart.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export DEPLOY_ENV=dev
exec bash "${SCRIPT_DIR}/restart.sh" "$@"

#!/usr/bin/env bash
#
# 把「需求评审」技能装到用户级技能目录，装完后任何项目里都能用。
#
# 用法：
#   bash install.sh              # 安装（已存在时提示，不覆盖）
#   bash install.sh --force      # 覆盖已安装的版本
#   bash install.sh --dry-run    # 只打印将要做什么
#
# 只做两件事：拷贝技能目录、拷贝斜杠命令。不碰 settings.json，不碰任何凭据。

set -euo pipefail

FORCE=0
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '3,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "无法识别的参数：$arg（可用：--force / --dry-run / --help）" >&2
      exit 2
      ;;
  esac
done

SKILL_NAME="review-jira-req"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 命令文件随技能一起打包，技能目录被单独拷走后仍能装上斜杠命令。
COMMAND_SOURCE="${SOURCE_DIR}/command/${SKILL_NAME}.md"

CLAUDE_HOME="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SKILL_DEST="${CLAUDE_HOME}/skills/${SKILL_NAME}"
COMMAND_DEST="${CLAUDE_HOME}/commands/${SKILL_NAME}.md"

say() { printf '%s\n' "$*"; }
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    say "  [dry-run] $*"
  else
    "$@"
  fi
}

# ---- 前置检查 ----------------------------------------------------------------

if ! command -v node >/dev/null 2>&1; then
  say "缺少 node。本技能的脚本需要 Node.js >= 20。"
  exit 1
fi

NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
if [ "$NODE_MAJOR" -lt 20 ]; then
  say "Node.js 版本过低：当前 $(node -v)，需要 >= 20。"
  exit 1
fi

if [ ! -f "${SOURCE_DIR}/SKILL.md" ]; then
  say "在 ${SOURCE_DIR} 下找不到 SKILL.md，安装包不完整。"
  exit 1
fi

if [ ! -d "${SOURCE_DIR}/bin" ]; then
  say "在 ${SOURCE_DIR} 下找不到 bin/，安装包不完整。"
  exit 1
fi

if [ -e "$SKILL_DEST" ] && [ "$FORCE" -ne 1 ]; then
  say "已存在 ${SKILL_DEST}。"
  say "要覆盖请加 --force，或先手动备份。"
  exit 1
fi

# ---- 安装 --------------------------------------------------------------------

say "Node $(node -v) OK"
say "安装技能：${SKILL_DEST}"
run mkdir -p "$(dirname "$SKILL_DEST")"
if [ "$FORCE" -eq 1 ] && [ -e "$SKILL_DEST" ]; then
  run rm -rf "$SKILL_DEST"
fi
run cp -R "$SOURCE_DIR" "$SKILL_DEST"

# 包内可能带有构建期的杂项，拷完清掉，避免装进用户目录
run find "$SKILL_DEST" -name '.DS_Store' -delete 2>/dev/null || true

if [ -f "$COMMAND_SOURCE" ]; then
  say "安装斜杠命令：${COMMAND_DEST}"
  run mkdir -p "$(dirname "$COMMAND_DEST")"
  run cp "$COMMAND_SOURCE" "$COMMAND_DEST"
else
  say "未找到命令文件 ${COMMAND_SOURCE}，跳过斜杠命令（技能本身仍可用）。"
fi

# ---- 收尾 --------------------------------------------------------------------

cat <<EOF

安装完成。

下一步：
  1) 设置 JIRA 凭据（只放环境变量，本工具不会写进任何文件）：
       export JIRA_BASE_URL="https://jira.example.com"
       export JIRA_PAT="<你的 Personal Access Token>"
     建议写进 shell 的 rc 文件，或用一个单独的 .env 在你自己的 shell 里 source。

  2) 在任意项目目录里直接要求评审即可，例如：
       「用 review-jira-req 评审 SHOP-101」
     或使用斜杠命令：
       /review-jira-req SHOP-101

  3) 没有真实 JIRA 想先试跑：
       node "${CLAUDE_HOME}/skills/${SKILL_NAME}/bin/mock-jira.mjs" --print-env

产物会落在你执行时所在项目的 reports/ 目录下，建议把 reports/ 加进 .gitignore。
EOF

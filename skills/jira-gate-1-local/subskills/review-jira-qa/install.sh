#!/usr/bin/env bash
#
# 安装 qa-risk-analysis 技能与三个斜杠命令到 Claude Code 用户目录。
#
# 用法：
#   bash install.sh              # 安装；目标已存在时拒绝
#   bash install.sh --force      # 覆盖技能目录与三个命令
#   bash install.sh --dry-run    # 只打印将要做什么
#   bash install.sh --verify     # 安装后运行 selftest
#
# 只拷贝技能目录与命令文件；不改 settings.json，不写凭据。

set -euo pipefail

FORCE=0
DRY_RUN=0
VERIFY=0

for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --verify) VERIFY=1 ;;
    -h|--help)
      sed -n '3,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "无法识别的参数：$arg（可用：--force / --dry-run / --verify / --help）" >&2
      exit 2
      ;;
  esac
done

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="qa-risk-analysis"
CLAUDE_HOME="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SKILL_DEST="${CLAUDE_HOME}/skills/${SKILL_NAME}"
COMMANDS=(qa-risk-analysis qa-design qa-cases)

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

for required in VERSION SKILL.md bin/preflight.mjs bin/selftest.mjs bin/render-gate.mjs; do
  if [ ! -e "${SOURCE_DIR}/${required}" ]; then
    say "安装包不完整：缺少 ${required}"
    exit 1
  fi
done

for cmd in "${COMMANDS[@]}"; do
  if [ ! -f "${SOURCE_DIR}/command/${cmd}.md" ]; then
    say "安装包不完整：缺少 command/${cmd}.md"
    exit 1
  fi
done

COLLISIONS=()
[ -e "$SKILL_DEST" ] && COLLISIONS+=("$SKILL_DEST")
for cmd in "${COMMANDS[@]}"; do
  [ -e "${CLAUDE_HOME}/commands/${cmd}.md" ] && COLLISIONS+=("${CLAUDE_HOME}/commands/${cmd}.md")
done
if [ "${#COLLISIONS[@]}" -gt 0 ] && [ "$FORCE" -ne 1 ]; then
  say "以下目标已存在："
  for item in "${COLLISIONS[@]}"; do say "  - $item"; done
  say "要覆盖请加 --force。"
  exit 1
fi

# ---- 安装 --------------------------------------------------------------------

say "Node $(node -v) OK"
say "安装技能：${SKILL_DEST}"
run mkdir -p "$(dirname "$SKILL_DEST")"
if [ "$FORCE" -eq 1 ]; then
  run rm -rf "$SKILL_DEST"
fi
run cp -R "$SOURCE_DIR" "$SKILL_DEST"

# 清掉构建期/运行期杂项，避免装进用户目录
run rm -rf "$SKILL_DEST/dist" "$SKILL_DEST/.git" "$SKILL_DEST/.qa" "$SKILL_DEST/node_modules" 2>/dev/null || true
run find "$SKILL_DEST" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
run find "$SKILL_DEST" -name '.DS_Store' -delete 2>/dev/null || true
run find "$SKILL_DEST" -maxdepth 1 -name '*.zip' -delete 2>/dev/null || true

for cmd in "${COMMANDS[@]}"; do
  say "安装命令：${CLAUDE_HOME}/commands/${cmd}.md"
  run mkdir -p "${CLAUDE_HOME}/commands"
  run cp "${SOURCE_DIR}/command/${cmd}.md" "${CLAUDE_HOME}/commands/${cmd}.md"
done

PYBIN="";
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import openpyxl' >/dev/null 2>&1; then PYBIN="$candidate"; break; fi
done
if [ -z "$PYBIN" ]; then
  say "提示：未找到带 openpyxl 的 Python 3；阶段三生成 Excel 前需安装（pip install openpyxl）。阶段一/二不受影响。"
fi

if [ "$VERIFY" -eq 1 ] && [ "$DRY_RUN" -eq 0 ]; then
  say "运行自检："
  (cd "$SKILL_DEST" && node bin/selftest.mjs) || {
    say "自检失败，安装文件已保留在 ${SKILL_DEST}。"
    exit 1
  }
fi

cat <<EOF

安装完成。

下一步：
  1) 设置 Jira 凭据（只放环境变量）：
       export JIRA_BASE_URL="https://jira.example.com"
       export JIRA_PAT="<你的 Personal Access Token>"
  2) 在任意工作目录执行：
       /qa-risk-analysis <ISSUE-KEY 或 URL>
       /qa-design <ISSUE-KEY 或 URL>
       /qa-cases <ISSUE-KEY 或 URL>
  3) 无真实 Jira 试跑：
       node "${SKILL_DEST}/bin/mock-jira.mjs" --port 18080 --print-env

产物落在当前目录的 inputs/ 与 outputs/ 下。
EOF

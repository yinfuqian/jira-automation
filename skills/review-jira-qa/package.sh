#!/usr/bin/env bash
# package.sh — 把 qa-gate 打成可分享的 zip。
#
# 用法：bash package.sh [版本号]
# 产出：dist/qa-gate-<版本>.zip
# 排除：凭据、运行态、构建产物、pycache、历史产物。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VER="${1:-$(cat "$ROOT/VERSION" 2>/dev/null || echo 0.1.0)}"
NAME="qa-gate-$VER"
DIST="$ROOT/dist"
STAGE="$DIST/.stage/$NAME"

log() { printf '[package] %s\n' "$*"; }
die() { printf '[package][ERR] %s\n' "$*" >&2; exit 1; }

[ -f "$ROOT/SKILL.md" ] || die "找不到 SKILL.md"
[ -f "$ROOT/install.sh" ] || die "找不到 install.sh"
PYBIN=""
for candidate in python3 python py; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys' >/dev/null 2>&1; then PYBIN="$candidate"; break; fi
done
[ -n "$PYBIN" ] || die "需要可用的 Python 3 打包 zip"

rm -rf "$STAGE"
mkdir -p "$STAGE"

# 白名单式拷贝
cp "$ROOT/SKILL.md" "$ROOT/README.md" "$ROOT/install.sh" "$ROOT/package.sh" "$STAGE/"
[ -f "$ROOT/.env.example" ] && cp "$ROOT/.env.example" "$STAGE/"
cp -R "$ROOT/bin" "$ROOT/command" "$ROOT/prompts" "$ROOT/templates" "$ROOT/test" "$STAGE/"
[ -d "$ROOT/docs" ] && cp -R "$ROOT/docs" "$STAGE/"

# 清理运行态与构建产物
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -name '*.pyc' -delete 2>/dev/null || true
find "$STAGE" -name '.DS_Store' -delete 2>/dev/null || true
rm -rf "$STAGE/.qa" "$STAGE/dist" "$STAGE/node_modules" 2>/dev/null || true
find "$STAGE" -type f \( -name '.env' -o -name '.env.*.local' \) -delete 2>/dev/null || true
find "$STAGE" -type f -name '*.local' -delete 2>/dev/null || true

chmod +x "$STAGE/install.sh" "$STAGE/package.sh" 2>/dev/null || true
find "$STAGE/bin" -name '*.mjs' -exec chmod +x {} + 2>/dev/null || true

printf '%s\n' "$VER" > "$STAGE/VERSION"

# 密钥/本机路径扫描（宽松：只拦截明显凭据与真实工单号）
if grep -RInE --exclude=package.sh "eyJ[A-Za-z0-9_-]{10,}|AKIA[0-9A-Z]{16}|glpat-|JIRA_PAT=[\"']?[A-Za-z0-9_-]{8,}|Authorization: Bearer [A-Za-z0-9]{8,}" "$STAGE" >/dev/null 2>&1; then
  die "包内疑似包含真实凭据，已停止打包"
fi
if grep -RInE --exclude=package.sh '/Users/|C:\\\\Users|E:\\\\code' "$STAGE" >/dev/null 2>&1; then
  die "包内包含本机绝对路径，已停止打包"
fi

ZIP="$DIST/$NAME.zip"
mkdir -p "$DIST"
rm -f "$ZIP"
PY="$PYBIN"
"$PY" - "$STAGE" "$ZIP" "$NAME" <<'PY'
import os, sys, zipfile
stage, zip_path, root = sys.argv[1], sys.argv[2], sys.argv[3]
base = os.path.dirname(stage)
with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
    for cur, dirs, files in os.walk(stage):
        dirs[:] = sorted(d for d in dirs if d not in ('.git', '__pycache__'))
        for f in sorted(files):
            full = os.path.join(cur, f)
            z.write(full, os.path.relpath(full, base))
PY

N_FILES="$("$PY" -c "import zipfile,sys;print(len(zipfile.ZipFile(sys.argv[1]).namelist()))" "$ZIP")"
SIZE="$(wc -c <"$ZIP" | tr -d ' ')"
log "已完成：$ZIP"
log "  条目 $N_FILES 个 · $((SIZE/1024)) KB"
log "  顶层目录：$NAME/"

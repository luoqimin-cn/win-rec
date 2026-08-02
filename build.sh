#!/bin/bash
set -euo pipefail

REPO="luoqimin-cn/win-rec"
BRANCH="${1:-main}"

echo "=== win-rec 构建触发脚本 ==="

# 检查是否有未提交的更改
if [[ -n $(git status --porcelain) ]]; then
    echo "[1/3] 有未提交更改，正在提交..."
    git add -A
    git commit -m "sync: local changes $(date '+%Y-%m-%d %H:%M')" || {
        echo "没有需要提交的内容，跳过 commit"
    }
else
    echo "[1/3] 工作区干净，跳过 commit"
fi

echo "[2/3] 推送到 GitHub ($BRANCH)..."
git push origin "$BRANCH"

echo "[3/3] 触发 CI 构建..."
gh workflow run build-windows.yml -R "$REPO" --ref "$BRANCH"

echo ""
echo "✓ 已推送并触发构建"
echo "  查看进度: https://github.com/$REPO/actions"
echo "  下载地址: https://github.com/$REPO/releases/tag/latest"

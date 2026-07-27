#!/bin/bash
set -e
SRC="/Users/jamesluo/Downloads/win-rec"
DIR="/Users/jamesluo/Library/Mobile Documents/com~apple~CloudDocs/AI Studio/win-rec"
TOKEN=$(cat "$DIR/.build_token")
REPO="luoqimin-cn/win-rec"

cd "$DIR"

# 1. sync
rsync -a --exclude '.git' "$SRC"/ ./

# 2. commit
git add -A
git diff --cached --stat
git commit -m "update: $(date '+%m-%d %H:%M')" || { echo "无变更，跳过"; exit 0; }

# 3. push
git -c http.version=HTTP/1.1 -c remote.origin.url="https://${TOKEN}@github.com/${REPO}.git" push

# 4. trigger build
echo -n "触发构建... "
HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
  "https://api.github.com/repos/${REPO}/actions/workflows/build-windows.yml/dispatches" \
  -H "Authorization: token ${TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  -d '{"ref":"main"}')
echo "HTTP $HTTP"

# 5. wait & check
echo "等待构建完成..."
sleep 60

for i in $(seq 1 15); do
  RESULT=$(curl -s "https://api.github.com/repos/${REPO}/actions/runs?per_page=1" \
    -H "Authorization: token ${TOKEN}" \
    -H "Accept: application/vnd.github+json")
  STATUS=$(echo "$RESULT" | python3 -c "import sys,json;print(json.load(sys.stdin)['workflow_runs'][0]['status'])")
  CONCLUSION=$(echo "$RESULT" | python3 -c "import sys,json;print(json.load(sys.stdin)['workflow_runs'][0]['conclusion'] or '')")
  URL=$(echo "$RESULT" | python3 -c "import sys,json;print(json.load(sys.stdin)['workflow_runs'][0]['html_url'])")

  if [ "$STATUS" = "completed" ]; then
    if [ "$CONCLUSION" = "success" ]; then
      echo "✓ 构建成功！"
      echo "下载: $URL"
    else
      echo "✗ 构建失败 ($CONCLUSION)"
      echo "查看: $URL"
    fi
    exit 0
  fi

  echo "  构建中... ($((i+1))/15)"
  sleep 60
done

echo "⚠ 超时，手动查看: https://github.com/${REPO}/actions"

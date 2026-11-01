#!/bin/bash
# 在打开指定工程的 EasyEDA 窗口上执行 JS 代码（经 easyeda-api bridge）。
# 用法: eda_exec.sh <工程名> <代码文件> [超时秒=28]
# 前置: easyeda-api skill 的 bridge 已运行（端口 49620-49629 自动扫描）。
# 输出: bridge 的原始 JSON 响应（stdout）；TARGET_WINDOW 输出到 stderr。
export PATH="/c/Users/Leon/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:/c/Users/Leon/.workbuddy/binaries/PortableGit/versions/1.2.0/bin:$PATH"
NODE="/c/Users/Leon/.workbuddy/binaries/node/versions/22.22.2-6/node.exe"

PROJECT="$1"
CODE_FILE="$2"
TIMEOUT="${3:-28}"

if [ -z "$PROJECT" ] || [ -z "$CODE_FILE" ] || [ ! -f "$CODE_FILE" ]; then
  echo "usage: eda_exec.sh <project_name> <code_file> [timeout_sec]" >&2
  exit 2
fi

# 1. 找 bridge 端口
BRIDGE_PORT=""
for port in $(seq 49620 49629); do
  resp=$(curl -s http://127.0.0.1:$port/health 2>/dev/null)
  if echo "$resp" | grep -q 'easyeda-bridge'; then BRIDGE_PORT=$port; break; fi
done
if [ -z "$BRIDGE_PORT" ]; then
  echo "ERROR: easyeda-bridge 未运行。请按 easyeda-api skill 启动 bridge（node scripts/bridge-server.mjs 后台运行）。" >&2
  exit 3
fi

# 2. 定位目标窗口（窗口 ID 每隔几分钟重连就变，必须每次重新查）
TARGET=""
for w in $(curl -s http://127.0.0.1:$BRIDGE_PORT/eda-windows | "$NODE" -e "let d='';process.stdin.on('data',c=>d+=c).on('end',()=>{try{console.log(JSON.parse(d).windows.map(w=>w.windowId).join(' '))}catch(e){console.log('')}})"); do
  R=$(curl -s -m 20 -X POST http://127.0.0.1:$BRIDGE_PORT/execute -H "Content-Type: application/json" \
    -d "{\"code\": \"const p = await eda.dmt_Project.getCurrentProjectInfo(); return p?.friendlyName || p?.name || 'none';\", \"windowId\": \"$w\"}")
  if echo "$R" | grep -q "\"$PROJECT\""; then TARGET="$w"; break; fi
done
if [ -z "$TARGET" ]; then
  echo "ERROR: 未找到当前工程为 [$PROJECT] 的 EDA 窗口（bridge: $BRIDGE_PORT）。" >&2
  exit 4
fi
echo "TARGET_WINDOW=$TARGET" >&2

# 3. 执行代码
"$NODE" -e "
const fs = require('fs');
const payload = JSON.stringify({ code: fs.readFileSync(process.argv[1], 'utf8'), windowId: process.argv[2] });
fs.writeFileSync(process.argv[3], payload);
" "$CODE_FILE" "$TARGET" "$TMP/eda_skill_payload.json"

curl -s -m "$TIMEOUT" -X POST http://127.0.0.1:$BRIDGE_PORT/execute -H "Content-Type: application/json" --data @"$TMP/eda_skill_payload.json"

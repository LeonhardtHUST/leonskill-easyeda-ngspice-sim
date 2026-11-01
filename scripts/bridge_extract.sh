#!/bin/bash
# 在线提取：从嘉立创EDA工程的普通原理图页提取 IR（三段式，规避 30s 桥接硬超时）。
# 用法: bridge_extract.sh <工程名> [页名或页uuid] <输出_ir.json>
# 前置: easyeda-api bridge 运行中；目标工程在 EDA 中打开。
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="/c/Users/Leon/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:/c/Users/Leon/.workbuddy/binaries/PortableGit/versions/1.2.0/bin:$PATH"
NODE="/c/Users/Leon/.workbuddy/binaries/node/versions/22.22.2-6/node.exe"
PY="/c/Users/Leon/.workbuddy/binaries/python/versions/3.13.12/python.exe"

PROJECT="$1"; PAGE_KEY="$2"; OUT="$3"
if [ -z "$PROJECT" ] || [ -z "$OUT" ]; then
  echo "usage: bridge_extract.sh <project> [page_name|page_uuid] <out_ir.json>" >&2; exit 2
fi
[ -z "$PAGE_KEY" ] && PAGE_KEY="P1"

WORK="$TMP/eda_extract_$$"; mkdir -p "$WORK"

# 1) 列出原理图页
cat > "$WORK/list_pages.js" <<'EOF'
const pages = await eda.dmt_Schematic.getAllSchematicPagesInfo();
const out = (pages || []).map(p => ({ uuid: p.uuid, name: p.name, sch: p.parentSchematicUuid }));
return JSON.stringify(out);
EOF
RESP=$(bash "$SCRIPT_DIR/eda_exec.sh" "$PROJECT" "$WORK/list_pages.js" 25 2>/dev/null)
echo "$RESP" | "$NODE" -e "let d='';process.stdin.on('data',c=>d+=c).on('end',()=>{try{process.stdout.write(JSON.parse(d).result)}catch(e){process.stdout.write('[]')}})" > "$WORK/pages.json"
PAGE_UUID=$("$PY" -c "
import json,sys
pages=json.load(open(r'$WORK/pages.json'))
key=sys.argv[1]
hit=next((p for p in pages if p['uuid']==key or p['name']==key),None)
if not hit and pages: hit=pages[0]
print(hit['uuid'] if hit else '')
" "$PAGE_KEY")
if [ -z "$PAGE_UUID" ]; then echo "ERROR: 找不到原理图页 [$PAGE_KEY]" >&2; exit 4; fi
echo "page: $PAGE_KEY -> $PAGE_UUID" >&2

# 2) 三段提取
extract_seg() {
  local seg="$1"
  sed "s/__PAGE_UUID__/$PAGE_UUID/g; s/__SEG__/$seg/g" "$SCRIPT_DIR/js/extract_sch.js" > "$WORK/$seg.js"
  RESP=$(bash "$SCRIPT_DIR/eda_exec.sh" "$PROJECT" "$WORK/$seg.js" 27 2>/dev/null)
  echo "$RESP" | "$NODE" -e "let d='';process.stdin.on('data',c=>d+=c).on('end',()=>{try{const r=JSON.parse(d);if(!r.success){console.error('SEG FAIL: '+(r.error||'?'));process.exit(3)}process.stdout.write(r.result)}catch(e){console.error('BAD RESP');process.exit(4)}})" > "$WORK/$seg_raw.json" \
    || { echo "ERROR: 提取段 $seg 失败" >&2; exit 5; }
  "$PY" -c "
import json,sys
raw=json.load(open(r'$WORK/$seg_raw.json',encoding='utf-8'))
seg=sys.argv[1]
if not raw.get('docOk'):
    print('WARN: 当前文档与目标页不一致（可能未激活成功）', file=sys.stderr)
key={'comps':'comps','pins':'pins','wires':'wires'}[seg]
json.dump(raw[key], open(r'$WORK/$seg.json','w',encoding='utf-8'), ensure_ascii=False)
print(f'{seg}: {len(raw[key])}', file=sys.stderr)
" "$seg"
}

extract_seg comps
extract_seg pins
extract_seg wires

# 3) 合并为 IR
"$PY" "$SCRIPT_DIR/ir_common.py" --merge "$WORK/comps.json" "$WORK/pins.json" "$WORK/wires.json" "$OUT" \
  "$PROJECT" "{\"uuid\":\"$PAGE_UUID\",\"name\":\"$PAGE_KEY\"}"
echo "IR: $OUT" >&2

#!/bin/bash
# 从 EasyEDA 工程的活动仿真页（NGspice1 等 doctype=8 页面）导出 NGspice 网表。
# 用法: export_netlist.sh <工程名> <输出文件.cir>
# 注意: 仿真页必须已在 EDA 中打开（openDocument 不支持仿真页，只能人工双击打开）。
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="/c/Users/Leon/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:/c/Users/Leon/.workbuddy/binaries/PortableGit/versions/1.2.0/bin:$PATH"
NODE="/c/Users/Leon/.workbuddy/binaries/node/versions/22.22.2-6/node.exe"

PROJECT="$1"
OUT="$2"
if [ -z "$PROJECT" ] || [ -z "$OUT" ]; then
  echo "usage: export_netlist.sh <project_name> <output.cir>" >&2; exit 2
fi

# 1. 激活工程窗口里的仿真页（doctype=8 的标签）。首次激活可能引起扩展重载断线，忽略该次响应。
cat > "$TMP/skill_activate.js" <<'EOF'
const tree = await eda.dmt_EditorControl.getSplitScreenTree();
const tabs = (tree?.tabs || []);
const simTab = tabs.find(t => t.data?.doctype === 8);
if (!simTab) return '__NO_SIM_TAB__';
await eda.dmt_EditorControl.activateDocument(simTab.tabId);
return '__SIM_ACTIVATED__';
EOF
bash "$SCRIPT_DIR/eda_exec.sh" "$PROJECT" "$TMP/skill_activate.js" 15 >/dev/null 2>&1 || true
sleep 6

# 2. 导出网表（带 12s 竞时保护，防空页/异常时挂死）
cat > "$TMP/skill_export.js" <<'EOF'
const race=(p,ms)=>Promise.race([p.then(v=>v),new Promise(r2=>setTimeout(()=>r2('__T__'),ms))]);
try{
  const nl=await race(eda.sch_ManufactureData.getSimulationNetlistFile('sim','Ngspice'),12000);
  return (nl&&nl!=='__T__')?await nl.text():'__TIMEOUT__';
}catch(e){return '__ERR__'+String(e).slice(0,120);}
EOF
RESP=$(bash "$SCRIPT_DIR/eda_exec.sh" "$PROJECT" "$TMP/skill_export.js" 25 2>/dev/null)
echo "$RESP" | "$NODE" -e "let d='';process.stdin.on('data',c=>d+=c).on('end',()=>{
  try{const r=JSON.parse(d);const net=r.result||'';
    if(net.startsWith('__')){console.error('EXPORT FAILED: '+net.slice(0,120));process.exit(5)}
    process.stdout.write(net);
  }catch(e){console.error('BAD RESPONSE: '+d.slice(0,200));process.exit(6)}
})" > "$OUT"
grep -qi "ngspice\|\.MODEL\|\.tran" "$OUT" || { echo "ERROR: 导出内容不像网表" >&2; exit 7; }
echo "netlist exported: $OUT ($(wc -c < "$OUT") bytes)" >&2

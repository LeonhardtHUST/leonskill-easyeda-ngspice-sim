#!/bin/bash
# 在线总入口：从工程原理图页直接导出网表并仿真（不要求预先有仿真页）。
# 用法: sim_from_sch.sh <工程名> [页名|页uuid] <op|tran|ac|dc|custom> [模式参数/控制文件] [工作目录]
# 示例: sim_from_sch.sh "2026秋季硬件2" P1 op
#       sim_from_sch.sh "2026秋季硬件2" P1 ac "dec 50 1 10meg" ./out
# 可选环境变量: PROBE=1 时额外探测"普通页 EDA 直导网表"能力（有挂起风险，默认关闭）
#
# 流程: bridge 提取三类图元 → IR → 并查集连通性 → 自算网表(+SVG) → 注入 .control → ngspice
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="/c/Users/Leon/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:/c/Users/Leon/.workbuddy/binaries/PortableGit/versions/1.2.0/bin:$PATH"
PY="/c/Users/Leon/.workbuddy/binaries/python/versions/3.13.12/python.exe"
NODE="/c/Users/Leon/.workbuddy/binaries/node/versions/22.22.2-6/node.exe"
NGSPICE="${NGSPICE_BIN:-/c/Users/Leon/.workbuddy/binaries/ngspice/ngspice-47/Spice64/bin/ngspice_con.exe}"

PROJECT="$1"; PAGE="${2:-P1}"; MODE="${3:-op}"; ARG="$4"; WORKDIR="${5:-.}"
if [ -z "$PROJECT" ]; then
  sed -n '2,8p' "$0" >&2; exit 2
fi
mkdir -p "$WORKDIR"; cd "$WORKDIR"
W="$PWD"

echo "[0/6] 检查 EDA 仿真页状态..."
RESP=$(bash "$SCRIPT_DIR/eda_exec.sh" "$PROJECT" "$SCRIPT_DIR/js/check_simtab.js" 22 2>/dev/null || true)
echo "$RESP" | "$NODE" -e "let d='';process.stdin.on('data',c=>d+=c).on('end',()=>{try{const r=JSON.parse(JSON.parse(d).result);console.log('  hasSimTab='+r.hasSimTab+(r.simTabTitle?' ('+r.simTabTitle+')':''))}catch(e){console.log('  (无法获取标签信息)')}})"

if [ "${PROBE:-0}" = "1" ]; then
  echo "[0b] 探测 EDA 直导网表能力（普通页）..."
  RESP=$(bash "$SCRIPT_DIR/eda_exec.sh" "$PROJECT" "$SCRIPT_DIR/js/probe_netlist.js" 28 2>/dev/null || true)
  echo "$RESP" | "$NODE" -e "let d='';process.stdin.on('data',c=>d+=c).on('end',()=>{try{const r=JSON.parse(JSON.parse(d).result);console.log('  probe='+r.status+' '+(r.ms||'')+'ms'+(r.len?' len='+r.len:''))}catch(e){console.log('  (探测失败/挂起)')}})"
fi

echo "[1/6] 提取原理图 $PAGE ..."
bash "$SCRIPT_DIR/bridge_extract.sh" "$PROJECT" "$PAGE" "$W/ir.json"

echo "[2/6] 计算连通性..."
"$PY" "$SCRIPT_DIR/connectivity.py" "$W/ir.json" "$W/ir_nets.json" | head -20

echo "[3/6] 生成网表..."
"$PY" "$SCRIPT_DIR/netlist_gen.py" "$W/ir_nets.json" "$W/schematic.cir"

echo "[4/6] 生成仿真线路图 SVG..."
"$PY" "$SCRIPT_DIR/svg_schematic.py" "$W/ir_nets.json" "$W/sim_diagram.svg"

echo "[5/6] 注入分析控制块并运行..."
if [ "$MODE" = "custom" ]; then
  "$PY" "$SCRIPT_DIR/build_deck.py" "$W/schematic.cir" "$W/deck.cir" custom "$ARG"
else
  "$PY" "$SCRIPT_DIR/build_deck.py" "$W/schematic.cir" "$W/deck.cir" "$MODE" "$ARG"
fi
"$NGSPICE" -b "$W/deck.cir" 2>&1 | grep -v "^$" | grep -v "^Note: No compatibility" | head -60

echo "[6/6] 完成。产物: ir.json / schematic.cir / sim_diagram.svg / deck.cir / *.raw"
ls -la "$W"/*.raw 2>/dev/null || true

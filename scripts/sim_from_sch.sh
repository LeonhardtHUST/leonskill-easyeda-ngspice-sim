#!/bin/bash
# 在线总入口：从工程原理图页直接导出网表并仿真（不要求预先有仿真页）。
#
# 用法: sim_from_sch.sh <工程名> [页名|页uuid] <op|tran|ac|dc|custom> [模式参数/控制文件] [工作目录] [v2.2 选项]
# 示例: sim_from_sch.sh "2026秋季硬件2" P1 op
#       sim_from_sch.sh "2026秋季硬件2" P1 ac "dec 50 1 10meg" ./out
#       sim_from_sch.sh "SDR" P1 tran "1p 40n" ./out --config sim.json --strict
#
# v2.2 选项与离线入口完全一致（见 _opts.sh）：--config / --models /
# --behavioral / --override / --power-policy / --strict / --allow-partial-model
#
# 可选环境变量: PROBE=1 时额外探测"普通页 EDA 直导网表"能力（有挂起风险，默认关闭）
#
# 流程: bridge 提取三类图元 → IR → 并查集连通性 → 自算网表(+SVG) → 注入 .control → ngspice
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="/c/Users/Leon/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:/c/Users/Leon/.workbuddy/binaries/PortableGit/versions/1.2.0/bin:$PATH"
PY="/c/Users/Leon/.workbuddy/binaries/python/versions/3.13.12/python.exe"
NODE="/c/Users/Leon/.workbuddy/binaries/node/versions/22.22.2-6/node.exe"
NGSPICE="${NGSPICE_BIN:-/c/Users/Leon/.workbuddy/binaries/ngspice/ngspice-47/Spice64/bin/ngspice_con.exe}"
source "$SCRIPT_DIR/_opts.sh"

parse_sim_opts "$@"
PROJECT="${POSITIONAL[0]:-}"; PAGE="${POSITIONAL[1]:-P1}"
MODE="${POSITIONAL[2]:-op}"; ARG="${POSITIONAL[3]:-}"; WORKDIR="${POSITIONAL[4]:-.}"
if [ -z "$PROJECT" ]; then
  sed -n '2,16p' "$0" >&2; exit 2
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

echo "[3/6] 生成网表（v2.2：模型映射 / ground 策略 / override）..."
"$PY" "$SCRIPT_DIR/netlist_gen.py" "$W/ir_nets.json" "$W/schematic.cir" \
  --report "$W/netlist_report.json" "${NET_OPTS[@]}"

echo "[4/6] 生成仿真线路图 SVG..."
"$PY" "$SCRIPT_DIR/svg_schematic.py" "$W/ir_nets.json" "$W/sim_diagram.svg"

echo "[5/6] 注入分析控制块并运行（支持 sweep / metrics / manifest）..."
"$PY" "$SCRIPT_DIR/sim_exec.py" "$W/schematic.cir" "$MODE" "$ARG" \
  --report "$W/netlist_report.json" --workdir "$W" \
  --project "$PROJECT" --ngspice "$NGSPICE" "${EXEC_OPTS[@]}"

echo "[6/6] 完成。产物: ir.json / schematic.cir / sim_diagram.svg / deck.cir / *.raw / metrics.json / simulation_manifest.json"
ls -1 "$W"/*.raw "$W"/*.csv "$W"/simulation_manifest.json 2>/dev/null || true

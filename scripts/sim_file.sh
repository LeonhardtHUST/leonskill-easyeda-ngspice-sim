#!/bin/bash
# 离线总入口：直接解析嘉立创EDA专业版工程文件（.epro2）→ 自算网表 → SVG → 仿真。
# 不需要运行中的 EDA / bridge。
#
# 用法: sim_file.sh <工程.epro2> <op|tran|ac|dc|custom> [模式参数/控制文件] [页名] [工作目录] [v2.2 选项]
# 示例: sim_file.sh ~/Downloads/xxx.epro2 op
#       sim_file.sh ~/Downloads/xxx.epro2 ac "dec 50 1 10meg" P1 ./out
#       sim_file.sh board.epro2 tran "1p 40n" "" ./out --config sim.json --strict
#
# v2.2 选项（详见 _opts.sh）:
#   --config <sim.json> --models <model_map.json> --behavioral <file|off>
#   --override <sim_override.json> --power-policy auto|explicit
#   --strict --allow-partial-model
#
# 环境变量: KEEP_OPEN=1 → 无模型器件保留为注释行（默认直接排除）
# 说明: 只支持 .epro2（含 .epru 日志）；旧版 .epro 请先在 EDA 中"另存为/导出"为 .epro2。
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="/c/Users/Leon/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:/c/Users/Leon/.workbuddy/binaries/PortableGit/versions/1.2.0/bin:$PATH"
PY="/c/Users/Leon/.workbuddy/binaries/python/versions/3.13.12/python.exe"
NGSPICE="${NGSPICE_BIN:-/c/Users/Leon/.workbuddy/binaries/ngspice/ngspice-47/Spice64/bin/ngspice_con.exe}"
source "$SCRIPT_DIR/_opts.sh"

parse_sim_opts "$@"
PROJ_FILE="${POSITIONAL[0]:-}"; MODE="${POSITIONAL[1]:-op}"
ARG="${POSITIONAL[2]:-}"; PAGE="${POSITIONAL[3]:-}"; WORKDIR="${POSITIONAL[4]:-.}"
if [ -z "$PROJ_FILE" ]; then
  sed -n '2,18p' "$0" >&2; exit 2
fi
mkdir -p "$WORKDIR"
PROJ_ABS="$(cd "$(dirname "$PROJ_FILE")" && pwd)/$(basename "$PROJ_FILE")"
cd "$WORKDIR"; W="$PWD"
KEEP=""
[ "${KEEP_OPEN:-0}" = "1" ] && KEEP="--keep-open"

echo "[1/6] 解析工程文件 $(basename "$PROJ_FILE") ..."
"$PY" "$SCRIPT_DIR/epro2_parse.py" "$PROJ_ABS" "$W/ir.json" $PAGE

echo "[2/6] 计算连通性..."
"$PY" "$SCRIPT_DIR/connectivity.py" "$W/ir.json" "$W/ir_nets.json" | head -20

echo "[3/6] 生成网表（v2.2：模型映射 / ground 策略 / override）..."
"$PY" "$SCRIPT_DIR/netlist_gen.py" "$W/ir_nets.json" "$W/schematic.cir" $KEEP \
  --report "$W/netlist_report.json" "${NET_OPTS[@]}"

echo "[4/6] 生成仿真线路图 SVG..."
"$PY" "$SCRIPT_DIR/svg_schematic.py" "$W/ir_nets.json" "$W/sim_diagram.svg"

if [ "${WRITEBACK:-0}" = "1" ]; then
  echo "[4b] 写回仿真页到工程文件（自动备份 .bak.epro2）..."
  "$PY" "$SCRIPT_DIR/router.py" "$W/ir_nets.json" "$W/plan.json" >/dev/null
  "$PY" "$SCRIPT_DIR/epro2_writeback.py" "$PROJ_ABS" "$W/plan.json" \
    "$SCRIPT_DIR/models/simlib_pack.json" 2>&1 | sed 's/^/     /'
fi

echo "[5/6] 注入分析控制块并运行（支持 sweep / metrics / manifest）..."
"$PY" "$SCRIPT_DIR/sim_exec.py" "$W/schematic.cir" "$MODE" "$ARG" \
  --report "$W/netlist_report.json" --workdir "$W" \
  --project "$(basename "$PROJ_FILE")" --source "$PROJ_ABS" \
  --ngspice "$NGSPICE" "${EXEC_OPTS[@]}"

echo "[6/6] 完成。产物: $W/{ir.json, ir_nets.json, schematic.cir, netlist_report.json,"
echo "      sim_diagram.svg, deck.cir, *.raw, *.csv, metrics.json, simulation_manifest.json}"
ls -1 "$W"/*.raw "$W"/*.csv "$W"/simulation_manifest.json 2>/dev/null || true

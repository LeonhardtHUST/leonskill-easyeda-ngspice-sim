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
# v2.3 选项（详见 _opts.sh）:
#   --ibis <map.json> --pcb <epro2|geometry.json> --stackup <stackup.json>
#   --auto-parasitics [--auto-net NET ...] [--board-thickness 1.6]
#   --workspace <dir> --ports <ports.json> --cases N --seed N --criteria <json>
#
# 架构边界：EasyEDA 只做设计数据源；本脚本 + ngspice 完全外部执行，
#           不需要打开 EasyEDA、不需要切换到 Simulation 页面。
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

# v2.5：em 模式只产出 openEMS 工程（不求解、不生成网表），走独立分支
if [ "${MODE,,}" = "em" ]; then
  EM_DIR="${EM_OUT:-$W/em}"
  echo "[em] 生成 openEMS coupon 工程（不求解，需用户自己的 openEMS）..."
  EM_ARGS=( "$PROJ_ABS" --out "$EM_DIR" )
  # 叠层 / 板厚从 PCB_OPTS 里复用
  _i=0; _arr=($PCB_OPTS)
  while [ $_i -lt ${#_arr[@]} ]; do
    case "${_arr[$_i]}" in
      --stackup) EM_ARGS+=( --stackup "${_arr[$((_i+1))]}" ); _i=$((_i+2)) ;;
      --board-thickness) EM_ARGS+=( --board-thickness "${_arr[$((_i+1))]}" ); _i=$((_i+2)) ;;
      *) _i=$((_i+1)) ;;
    esac
  done
  "$PY" "$SCRIPT_DIR/pcb_em.py" "${EM_ARGS[@]}" "${EM_OPTS[@]}"
  echo "[em] 产物: $EM_DIR/{em_coupon.py, EM_PLAN.json}"
  exit 0
fi

# 模式白名单：不认识的模式宁可立刻报错，也不要"看起来跑过了"
case "${MODE,,}" in
  op|tran|ac|dc|custom|sweep|pdn|sparam|montecarlo|mc|corner) ;;
  *)
    cat >&2 <<EOF
[错误] 未知模式: $MODE
  sim_file.sh 支持: op|tran|ac|dc|custom|sweep|pdn|sparam|montecarlo|mc|corner|em
  说明：v2.4 的 RF 宏模型 / 差分 SI / 实测相关 / 参数校准 目前是 **Python API**，
        没有挂到本 shell 入口（避免出现"命令能跑但语义不对"的假 CLI）。
        对应入口见 SKILL.md 的「Python API」小节。
EOF
    exit 2 ;;
esac

echo "[1/6] 解析工程文件 $(basename "$PROJ_FILE") ..."
"$PY" "$SCRIPT_DIR/epro2_parse.py" "$PROJ_ABS" "$W/ir.json" $PAGE

echo "[2/6] 计算连通性..."
"$PY" "$SCRIPT_DIR/connectivity.py" "$W/ir.json" "$W/ir_nets.json" | head -20

# v2.3：PCB 几何自动提参 → override（opt-in；不需要打开 EDA 仿真页）
if [ "$AUTO_PARASITICS" = "1" ]; then
  echo "[2b] PCB 几何提参 → 自动 override ..."
  if ! auto_parasitics "$SCRIPT_DIR" "$PY" "$W" "$W/sim_override.generated.json"; then
    echo "[2b] 自动提参失败（见上），继续使用显式 override" >&2
  else
    NET_OPTS+=(--override "$W/sim_override.generated.json")
  fi
fi

echo "[3/6] 生成网表（模型映射 / ground 策略 / IBIS / override）..."
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

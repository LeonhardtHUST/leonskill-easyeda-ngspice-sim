#!/bin/bash
# 一键流水线：EasyEDA 仿真页 → 导网表 → 生成 deck → 本地 ngspice 仿真 → 输出结果。
#
# 用法:
#   sim_pipeline.sh <工程名> <模式> [模式参数或custom控制文件] [工作目录]
#
# 模式:
#   op      静态工作点（结果直接打印）
#   tran    瞬态仿真      参数默认 "5u 25m"，波形写入 tran.raw
#   ac      AC 扫频       参数默认 "dec 100 10 100meg"，数据写入 ac.raw
#   dc      DC 扫描        参数必填（如 "V1 0 12 0.1"），数据写入 dc.raw
#   custom  自定义控制块   第3参数=控制块正文文件（不含 .control/.endc）
#
# 前置:
#   - easyeda-api bridge 已运行
#   - 目标工程在 EDA 中打开，且仿真页（Simulation1/NGspice1…）已人工打开
#   - ngspice-47 已安装（NGSPICE_BIN 环境变量可覆盖路径）
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PATH="/c/Users/Leon/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:/c/Users/Leon/.workbuddy/binaries/PortableGit/versions/1.2.0/bin:$PATH"
export PATH="/c/Users/Leon/.workbuddy/binaries/python/versions/3.13.12:$PATH"
PY="/c/Users/Leon/.workbuddy/binaries/python/versions/3.13.12/python.exe"
NGSPICE="${NGSPICE_BIN:-/c/Users/Leon/.workbuddy/binaries/ngspice/ngspice-47/Spice64/bin/ngspice_con.exe}"

PROJECT="$1"; MODE="${2:-op}"; ARG="$3"; WORKDIR="${4:-.}"
if [ -z "$PROJECT" ]; then
  grep -E "^#( |$)" "$0" | head -20 >&2; exit 2
fi
mkdir -p "$WORKDIR"; cd "$WORKDIR"

echo "[1/4] 从 EDA 导出网表..."
bash "$SCRIPT_DIR/export_netlist.sh" "$PROJECT" circuit_raw.cir

echo "[2/4] 生成 deck (mode=$MODE)..."
if [ "$MODE" = "custom" ]; then
  "$PY" "$SCRIPT_DIR/build_deck.py" circuit_raw.cir deck.cir custom "$ARG"
else
  "$PY" "$SCRIPT_DIR/build_deck.py" circuit_raw.cir deck.cir "$MODE" "$ARG"
fi

echo "[3/4] ngspice 仿真运行..."
"$NGSPICE" -b deck.cir 2>&1 | grep -v "^$" | grep -v "^Note: No compatibility" | head -60

echo "[4/4] 完成。"
ls -la *.raw 2>/dev/null || echo "(无 .raw 波形文件——op/纯 meas 模式属正常)"

#!/bin/bash
# v2.3 共用 CLI 选项解析（被 sim_file.sh / sim_from_sch.sh source）。
#
#   parse_sim_opts "$@"   → 设置 POSITIONAL[] / NET_OPTS[] / EXEC_OPTS[] / PCB_OPTS[]
#
# 原则：v2.2 的老位置参数与选项全部保留；v2.3 新功能一律 opt-in。

parse_sim_opts() {
  POSITIONAL=()
  NET_OPTS=()
  EXEC_OPTS=()
  PCB_OPTS=()
  EM_OPTS=()
  EM_OUT=""
  AUTO_PARASITICS=0
  PCB_AUTO_NETS=""
  WORKSPACE=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --config)
        NET_OPTS+=(--config "$2"); EXEC_OPTS+=(--config "$2"); shift 2 ;;
      --models)
        NET_OPTS+=(--models "$2"); shift 2 ;;
      --behavioral)
        NET_OPTS+=(--behavioral "$2"); shift 2 ;;
      --override)
        NET_OPTS+=(--override "$2"); shift 2 ;;
      --power-policy)
        NET_OPTS+=(--power-policy "$2"); shift 2 ;;
      --strict)
        NET_OPTS+=(--strict); EXEC_OPTS+=(--strict); shift ;;
      --allow-partial-model)
        NET_OPTS+=(--allow-partial-model); shift ;;
      # ---- v2.3 ----
      --ibis)
        NET_OPTS+=(--ibis "$2"); shift 2 ;;
      --pcb)
        PCB_OPTS+=(--pcb "$2"); shift 2 ;;
      --stackup)
        PCB_OPTS+=(--stackup "$2"); shift 2 ;;
      --auto-parasitics)
        AUTO_PARASITICS=1; shift ;;
      --auto-net)
        PCB_AUTO_NETS="$PCB_AUTO_NETS --net $2"; shift 2 ;;
      --board-thickness)
        PCB_OPTS+=(--board-thickness "$2"); shift 2 ;;
      --workspace)
        WORKSPACE="$2"; EXEC_OPTS+=(--workspace "$2"); shift 2 ;;
      --ports)
        EXEC_OPTS+=(--ports "$2"); shift 2 ;;
      --cases)
        EXEC_OPTS+=(--cases "$2"); shift 2 ;;
      --seed)
        EXEC_OPTS+=(--seed "$2"); shift 2 ;;
      --criteria)
        EXEC_OPTS+=(--criteria "$2"); shift 2 ;;
      --help|-h)
        echo "用法见脚本头部注释"; exit 0 ;;
      # ---- v2.5 ----
      --net)
        EM_OPTS+=(--net "$2"); shift 2 ;;
      --fmax)
        EM_OPTS+=(--fmax "$2"); shift 2 ;;
      --margin)
        EM_OPTS+=(--margin "$2"); shift 2 ;;
      --em-out)
        EM_OUT="$2"; shift 2 ;;
      --cache)
        EXEC_OPTS+=(--cache "$2"); shift 2 ;;
      --no-cache)
        EXEC_OPTS+=(--no-cache); shift ;;
      *)
        POSITIONAL+=("$1"); shift ;;
    esac
  done
}

# PCB 几何 → 自动 override（--auto-parasitics 时调用）
# 用法: auto_parasitics <scripts_dir> <python> <workdir> <out_json>
auto_parasitics() {
  local S="$1" PY="$2" W="$3" OUT="$4"
  local PCB="" STACK=""
  local i=0
  # 注意：必须写 "${PCB_OPTS[@]}"；$PCB_OPTS 在 bash 里只展开元素 0
  #（v2.5 修：旧写法导致 --stackup 的值永远是空串，auto-parasitics 一直丢叠层）
  local arr=("${PCB_OPTS[@]}")
  while [ $i -lt ${#arr[@]} ]; do
    case "${arr[$i]}" in
      --pcb) PCB="${arr[$((i+1))]}"; i=$((i+2)) ;;
      --stackup) STACK="${arr[$((i+1))]}"; i=$((i+2)) ;;
      *) i=$((i+1)) ;;
    esac
  done
  if [ -z "$PCB" ]; then
    echo "[pcb] --auto-parasitics 需要同时给 --pcb <工程.epro2|PCB_GEOMETRY.json>" >&2
    return 2
  fi
  local ARGS=( "$PCB" --out "$OUT" )
  [ -n "$PCB_AUTO_NETS" ] && ARGS+=( $PCB_AUTO_NETS ) || ARGS+=( --net all )
  [ -n "$STACK" ] && ARGS+=( --stackup "$STACK" )
  "$PY" "$S/pcb_to_override.py" "${ARGS[@]}"
}

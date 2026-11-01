#!/bin/bash
# v2.2 共用 CLI 选项解析（被 sim_file.sh / sim_from_sch.sh source）。
#
# 目标：不破坏旧的位置参数用法，同时允许把复杂仿真配置全部塞进 json。
#
#   parse_sim_opts "$@"   → 设置 POSITIONAL[] / NET_OPTS[] / EXEC_OPTS[]
#
# 支持的选项：
#   --config <sim.json>          复杂仿真主入口
#   --models <model_map.json>    外部 SPICE 模型映射
#   --behavioral <file|off>      行为级模型库
#   --override <sim_override.json>
#   --power-policy auto|explicit
#   --strict
#   --allow-partial-model

parse_sim_opts() {
  POSITIONAL=()
  NET_OPTS=()
  EXEC_OPTS=()
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
      --help|-h)
        echo "用法见脚本头部注释"; exit 0 ;;
      *)
        POSITIONAL+=("$1"); shift ;;
    esac
  done
}

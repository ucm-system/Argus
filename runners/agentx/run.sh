#!/bin/bash
# =============================================================================
# Argus Runner: agentx —— 长上下文 agentic 压测（真实 Weka agentic coding trace 回放）
#
# 契约：design/ARCHITECTURE.md §5.2（entry）、§5.3（输入文件 + env + 输出）、§5.4（输入校验）、
#       §5.4.1（公共参数）、§5.8（退出码）、§5.9（结果归一化）、§5.10（独立运行）、
#       附录 A（result.json schema 2.0）
#
# 本文件只负责「平台契约面」：输出目录、输入文件三通道、元信息与退出码。
# 参数校验、agentx 调用、result.json 归一化都在 /runner/agentx_runner.py（同目录 COPY 进镜像）。
#
# 调用形态（§5.3）：业务输入只走输入文件；平台执行 entry 时 argv 恒空。
#   输入文件读取优先级：--params-file <path> > $AG_PARAMS_FILE > /workspace/input/params.jsonl
#   JSONL 逐行解析，每行必须是一个 JSON 对象；本 Runner 的参数示例（单行）：
#     {"concurrency":8,"benchmark_duration_seconds":1800,
#      "public_dataset":"semianalysis_cc_traces_weka_062126","random_seed":42}
#   公共参数走环境变量（§5.4.1）：AG_SERVED_URL / AG_SERVED_MODEL_NAME。
#
# 独立运行（§5.10）：公共参数可用 CLI（优先于环境变量），业务参数可用输入文件：
#   /runner/run.sh --params-file /tmp/params.jsonl \
#                  --url=http://10.0.0.10:8000/v1 --model_name=Qwen3-32B
#
# 运行元信息走环境变量：AG_TASK_ID / AG_ATTEMPT / AG_RUNNER_SEQ（缺省 local / 1 / 1，§5.10）。
#
# 产物写到 $AG_OUTPUT_DIR（默认 /workspace/output）：
#   result.json（本 Runner 产物，D-74 三选一里选 result.json）
#   agentx/（agentx 原生产物：profile_export_aiperf.json/.csv、profile_export.jsonl、
#           profile_export_aiperf_timeslices.*、profile_export_console.txt、server_metrics_export.*）
#   logs/runner.log（本 Runner 日志）、logs/agentx.log（agentx 全量输出）
#   raw/（诊断用文本）
#
# 退出码（§5.8，与 result.json.status 一致）：0 成功；1 业务失败；2 参数/输入错误。
# 指标口径（附录 A）：名字只放指标，统计量与单位走字段；多组维度只能进 labels。
# =============================================================================
set -uo pipefail   # 不用 -e：任何一步失败也要把 result.json 写出来（§5.9）

VERSION="1.0.0"
OUT="${AG_OUTPUT_DIR:-/workspace/output}"

# 平台只把 /workspace/output 与 /tmp 开着写（execution §3.3）；产物目录建不出来就无从产出结果。
if ! mkdir -p "${OUT}/logs" "${OUT}/raw" 2>/dev/null; then
  echo "无法创建输出目录 ${OUT}（AG_OUTPUT_DIR 必须可写）" >&2
  exit 2
fi

# 输入文件三通道（§5.3）在这里收敛成一条路径：显式 --params-file 优先，其次 AG_PARAMS_FILE，
# 再次约定路径；三通道皆无时允许空输入（本 Runner 参数全可选，独立运行时只给 --url 也能跑）。
PARAMS=""
ARGV=()
while [ $# -gt 0 ]; do
  case "$1" in
    --params-file)   [ $# -ge 2 ] || { echo "--params-file 缺少路径" >&2; exit 2; }; PARAMS="$2"; shift 2 ;;
    --params-file=*) PARAMS="${1#*=}"; shift ;;
    *)               ARGV+=("$1"); shift ;;
  esac
done

if [ -n "${PARAMS}" ]; then
  [ -r "${PARAMS}" ] || { echo "输入文件不可读：${PARAMS}" >&2; exit 2; }
elif [ -n "${AG_PARAMS_FILE:-}" ]; then
  PARAMS="${AG_PARAMS_FILE}"
  [ -r "${PARAMS}" ] || { echo "AG_PARAMS_FILE 指向的输入文件不可读：${PARAMS}" >&2; exit 2; }
elif [ -r /workspace/input/params.jsonl ]; then
  PARAMS=/workspace/input/params.jsonl
fi

# 运行期实现在 python 侧（解析 JSONL、拼 agentx 命令、映射指标、写 result.json）。
# 用 exec 让 python 直接成为容器主进程：收到 SIGTERM 时由它自己收尾（取消语义要写进结果）。
# AGENTX_RUNNER_PY / AGENTX_PYTHON 只给离线冒烟用（tests/smoke.sh 在宿主上直接跑这套文件）；
# 镜像里两个都不设，走下面的默认值。
PY_RUNNER="${AGENTX_RUNNER_PY:-/runner/agentx_runner.py}"
PYTHON_BIN="${AGENTX_PYTHON:-python3}"
[ -r "${PY_RUNNER}" ] || { echo "找不到运行期实现：${PY_RUNNER}" >&2; exit 2; }

CMD=("${PYTHON_BIN}" "${PY_RUNNER}")
[ -n "${PARAMS}" ] && CMD+=(--params-file "${PARAMS}")
CMD+=("${ARGV[@]}")
exec "${CMD[@]}"

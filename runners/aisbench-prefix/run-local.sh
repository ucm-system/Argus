#!/bin/bash
# 本地便捷入口：**只 run，不 build**（镜像得先有；构建命令见 README「构建与推送」）。
#
#   IMAGE=registry.dev.huawei.com/argus/aisbench-prefix:0.1.0 ./run-local.sh
#   # .local/params.jsonl 存在时自动挂成输入文件
#
# 与平台里同构（§5.10）：业务参数走输入文件，公共参数走 CLI/环境变量。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${IMAGE:-ghcr.io/ucm-system/argus-runner-aisbench-prefix:0.1.0}"
OUT_DIR="${OUT_DIR:-${HERE}/../../.data/prefix-runner/local}"
URL="${URL:-http://host.docker.internal:8000/v1}"
MODEL="${MODEL:-Qwen3-32B}"
MODEL_PATH="${MODEL_PATH:-/models/Qwen3-32B}"
PARAMS="${PARAMS:-${HERE}/.local/params.jsonl}"
KEY="${ENDPOINT_API_KEY:-}"

mkdir -p "${OUT_DIR}"
OPTS=(--rm --add-host=host.docker.internal:host-gateway -e AG_TASK_ID=local
      -v "${OUT_DIR}:/workspace/output")
[ -f "${PARAMS}" ] && OPTS+=(-v "${PARAMS}:/workspace/input/params.jsonl:ro")
[ -n "${KEY}" ] && OPTS+=(-e "ENDPOINT_API_KEY=${KEY}")
ARGS=(docker run "${OPTS[@]}" "${IMAGE}" /runner/run.sh
      --url="${URL}" --model_name="${MODEL}" --model_path="${MODEL_PATH}")

echo "镜像：${IMAGE}"
echo "产物：${OUT_DIR}"
"${ARGS[@]}"
echo
echo "结论：${OUT_DIR}/result.json"

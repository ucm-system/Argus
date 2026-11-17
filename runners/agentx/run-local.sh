#!/bin/bash
# =============================================================================
# 本地便捷入口：跑一次 agentx Runner（独立运行用）
#
# ⚠ 两件事说清楚：
#   1. 这不是契约入口。契约入口只有一个：容器内的 /runner/run.sh（ARCHITECTURE §5.2）。
#      v2 契约里平台执行 entry 时 argv 恒空（§5.3）；独立运行没有平台，按 §5.10 的
#      同构形态显式传：/runner/run.sh --params-file ... --url ... --model_name ...
#   2. **本脚本不 build**。镜像由 CI 产出，这里只 pull/run（§6.2）。
#
# 用法：
#   IMAGE=registry.dev.huawei.com/argus/agentx:1.0.0 ./run-local.sh \
#     --url=http://10.0.0.10:8000/v1 --model_name=Qwen3-32B
#   业务参数走输入文件（JSONL）：.local/params.jsonl 存在时自动挂载为
#   /workspace/input/params.jsonl:ro，也可以用 --params-file 指别的路径：
#     echo '{"concurrency":8,"benchmark_duration_seconds":1800,"allow_short_run":false}' > .local/params.jsonl
#   产物默认写到 ./.local/out（可用 AG_OUTPUT_DIR 覆盖）
#
# 环境变量：IMAGE / AG_OUTPUT_DIR / ENDPOINT_API_KEY / HF_TOKEN / HF_ENDPOINT / AG_TASK_ID 等
# 语料缓存：默认落在容器内 <output>/.cache（跑完删除）。想在本地复用 1.85GB 语料，
#   用 hf_cache_dir 参数指到挂载进来的目录，例如 .local/hf -> /data/hf。
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${IMAGE:-registry.dev.huawei.com/argus/agentx:1.0.0}"
OUT_DIR="${AG_OUTPUT_DIR:-${HERE}/.local/out}"
PARAMS_SRC="${PARAMS_FILE:-${HERE}/.local/params.jsonl}"

if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
  echo "镜像 ${IMAGE} 不存在。" >&2
  echo "  镜像由 CI 产出，本脚本不 build（§6.2）。" >&2
  echo "  本地验证可指定 CI 产出的镜像：IMAGE=registry.dev.huawei.com/argus/agentx@sha256:... $0 ..." >&2
  echo "  自己构建：cd runners/agentx && docker build -f build/Dockerfile -t ${IMAGE} ." >&2
  exit 2
fi

mkdir -p "${OUT_DIR}" || exit 2

ENVARGS=(-e "AG_TASK_ID=${AG_TASK_ID:-local}" \
         -e "AG_ATTEMPT=${AG_ATTEMPT:-1}" \
         -e "AG_RUNNER_SEQ=${AG_RUNNER_SEQ:-1}" \
         -e "AG_RUNNER_IMAGE=${IMAGE}")
[ -n "${ENDPOINT_API_KEY:-}" ] && ENVARGS+=(-e "ENDPOINT_API_KEY=${ENDPOINT_API_KEY}")
[ -n "${HF_TOKEN:-}" ] && ENVARGS+=(-e "HF_TOKEN=${HF_TOKEN}")
[ -n "${HF_ENDPOINT:-}" ] && ENVARGS+=(-e "HF_ENDPOINT=${HF_ENDPOINT}")

MOUNTS=(-v "${OUT_DIR}:/workspace/output")
if [ -f "${PARAMS_SRC}" ]; then
  echo "==> 挂载输入文件 ${PARAMS_SRC} -> /workspace/input/params.jsonl:ro"
  MOUNTS+=(-v "${PARAMS_SRC}:/workspace/input/params.jsonl:ro")
fi

echo "==> docker run ${IMAGE} /runner/run.sh $*"
docker run --rm \
  -u "$(id -u):$(id -g)" \
  "${ENVARGS[@]}" \
  "${MOUNTS[@]}" \
  "${IMAGE}" /runner/run.sh "$@"
rc=$?

echo "==> exit=${rc}"
if [ -f "${OUT_DIR}/result.json" ]; then
  echo "==> ${OUT_DIR}/result.json"
  if command -v jq >/dev/null 2>&1; then jq . "${OUT_DIR}/result.json"; else cat "${OUT_DIR}/result.json"; fi
else
  echo "!! 没有产出 result.json（本 Runner 选择产出它；产物契约三选一 D-74）" >&2
fi
exit "${rc}"

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
#   IMAGE=registry.dev.huawei.com/argus/agentx:1.0.1 ./run-local.sh \
#     --url=http://10.0.0.10:8000/v1 --model_name=Qwen3-32B
#   业务参数走输入文件（JSONL）：.local/params.jsonl 存在时自动挂载为
#   /workspace/input/params.jsonl:ro，也可以用 --params-file 指别的路径：
#     echo '{"concurrency":8,"benchmark_duration_seconds":1800,"allow_short_run":false}' > .local/params.jsonl
#   产物默认写到 ./.local/out（可用 AG_OUTPUT_DIR 覆盖）
#
# 环境变量：IMAGE / AG_OUTPUT_DIR / ENDPOINT_API_KEY / HF_TOKEN / HF_ENDPOINT / AG_TASK_ID 等
# 语料：把下载好的 HF 仓库目录用 DATASET_DIR 传进来（只读挂到 /data/cc-traces），
#   参数文件里写 {"dataset_dir":"/data/cc-traces/traces.jsonl"}；不传就回落到 --public-dataset（需外网）。
#   重建缓存默认挂 ${HOME}/.cache/agentx/mmap（MMAP_CACHE_DIR 可换），第二次运行不必重建。
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${IMAGE:-registry.dev.huawei.com/argus/agentx:1.0.1}"
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

# 本地数据集目录（可选，但推荐：不挂就只能回落到 HuggingFace 下载，需要外网）。
#   DATASET_DIR=~/code/cc-traces-weka-062126 ./run-local.sh ...
# 参数文件里仍需写 {"dataset_dir":"/data/cc-traces/traces.jsonl"}。
if [ -n "${DATASET_DIR:-}" ]; then
  if [ ! -d "${DATASET_DIR}" ]; then
    echo "DATASET_DIR 不是目录：${DATASET_DIR}" >&2
    exit 2
  fi
  echo "==> 挂载本地数据集 ${DATASET_DIR} -> /data/cc-traces:ro"
  MOUNTS+=(-v "${DATASET_DIR}:/data/cc-traces:ro")
  if [ -f "${PARAMS_SRC}" ] && ! grep -Eq '"dataset_dir"[[:space:]]*:' "${PARAMS_SRC}"; then
    echo "!! 参数文件里没有 dataset_dir：本次会回落到 --public-dataset（需要外网）" >&2
  fi
fi

# 数据集重建缓存（可选）：挂上以后第二次运行不必重建（key 含 random_seed）。
MMAP_CACHE="${MMAP_CACHE_DIR:-${HOME}/.cache/agentx/mmap}"
if mkdir -p "${MMAP_CACHE}" 2>/dev/null; then
  echo "==> 挂载重建缓存 ${MMAP_CACHE} -> /data/mmap"
  MOUNTS+=(-v "${MMAP_CACHE}:/data/mmap")
  ENVARGS+=(-e "AIPERF_DATASET_MMAP_CACHE_DIR=/data/mmap")
fi

# HF 缓存（可选）：只有 hf_cache_dir 参数在用时才需要（例如 tokenizer 从 Hub 取）。
if [ -n "${HF_CACHE_DIR:-}" ] && [ -d "${HF_CACHE_DIR}" ]; then
  echo "==> 挂载 HF 缓存 ${HF_CACHE_DIR} -> /data/hf"
  MOUNTS+=(-v "${HF_CACHE_DIR}:/data/hf")
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

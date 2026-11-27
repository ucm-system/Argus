#!/bin/bash
# =============================================================================
# Argus Runner: aisbench-prefix —— 前缀缓存性能测量（固定并发点 / 并发阶梯）
#
# 封装对象：外部仓库 aisbench_auto_tools_prefix（vendored 在 /runner/tool，
# 来源 commit 与逐条本地改动见 tool/VENDORED.md）。
#
# 契约：design/ARCHITECTURE.md §5.2（entry）/§5.3（输入文件 + env + 产物）/
#       §5.4.1（公共参数）/§5.8（退出码）/§6.7（独占）/§12.4（环境变量与凭据）/
#       附录 A（result.json schema 2.0）
#
# 输入（§5.3）：业务参数只走输入文件，平台执行 entry 时 argv 恒空。
#   读取优先级：--params-file <path> > $AG_PARAMS_FILE > /workspace/input/params.jsonl
#   公共参数走环境变量（§5.4.1）：AG_SERVED_URL / AG_SERVED_MODEL_NAME / AG_MODEL_PATH
#
# 与上游用法的关系：上游是「编辑 config.py → 跑 python aisbench_test.py」，
#   站点信息（服务地址、模型名、权重路径、数据集目录、aisbench 源码根、鉴权）写在
#   config.py 里。Runner 的根文件系统只读（§6.8），所以 tool/config.py 被改写成
#   「每一项都可被 AGP_* 环境变量覆盖」——本脚本不编辑任何配置文件，只注入环境变量：
#   负载参数走命令行（上游本来就有），站点参数走 env（上游没有的那部分，见 VENDORED.md）。
#
# 产物（写 $AG_OUTPUT_DIR；D-74 三选一，本 Runner 选 result.json）：
#   result.json                 本 Runner 产物（summarize.py 从 sidecar 事件汇出）
#   logs/prefix.log             本 Runner + 工具的完整时间线
#   prefix_out/points/point-<c>/
#     events.jsonl              结构化工具事件（summarize.py 的唯一数据源）
#     aisbench.log              本次该点的工具+ais_bench 日志（上游落盘）
#     aisbench_all.log          同上（上游的累积副本）
#     aisbench_result.csv       上游的结果表（给人看的证据）
#     temp_api.py               该点生成的 ais_bench 模型配置（不含凭据）
#     work/                     ais_bench 自己的 --work-dir（configs/performances/…）
#   prefix_out/datasets/…       生成的数据集（默认按点分目录）
#
# 退出码（§5.8）：0 成功；1 业务失败（终态 FAILED）；2 参数/输入错误。无运行时重试（D-69）。
#   result.json.status / error.code 的映射见 README「退出码与 result.json」：
#     工具 0 → succeeded
#     工具 2（参数/配置错，AGP_* 给坏）→ failed VALIDATION_ERROR（exit 2）
#     工具 3（数据集生成失败）→ failed RUNNER_NONZERO_EXIT
#     工具 4（ais_bench 非零退出）→ failed RUNNER_NONZERO_EXIT（阶梯里单点失败不终止整轮）
#     工具 5（单点超时 AGP_RUN_TIMEOUT）→ failed RUNNER_TIMEOUT
#   错误码只用平台码表里有的（domain/codes.go + openapi ErrorCode）：自造的码会被
#   静默降级成 RUNNER_NONZERO_EXIT，丢掉区分信息（评审 #62 就这么发现过 SLA_* 缺码）。
# =============================================================================
set -uo pipefail   # 不用 -e：任何一步失败也要把 result.json 写出来（§5.9）

VERSION="0.1.4"
# 自身位置：镜像里是 /runner，仓库里直接跑就是 runners/aisbench-prefix——两种情况同构。
SELF="${BASH_SOURCE[0]}"
command -v readlink >/dev/null 2>&1 && SELF="$(readlink -f "${SELF}" 2>/dev/null || printf '%s' "${SELF}")"
RUNNER_DIR="${RUNNER_DIR:-$(cd "$(dirname "${SELF}")" && pwd)}"
OUT="${AG_OUTPUT_DIR:-/workspace/output}"
LOG_DIR="${OUT}/logs"
PREFIX_OUT="${OUT}/prefix_out"
POINTS_DIR="${PREFIX_OUT}/points"
RESULT="${OUT}/result.json"
TOOL_DIR="${RUNNER_TOOL_DIR:-${RUNNER_DIR}/tool}"
TOOL_ENTRY="${TOOL_DIR}/aisbench_test.py"
SUMMARIZE_PY="${RUNNER_DIR}/summarize.py"
# 解释器可覆盖（本地 venv 里跑时 PYTHON=/path/to/venv/bin/python；镜像里默认 python3）
PYTHON="${PYTHON:-python3}"

mkdir -p "${LOG_DIR}" "${POINTS_DIR}" 2>/dev/null || { echo "无法创建 ${LOG_DIR}" >&2; exit 2; }
LOG="${LOG_DIR}/prefix.log"
log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "${LOG}" >&2; }

TASK_ID="${AG_TASK_ID:-local}"
ATTEMPT_NO="${AG_ATTEMPT:-1}"
RUNNER_SEQ="${AG_RUNNER_SEQ:-1}"
# 平台当前不注入 AG_RUNNER_IMAGE（executor 渲染 Job 时没设），所以 image_ref 恒为 unknown —— 预期行为。
IMAGE_REF="${AG_RUNNER_IMAGE:-unknown}"
STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
START_EPOCH="$(date +%s)"

is_uint() { case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }
num_or() { if is_uint "$1"; then printf '%s' "$1"; else printf '%s' "$2"; fi; }
is_num() { case "$1" in ''|*[!0-9.]|*.*.*) return 1 ;; *) return 0 ;; esac; }

usage() {
  cat >&2 <<'EOF'
用法: /runner/run.sh [--params-file <jsonl>] [--url=<OpenAI base URL>]
                    [--model_name=<服务端模型名>] [--model_path=<容器内权重路径>]

输入文件（JSONL，每行一个 JSON 对象；平台内由 AG_PARAMS_FILE / /workspace/input/params.jsonl 给）:
  公共参数不进文件（§5.4.1）：url / model_name / model_path 走
  AG_SERVED_URL / AG_SERVED_MODEL_NAME / AG_MODEL_PATH。

负载（必填 concurrency；其余有默认）:
  {"concurrency":[8,16,32]}          目标并发；单个点也写成数组。多值=同一 Job 内阶梯
  {"input_len":2048,"output_len":1024,"data_num":64,"request_rate":0}
  {"dataset_type":"prefix_cache","repeat_rate":"90%","prefix_num":1,"dp":1,
   "prefix_test":true,"seed":1,"seed_per_point":true}

数据与工具:
  {"dataset":"/data/medium2.jsonl"}   现成 gsm8k 格式数据集（跳过生成，不需要 tokenizer）
  {"dataset_path":"/data/datasets"}   生成数据集的落盘目录（默认 <output>/prefix_out/datasets）
  {"work_path":"/tmp/prefix-ws"}      ais_bench 影子源码根（必须可写；勿写 /benchmark）
  {"summarizer":"default_perf"|"stable_stage"}
  {"aisbench_cmd":"ais_bench","aisbench_extra_args":"","num_warmups":"0","run_timeout":0}
  {"pods":["10.0.0.11:8000"]}         命中率快照节点，默认取被测服务的 host:port
  {"keep_pick_state":false,"no_stream":false}

其它上游开关: {"test_type":"stream"|"text","enable_think":false,"test_accuracy":false,
              "repeat":1,"npu_num":1,"length_mean":32768,"length_std":4915,
              "length_min":8192,"length_max":131072}

独立运行（§5.10）示例（离线冒烟，用镜像内的桩 ais_bench 与夹具数据集）:
  echo '{"concurrency":[4],"dataset":"/runner/fixtures/smoke_prefix.jsonl",
         "dataset_type":"normal","input_len":64,"output_len":8,"data_num":4,
         "aisbench_cmd":"python3 /runner/tests/stub_aisbench.py",
         "work_path":"/tmp/prefix-ws"}' > /tmp/p.jsonl
  /runner/run.sh --params-file /tmp/p.jsonl \
    --url=http://127.0.0.1:18080/v1 --model_name=mock-llm --model_path=/models/mock
EOF
}

# --- 写 result.json：把结论交给 summarize.py（附录 A schema 2.0）-----------------
write_result() {
  # 注意：这一串必须是**一条命令的前缀赋值**（中间不能有空行）——断成两条的话
  # AG_STARTED/AG_START_EPOCH 只是本 shell 的局部变量，子进程读不到，
  # result.json 的 duration_ms 就会恒为 0（kind 真跑时踩过）。
  AG_OUTPUT_DIR="${OUT}" \
  AG_TASK_ID="${TASK_ID}" \
  AG_ATTEMPT="$(num_or "${ATTEMPT_NO}" 1)" \
  AG_RUNNER_SEQ="$(num_or "${RUNNER_SEQ}" 1)" \
  AG_RUNNER_IMAGE="${IMAGE_REF}" \
  AG_RUNNER_VERSION="${VERSION}" \
  AG_STARTED="${STARTED}" AG_START_EPOCH="${START_EPOCH}" \
  AG_AISBENCH_VERSION="${AISBENCH_VERSION:-unknown}" AG_TOOL_VERSION="${VERSION}" \
  "${PYTHON}" "${SUMMARIZE_PY}" --out "${OUT}" || return 1
}

# 参数错误（本脚本自己的校验）：exit 2 + VALIDATION_ERROR（§5.8）
param_error() {
  local message="$1"
  log "参数错误：${message}"
  usage
  AG_STATUS=failed AG_CODE=VALIDATION_ERROR AG_REASON="${message}" \
  AG_RETRYABLE=false AG_RC="" AG_EXTRA_NOTE="本脚本参数校验未通过，未执行任何压测" \
    write_result || log "result.json 写入失败"
  exit 2
}

# 运行期硬失败（不可写、目标不可达、全部点失败等）：exit 1 + 码表里的码
hard_fail() {
  local message="$1" code="${2:-RUNNER_NONZERO_EXIT}" note="${3:-}" retry="${4:-false}"
  log "失败：${message}"
  AG_STATUS=failed AG_CODE="${code}" AG_REASON="${message}" \
  AG_RETRYABLE="${retry}" AG_RC="${TOOL_RC:-}" AG_EXTRA_NOTE="${note}" \
    write_result || log "result.json 写入失败"
  exit 1
}

# --- 输入文件解析（§5.3）：JSONL 逐行 → shell 变量 -------------------------------
# 白名单与 runner.yaml.parameters 一一对应（多了就报「未声明字段」，从严拒绝）。
parse_params_file() {
  local file="$1"
  [ -r "${file}" ] || { param_error "输入文件不存在或不可读：${file}"; }
  local parsed
  parsed="$(mktemp "${TMPDIR:-/tmp}/aisbench-prefix-params.XXXXXX")" || { hard_fail "无法创建临时文件"; }
  PARSED_TMP="${parsed}"
  if ! "${PYTHON}" - "${file}" >"${parsed}" <<'PYPARSE'
import json
import sys

KNOWN = set("""input_len output_len data_num concurrency request_rate dataset_type
prefix_num repeat_rate prefix_test seed seed_per_point dp dataset dataset_path work_path
summarizer aisbench_cmd aisbench_extra_args num_warmups run_timeout pods keep_pick_state
no_stream test_type enable_think test_accuracy repeat npu_num
length_mean length_std length_min length_max""".split())


def scalar(value):
    return isinstance(value, (str, int, float, bool))


path = sys.argv[1]
with open(path, "r", encoding="utf-8") as handle:
    for lineno, line in enumerate(handle, 1):
        text = line.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except ValueError as exc:
            print("输入第 %d 行不是合法 JSON：%s" % (lineno, exc), file=sys.stderr)
            raise SystemExit(2)
        if not isinstance(obj, dict):
            print("输入第 %d 行不是 JSON 对象：%s" % (lineno, text[:80]), file=sys.stderr)
            raise SystemExit(2)
        for key, value in obj.items():
            if key not in KNOWN:
                print("输入第 %d 行含未声明字段：%s" % (lineno, key), file=sys.stderr)
                raise SystemExit(2)
            if key in ("concurrency", "pods"):
                items = value if isinstance(value, list) else [value]
                for item in items:
                    if not scalar(item):
                        print("输入第 %d 行：%s 的每一项必须是标量" % (lineno, key), file=sys.stderr)
                        raise SystemExit(2)
                    print("%s\t%s" % (key.upper(), item))
            elif isinstance(value, bool):
                print("%s\t%s" % (key.upper(), "true" if value else "false"))
            elif isinstance(value, (int, float, str)):
                print("%s\t%s" % (key.upper(), value))
            else:
                print("输入第 %d 行：%s 必须是标量或标量数组" % (lineno, key), file=sys.stderr)
                raise SystemExit(2)
PYPARSE
  then
    rm -f "${PARSED_TMP}" 2>/dev/null || true
    param_error "输入文件解析失败（见上一行报错，带行号）"
  fi
  local key val
  while IFS=$'\t' read -r key val; do
    [ -n "${key}" ] || continue
    val="${val%$'\r'}"   # 防御：Windows 自测时 python 可能写出 CRLF
    case "${key}" in
      CONCURRENCY) CONCURRENCY_LIST+=("${val}") ;;
      PODS)        PODS_LIST+=("${val}") ;;
      # 输入字段名与 shell 变量名不一致的两处（漏了就会静默丢掉用户参数——评审 #62 同类坑）：
      # aisbench_extra_args 在脚本里叫 AISBENCH_EXTRA；summarizer 与工具名 summarize.py 撞名。
      AISBENCH_EXTRA_ARGS) AISBENCH_EXTRA="${val}" ;;
      SUMMARIZER)  SUMMARIZER_NAME="${val}" ;;
      *)           printf -v "${key}" '%s' "${val}" ;;
    esac
  done < "${PARSED_TMP}"
  rm -f "${PARSED_TMP}" 2>/dev/null || true
}

# --- CLI（独立运行通道 §5.10）：只有输入文件指针与公共参数 ------------------------
URL=""; MODEL_NAME=""; MODEL_PATH=""
INPUT_LEN=""; OUTPUT_LEN=""; DATA_NUM=""; REQUEST_RATE=""
DATASET_TYPE=""; PREFIX_NUM=""; REPEAT_RATE=""; PREFIX_TEST=""; SEED=""
SEED_PER_POINT=""; DP=""; DATASET=""; DATASET_PATH=""; WORK_PATH=""
SUMMARIZER_NAME=""; AISBENCH_CMD=""; AISBENCH_EXTRA=""; NUM_WARMUPS=""; RUN_TIMEOUT=""
KEEP_PICK_STATE=""; NO_STREAM="false"; TEST_TYPE=""; ENABLE_THINK="false"
TEST_ACCURACY="false"; REPEAT=""; NPU_NUM=""; LENGTH_MEAN=""; LENGTH_STD=""
LENGTH_MIN=""; LENGTH_MAX=""
CONCURRENCY_LIST=(); PODS_LIST=()
HELP_REQUESTED="false"; UNKNOWN=(); PARSED_FILE=""

while [ $# -gt 0 ]; do
  arg="$1"; shift
  case "${arg}" in
    --help|-h)       HELP_REQUESTED="true"; continue ;;
    --params-file)   [ $# -ge 1 ] && { PARSED_FILE="$1"; shift; continue; } || { echo "--params-file 缺少路径" >&2; exit 2; } ;;
    --params-file=*) PARSED_FILE="${arg#*=}"; continue ;;
    --url)           [ $# -ge 1 ] && { URL="$1"; shift; continue; } || { echo "--url 缺少值" >&2; exit 2; } ;;
    --url=*)         URL="${arg#*=}"; continue ;;
    --model_name)    [ $# -ge 1 ] && { MODEL_NAME="$1"; shift; continue; } || { echo "--model_name 缺少值" >&2; exit 2; } ;;
    --model_name=*)  MODEL_NAME="${arg#*=}"; continue ;;
    --model_path)    [ $# -ge 1 ] && { MODEL_PATH="$1"; shift; continue; } || { echo "--model_path 缺少值" >&2; exit 2; } ;;
    --model_path=*)  MODEL_PATH="${arg#*=}"; continue ;;
    *)               UNKNOWN+=("${arg}") ;;   # 业务参数不走 argv（§5.3）
  esac
done

if [ "${HELP_REQUESTED}" = "true" ]; then
  param_error "收到 --help，未执行任何压测（用法见上方）"
fi
if [ -n "${PARSED_FILE}" ]; then
  parse_params_file "${PARSED_FILE}"
elif [ -n "${AG_PARAMS_FILE:-}" ]; then
  parse_params_file "${AG_PARAMS_FILE}"
elif [ -f /workspace/input/params.jsonl ]; then
  parse_params_file /workspace/input/params.jsonl
fi

URL="${URL:-${AG_SERVED_URL:-}}"
MODEL_NAME="${MODEL_NAME:-${AG_SERVED_MODEL_NAME:-}}"
MODEL_PATH="${MODEL_PATH:-${AG_MODEL_PATH:-}}"
# 【kind 实测】tokenizer 目录是**硬要求**，不是"只有生成数据集才需要"：
# ais_bench 的 default_perf 汇总器自己会 `load_tokenizer(model_cfg["path"])` 来算
# 每条请求的 token 数（`workers.py → default_perf._load_details_perf_data`）。
# 于是路径给错/不存在时，压测会跑完、汇总阶段才报 UTILS-CFG-002 / UTILS-FILE-003，
# 白烧几分钟。这里提前判，并把该怎么做写进消息。
if [ -z "${MODEL_PATH}" ]; then
  param_error "缺少容器内 tokenizer 路径（公共参数 AG_MODEL_PATH，§5.4.1）：ais_bench 的性能汇总器要用它算 token 数。部署模式由 service-deploy 导出；外接模式（service:）目前平台不注入，可在 Task 里给 env: {AG_MODEL_PATH: /models/<模型名>}，或按 D-63 配统一共享挂载"
fi
case "${MODEL_PATH}" in
  /*) [ -d "${MODEL_PATH}" ] || param_error "AG_MODEL_PATH 指向的目录不存在：${MODEL_PATH}（容器内绝对路径；由 D-63 统一共享挂载或 Profile 挂载提供，本 Runner 不代管权重）" ;;
esac

[ "${#UNKNOWN[@]}" -eq 0 ] || param_error "无法识别的参数：${UNKNOWN[*]}（业务参数只走输入文件；公共参数用 --url / --model_name / --model_path）"
[ -n "${URL}" ] || param_error "缺少被测服务地址（--url 或环境变量 AG_SERVED_URL，公共参数 §5.4.1）"
[ "${#CONCURRENCY_LIST[@]}" -gt 0 ] || param_error "缺少 concurrency（输入文件字段；要测的目标并发，如 {\"concurrency\":[8,16,32]}）"

# 缺省值不进 argv，交给工具/这里的默认（与 runner.yaml 的 default 一致）
INPUT_LEN="${INPUT_LEN:-2048}"; OUTPUT_LEN="${OUTPUT_LEN:-1024}"; DATA_NUM="${DATA_NUM:-64}"
REQUEST_RATE="${REQUEST_RATE:-0}"; DATASET_TYPE="${DATASET_TYPE:-prefix_cache}"
PREFIX_NUM="${PREFIX_NUM:-1}"; REPEAT_RATE="${REPEAT_RATE:-90%}"; SEED="${SEED:-1}"
DP="${DP:-1}"; PREFIX_TEST="${PREFIX_TEST:-true}"; SEED_PER_POINT="${SEED_PER_POINT:-true}"
KEEP_PICK_STATE="${KEEP_PICK_STATE:-false}"; TEST_TYPE="${TEST_TYPE:-stream}"
SUMMARIZER_NAME="${SUMMARIZER_NAME:-default_perf}"; REPEAT="${REPEAT:-1}"; NPU_NUM="${NPU_NUM:-1}"
NUM_WARMUPS="${NUM_WARMUPS-0}"; RUN_TIMEOUT="${RUN_TIMEOUT:-0}"; AISBENCH_CMD="${AISBENCH_CMD:-ais_bench}"

is_uint "${INPUT_LEN}"  || param_error "input_len 必须是正整数"
is_uint "${OUTPUT_LEN}" || param_error "output_len 必须是正整数"
is_uint "${DATA_NUM}"   || param_error "data_num 必须是正整数"
is_uint "${SEED}"       || param_error "seed 必须是非负整数（0=不固定，不可复现）"
is_uint "${DP}"         || param_error "dp 必须是正整数"
is_uint "${PREFIX_NUM}" || param_error "prefix_num 必须是正整数"
is_uint "${REPEAT}"     || param_error "repeat 必须是正整数"
is_uint "${NPU_NUM}"    || param_error "npu_num 必须是正整数"
is_num  "${REQUEST_RATE}" || param_error "request_rate 必须是非负数（0=不限速）"
is_num  "${RUN_TIMEOUT}"  || param_error "run_timeout 必须是非负数（秒，0=不限）"
for c in "${CONCURRENCY_LIST[@]}"; do is_uint "${c}" || param_error "concurrency 的每一项必须是正整数，收到：${c}"; done
# 去重：重复的并发值会共用同一个产物目录（point-<c>），后者覆盖前者 → 结论串台。
# 顺带也保证 result.json 的指标键唯一（附录 A 的 name+statistic+labels 不重复）。
DEDUPED=()
for c in "${CONCURRENCY_LIST[@]}"; do
  already="false"
  for d in ${DEDUPED[@]+"${DEDUPED[@]}"}; do [ "${d}" = "${c}" ] && already="true" && break; done
  [ "${already}" = "true" ] && { log "concurrency 有重复值 ${c}，已去重"; continue; }
  DEDUPED+=("${c}")
done
CONCURRENCY_LIST=("${DEDUPED[@]}")
case "${DATASET_TYPE}" in prefix_cache|normal) ;; *) param_error "dataset_type 只能是 prefix_cache 或 normal" ;; esac
case "${TEST_TYPE}" in stream|text) ;; *) param_error "test_type 只能是 stream 或 text" ;; esac
case "${SUMMARIZER_NAME}" in default_perf|stable_stage) ;; *) param_error "summarizer 只能是 default_perf 或 stable_stage" ;; esac
for flag in PREFIX_TEST SEED_PER_POINT KEEP_PICK_STATE NO_STREAM ENABLE_THINK TEST_ACCURACY; do
  case "${!flag}" in true|false) ;; *) param_error "${flag} 只能是 true 或 false（收到 ${!flag}）" ;; esac
done
case "${REPEAT_RATE}" in
  *[!0-9.%]*|"") param_error "repeat_rate 形如 \"90%\" 或 0.9，收到：${REPEAT_RATE}" ;;
esac
if [ "${SEED_PER_POINT}" = "true" ] && [ "${#CONCURRENCY_LIST[@]}" -gt 1 ] && [ -n "${DATASET}" ]; then
  log "给了 dataset 且 seed_per_point=true：现成数据集不能被重新生成，按 false 处理（各点复用同一份数据集）"
  SEED_PER_POINT="false"
fi

# --- 被测地址归一化 ------------------------------------------------------------
# 平台给的是 OpenAI base URL（形如 http://host:8000/v1）。ais_bench 用
# urljoin(base, "v1/chat/completions") 拼接口，所以**只去掉尾斜杠、不裁掉 /v1**：
# 裁掉会变成 .../chat/completions，留着尾斜杠会变成 .../v1/v1/chat/completions。
# 剩下的（host/port 拆分、IPv6、默认端口、host_ip 是否合法 IP）交给 config.py +
# prefix_env.resolve_target() 统一处理，本脚本不重复实现第二套解析。
TARGET="${URL}"
while [ "${TARGET%/}" != "${TARGET}" ]; do TARGET="${TARGET%/}"; done
export AGP_URL="${TARGET}"

# --- work_path：不存在就建，建不了/不可写就硬失败 -------------------------------
# 工具要往 work_path 下写模型配置与数据集软链，平台 Job 的根文件系统只读
# （只有 /workspace/output 与 /tmp 可写），所以默认落在 /tmp/prefix-ws。
if [ ! -d "${WORK_PATH}" ]; then
  mkdir -p "${WORK_PATH}" 2>/dev/null && log "work_path 不存在，已创建：${WORK_PATH}"
fi
[ -d "${WORK_PATH}" ] || hard_fail "work_path 不可用：${WORK_PATH}（不存在且建不出来）。容器内请用 /tmp 下的路径，例如 /tmp/prefix-ws —— 平台 Job 根文件系统只读，/benchmark 写不进去"
if ! { : > "${WORK_PATH}/.argus-write-probe" 2>/dev/null; }; then
  hard_fail "work_path 不可写：${WORK_PATH}。容器内请用 /tmp 下的路径，例如 /tmp/prefix-ws"
fi
rm -f "${WORK_PATH}/.argus-write-probe" 2>/dev/null || true

# --- ais_bench 接线：影子源码根 + 数据集缓存锚点 --------------------------------
# 见 tool/../../README「work_path 接线」：
#   * 数据集：ais_bench 把**相对**数据集路径拼在 AIS_BENCH_DATASETS_CACHE 下
#     （datasets/utils/datasets.py: get_cache_dir()），于是
#     path='ais_bench/datasets/gsm8k' 命中 <work_path>/ais_bench/datasets/gsm8k。
#   * 模型配置：--config-dir 让 ais_bench 在 <config-dir>/models 下找 vllm_api_chat_temp。
export AIS_BENCH_DATASETS_CACHE="${AIS_BENCH_DATASETS_CACHE:-${WORK_PATH}}"
export AGP_WORK_PATH="${WORK_PATH}"

export AGP_MODEL_NAME="${MODEL_NAME}"
export AGP_MODEL_PATH="${MODEL_PATH}"
export AGP_SUMMARIZER="${SUMMARIZER_NAME}"
export AGP_AISBENCH_CMD="${AISBENCH_CMD}"
export AGP_NUM_WARMUPS="${NUM_WARMUPS}"
export AGP_RUN_TIMEOUT="${RUN_TIMEOUT}"
export AGP_SEED_GLOBAL=1
if [ "${KEEP_PICK_STATE}" = "true" ]; then export AGP_RESET_PICK_STATE=0; else export AGP_RESET_PICK_STATE=1; fi
# 挑题记录放独立目录（不跟 CWD 走）：跨 attempt 不累积，同一 seed 才可复现。
# 允许外部预置 AGP_STATE_DIR（离线冒烟要用一个可控目录验证「记录被标满」的行为）。
STATE_DIR="${AGP_STATE_DIR:-${TMPDIR:-/tmp}/prefix-state}"
mkdir -p "${STATE_DIR}" || hard_fail "无法创建状态目录 ${STATE_DIR}"
export AGP_STATE_DIR="${STATE_DIR}"
if [ "${KEEP_PICK_STATE}" != "true" ]; then
  rm -f "${STATE_DIR}/picked_ids.txt" 2>/dev/null || true
fi
EXTRA_ARGS="${AISBENCH_EXTRA:-}"
if [ "${WORK_PATH}" != "/benchmark" ]; then
  EXTRA_ARGS="${EXTRA_ARGS:+${EXTRA_ARGS} }--config-dir ${WORK_PATH}/ais_bench/benchmark/configs"
fi
[ -n "${EXTRA_ARGS}" ] && export AGP_AISBENCH_EXTRA_ARGS="${EXTRA_ARGS}"
if [ "${#PODS_LIST[@]}" -gt 0 ]; then
  export AGP_POD_INFO="$(printf '%s,' "${PODS_LIST[@]}" | sed 's/,$//')"
fi
export PYTHONUNBUFFERED=1
[ -n "${ENDPOINT_API_KEY:-}" ] && log "已通过环境变量注入 ENDPOINT_API_KEY（不进 argv、不落日志、不写进生成的模型配置）"

AISBENCH_VERSION="$(${PYTHON} -c 'import ais_bench.benchmark as m; print(getattr(m, "__version__", "unknown"))' 2>/dev/null || echo unknown)"
log "ais_bench：cmd=${AISBENCH_CMD} version=${AISBENCH_VERSION}"
log "寻址：url=${TARGET} model_name=${MODEL_NAME} model_path=${MODEL_PATH}（model_path 必须是容器内可加载的 tokenizer 目录）"

# --- 被测服务可达性预检（只为把错误说清楚，不替代 endpoint-probe）----------------
# 要给足耐心：kind 上新建 Pod 的头 1-2 分钟里 pod→service 的转发可能还没就绪
# （本机实测：precheck 第 7 次才 HTTP 200，而这里 3 次×10s 直接误判「不可达」，
# 白烧一个 Task）。12 次 × 10s + 5s 间隔 ≈ 3 分钟，远小于平台 watchdog。
if [ "${TEST_ACCURACY}" != "true" ]; then
  PROBE_OK="false"
  for attempt in $(seq 1 12); do
    if "${PYTHON}" - "${TARGET}" <<'PYPROBE' >&2
import socket, sys
from urllib.parse import urlparse

url = sys.argv[1]
parsed = urlparse(url if "://" in url else "http://" + url)
host = parsed.hostname or ""
port = parsed.port or (443 if parsed.scheme == "https" else 80)
try:
    with socket.create_connection((host, port), timeout=10):
        sys.exit(0)
except OSError as exc:
    print(f"[probe] TCP 连不上 {host}:{port}：{exc}", file=sys.stderr)
    sys.exit(1)
PYPROBE
    then PROBE_OK="true"; [ "${attempt}" -gt 1 ] && log "可达性预检第 ${attempt} 次通过"; break; fi
    [ $(( attempt % 4 )) -eq 0 ] && log "可达性预检已重试 ${attempt} 次仍未通：${TARGET}"
    sleep 5
  done
  if [ "${PROBE_OK}" != "true" ]; then
    hard_fail "被测服务不可达：${TARGET}（约 3 分钟内 12 次 TCP 连接都失败）。若 precheck 是通过的，说明是集群网络还没就绪或 NetworkPolicy 没放行该端口" \
      "ENDPOINT_UNREACHABLE" "未执行任何压测" "true"
  fi
fi
# --- 清掉上次残留，保证本次产物不被复用（本地复用同一 OUT 时尤其重要）-----------
if [ -d "${POINTS_DIR}" ]; then
  rm -rf "${POINTS_DIR}" 2>/dev/null && log "已清理上次残留的 ${POINTS_DIR}"
fi
mkdir -p "${POINTS_DIR}" || hard_fail "无法创建产物目录 ${POINTS_DIR}"
DATASETS_ROOT="${DATASET_PATH:-${PREFIX_OUT}/datasets}"

# --- 逐点执行 ------------------------------------------------------------------

POINT_FAILURES=()
OK_POINTS=0
TOOL_RC=0
INDEX=0
for C in "${CONCURRENCY_LIST[@]}"; do
  POINT_SEED="${SEED}"
  if [ "${SEED_PER_POINT}" = "true" ] && [ "${SEED}" != "0" ]; then
    POINT_SEED=$(( SEED + INDEX ))
  fi
  if [ "${SEED_PER_POINT}" = "true" ]; then
    POINT_DATASET_PATH="${DATASETS_ROOT}/c${C}"
  else
    POINT_DATASET_PATH="${DATASETS_ROOT}/shared"
  fi
  POINT_DIR="${POINTS_DIR}/point-${C}"
  mkdir -p "${POINT_DIR}" || { hard_fail "无法创建点目录 ${POINT_DIR}"; }
  if [ ! -d "${POINT_DATASET_PATH}" ]; then
    mkdir -p "${POINT_DATASET_PATH}" 2>/dev/null || hard_fail "dataset_path 不可用：${POINT_DATASET_PATH}（建不出来，容器内请落在 /workspace/output 或 /tmp 下）"
  fi

  CMD=("${PYTHON}" "${TOOL_ENTRY}")
  add_opt() { [ -n "$2" ] && CMD+=("$1" "$2"); return 0; }
  add_opt --input_len "${INPUT_LEN}"
  add_opt --output_len "${OUTPUT_LEN}"
  add_opt --data_num "${DATA_NUM}"
  add_opt --concurrency "${C}"
  add_opt --request_rate "${REQUEST_RATE}"
  add_opt --test_type "${TEST_TYPE}"
  add_opt --dataset "${DATASET}"
  add_opt --dataset_type "${DATASET_TYPE}"
  add_opt --prefix_num "${PREFIX_NUM}"
  add_opt --repeat_rate "${REPEAT_RATE}"
  add_opt --seed "${POINT_SEED}"
  add_opt --dp "${DP}"
  add_opt --repeat "${REPEAT}"
  add_opt --npu_num "${NPU_NUM}"
  add_opt --length_mean "${LENGTH_MEAN}"
  add_opt --length_std "${LENGTH_STD}"
  add_opt --length_min "${LENGTH_MIN}"
  add_opt --length_max "${LENGTH_MAX}"
  [ "${PREFIX_TEST}" = "true" ] && CMD+=(--prefix_test)
  [ "${ENABLE_THINK}" = "true" ] && CMD+=(--enable_think)
  [ "${TEST_ACCURACY}" = "true" ] && CMD+=(--test_accuracy)

  POINT_ENV=(
    "AGP_DATASET_PATH=${POINT_DATASET_PATH}"
    "AGP_OUTPUT_DIR=${POINT_DIR}/work"
    "AGP_EVENTS=${POINT_DIR}/events.jsonl"
    "AGP_CONCURRENCY=${C}"
  )
  log "并发点 c=${C}：seed=${POINT_SEED} dataset_path=${POINT_DATASET_PATH} 输出=${POINT_DIR}"
  # 工具的日志（含 ais_bench 的输出）一律落进 LOG；NO_STREAM=true 时不打到 stdout
  #（长跑一屏几万行，平台日志框只装 stdout，见 §5.2）。
  if [ "${NO_STREAM}" = "true" ]; then
    ( cd "${POINT_DIR}" || exit 2; env "${POINT_ENV[@]}" "${CMD[@]}" ) >>"${LOG}" 2>&1
    RC=$?
  else
    ( cd "${POINT_DIR}" || exit 2; env "${POINT_ENV[@]}" "${CMD[@]}" ) 2>&1 | tee -a "${LOG}"
    RC="${PIPESTATUS[0]}"
  fi
  TOOL_RC="${RC}"
  log "并发点 c=${C} 结束：工具退出码 ${RC}"

  case "${RC}" in
    0) OK_POINTS=$(( OK_POINTS + 1 )) ;;
    2) POINT_FAILURES+=("c=${C} 参数/配置错误（退出码 2）")
       log "参数/配置错误，后续点同样会失败，停止阶梯"
       hard_fail "工具报参数/配置错误（并发点 c=${C}，退出码 2）：见 logs/prefix.log 与 ${POINT_DIR}/events.jsonl" \
         "VALIDATION_ERROR" "常见原因：AGP_* 环境变量给坏、dataset 指向不存在的文件" ;;
    3) POINT_FAILURES+=("c=${C} 数据集生成失败（退出码 3）")
       hard_fail "生成数据集失败（并发点 c=${C}，退出码 3）。本 Runner 已在跑前清理挑题记录（picked_ids），所以这次不是 FAQ 里那种跨次累积；更可能是 prefix_num/data_num 超出 GSM8K 约 1300 行的容量，或 dataset_path/model_path 不对" \
         "RUNNER_NONZERO_EXIT" "把 data_num/prefix_num 调小、或给 dataset 指定现成数据集" ;;
    5) POINT_FAILURES+=("c=${C} 超时（退出码 5）")
       hard_fail "并发点 c=${C} 超时（run_timeout=${RUN_TIMEOUT}s），已按进程组终止 ais_bench" \
         "RUNNER_TIMEOUT" "调大 run_timeout，或减小并发/数据量后重跑" "true" ;;
    *) POINT_FAILURES+=("c=${C} 失败（退出码 ${RC}）") ;;
  esac
  INDEX=$(( INDEX + 1 ))
done

# --- 结论 ----------------------------------------------------------------------
if [ "${OK_POINTS}" -gt 0 ]; then
  STATUS="succeeded"; CODE=""; EXIT_CODE=0; RETRYABLE="false"
  REASON="完成 ${OK_POINTS}/${#CONCURRENCY_LIST[@]} 个并发点"
else
  STATUS="failed"; CODE="RUNNER_NONZERO_EXIT"; EXIT_CODE=1; RETRYABLE="false"
  REASON="${#CONCURRENCY_LIST[@]} 个并发点全部失败：$(printf '%s; ' ${POINT_FAILURES[@]+"${POINT_FAILURES[@]}"})"
fi
NOTE="$( [ "${#POINT_FAILURES[@]}" -gt 0 ] && printf '失败的并发点：%s' "$(printf '%s; ' ${POINT_FAILURES[@]+"${POINT_FAILURES[@]}"})" )"

log "结论：status=${STATUS} code=${CODE:-<无>} 成功点=${OK_POINTS}/${#CONCURRENCY_LIST[@]} ${REASON}"
if ! AG_STATUS="${STATUS}" AG_CODE="${CODE}" AG_REASON="${REASON}" \
     AG_RETRYABLE="${RETRYABLE}" AG_RC="${TOOL_RC}" AG_EXTRA_NOTE="${NOTE}" write_result; then
  log "result.json 写入失败（本 Runner 的产物即 result.json，D-74 三选一）"
  printf '{"schema_version":"2.0","task_id":"%s","attempt_no":%s,"runner_seq":%s,"runner":{"name":"aisbench-prefix","version":"%s","image_ref":"%s"},"status":"failed","started_at":"%s","finished_at":"%s","metrics":[],"error":{"code":"RUNNER_NONZERO_EXIT","message":"result.json 写入失败","detail":{},"retryable":false},"artifacts":[]}\n' \
    "${TASK_ID}" "$(num_or "${ATTEMPT_NO}" 1)" "$(num_or "${RUNNER_SEQ}" 1)" "${VERSION}" "${IMAGE_REF}" \
    "${STARTED}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${RESULT}" 2>/dev/null || true
  exit 2
fi
log "产物：${PREFIX_OUT}（points/point-*/{events.jsonl,aisbench.log,aisbench_result.csv,work/}、datasets/）"
exit "${EXIT_CODE}"

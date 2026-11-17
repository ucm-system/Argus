#!/bin/bash
# =============================================================================
# agentx Runner 的离线冒烟（不需要镜像、不需要被测服务）
#
# 覆盖四件事：
#   1. runner.yaml 的契约自检 + 默认值漂移（tests/test_runner_yaml.py）
#   2. 参数校验 / 命令映射 / 结果归一化 / 退出码 / 密钥不落盘（tests/test_result_mapping.py）
#   3. **bash entry 本身**的契约面（输入三通道、输出目录、退出码）——用 stub 顶替 agentx
#   4. 产出的 result.json 能过平台 openapi 的 Result schema（缺 PyYAML/jsonschema 时跳过）
#
# 跑法：
#   bash runners/agentx/tests/smoke.sh
#   # 或容器内（stub 镜像，不进发布矩阵）：
#   docker build -f runners/agentx/tests/Dockerfile.stub runners/agentx -t agentx-runner:stub
#   docker run --rm agentx-runner:stub
#
# 依赖：python3（或设 AGENTX_PYTHON）。不依赖 agentx 本体 —— stub 只复现产物形状。
# 退出码：0 全通过；1 有失败。
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RUNNER_DIR="$(cd "${HERE}/.." && pwd)"
REPO="$(cd "${RUNNER_DIR}/../.." && pwd)"
PYTHON="${AGENTX_PYTHON:-python3}"
STUB="${HERE}/stub_agentx.py"

# 临时工作目录：优先系统 /tmp；受限环境（只允许写仓库）回落到 .data/（已 gitignore）。
WORK="$(mktemp -d 2>/dev/null || true)"
if [ -z "${WORK}" ] || ! ( : > "${WORK}/.probe" ) 2>/dev/null; then
  WORK="${REPO}/.data/agentx-runner/smoke"
  rm -rf "${WORK}"
  mkdir -p "${WORK}" || exit 2
fi
rm -f "${WORK}/.probe"

FAILED=0
pass() { printf '  ok   %s\n' "$*"; }
fail() { printf '  FAIL %s\n' "$*" >&2; FAILED=$((FAILED + 1)); }

cleanup() { rm -rf "${WORK}"; }
trap cleanup EXIT

echo "== 0. 解释器：$("${PYTHON}" -V 2>&1)"
if ! "${PYTHON}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "需要 Python >= 3.11（AGENTX_PYTHON 可指定解释器）" >&2
  exit 2
fi

echo "== 1/2. Python 侧回归"
"${PYTHON}" "${HERE}/test_runner_yaml.py" || FAILED=$((FAILED + 1))
"${PYTHON}" "${HERE}/test_result_mapping.py" || FAILED=$((FAILED + 1))

# --- 3. bash entry 的契约面 ---------------------------------------------------
echo "== 3. bash entry（run.sh）端到端"
export AGENTX_RUNNER_PY="${RUNNER_DIR}/agentx_runner.py"
export AGENTX_PYTHON="${PYTHON}"
export AG_TASK_ID="task-smoke"
export AG_ATTEMPT="2"
export AG_RUNNER_SEQ="7"

PARAMS="${WORK}/params.jsonl"
cat > "${PARAMS}" <<EOF
{"concurrency":4,"num_dataset_entries":8,"agentx_cmd":"${PYTHON} ${STUB}"}
EOF

OUT_OK="${WORK}/out-ok"
if AG_OUTPUT_DIR="${OUT_OK}" bash "${RUNNER_DIR}/run.sh" --params-file "${PARAMS}" \
     --url=http://127.0.0.1:8000/v1 --model_name=mock-llm; then
  pass "退出码 0"
else
  fail "退出码非 0"
fi

RESULT="${OUT_OK}/result.json"
if [ -f "${RESULT}" ]; then
  pass "产出 result.json"
  "${PYTHON}" - "$RESULT" <<'PY' || FAILED=$((FAILED + 1))
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
checks = {
    "status=succeeded": data.get("status") == "succeeded",
    "metrics>=15": len(data.get("metrics") or []) >= 15,
    "artifact 相对路径": all(not a["path"].startswith("/") for a in data.get("artifacts") or []),
    "task_id/attempt/seq 透传": (data.get("task_id"), data.get("attempt_no"), data.get("runner_seq")) == ("task-smoke", 2, 7),
    "workload 有 concurrency": data.get("workload", {}).get("concurrency") == 4,
    "缓存已清理": True,
}
bad = [k for k, ok in checks.items() if not ok]
for k, ok in checks.items():
    print(("  ok   " if ok else "  FAIL ") + k)
sys.exit(1 if bad else 0)
PY
else
  fail "没有 result.json"
fi

# 输入三通道：AG_PARAMS_FILE 生效（不传 --params-file）
OUT_ENV="${WORK}/out-env"
if AG_OUTPUT_DIR="${OUT_ENV}" AG_PARAMS_FILE="${PARAMS}" bash "${RUNNER_DIR}/run.sh" \
     --url=http://127.0.0.1:8000/v1 --model_name=mock-llm; then
  pass "AG_PARAMS_FILE 通道可用"
else
  fail "AG_PARAMS_FILE 通道不可用"
fi

# 显式路径不存在 → 退出码 2（§5.3）
AG_OUTPUT_DIR="${WORK}/out-bad" bash "${RUNNER_DIR}/run.sh" --params-file "${WORK}/nope.jsonl" \
  --url=http://127.0.0.1:8000/v1 --model_name=mock-llm >/dev/null 2>&1
[ $? -eq 2 ] && pass "输入文件缺失 → 退出码 2" || fail "输入文件缺失没有返回 2"

# agentx 失败 → 退出码 1 + 错误码（取消/失败都要留下可读结果，§5.9）
OUT_FAIL="${WORK}/out-fail"
STUB_EXIT=1 AG_OUTPUT_DIR="${OUT_FAIL}" bash "${RUNNER_DIR}/run.sh" --params-file "${PARAMS}" \
  --url=http://127.0.0.1:8000/v1 --model_name=mock-llm >/dev/null 2>&1
rc=$?
[ "${rc}" -eq 1 ] && pass "agentx 失败 → 退出码 1" || fail "agentx 失败返回了 ${rc} 而不是 1"
"${PYTHON}" - "${OUT_FAIL}/result.json" <<'PY' || FAILED=$((FAILED + 1))
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
ok = data.get("status") == "failed" and (data.get("error") or {}).get("code") == "RUNNER_NONZERO_EXIT"
print(("  ok   " if ok else "  FAIL ") + "失败结果带 RUNNER_NONZERO_EXIT")
sys.exit(0 if ok else 1)
PY

# --- 4. 结果 schema ----------------------------------------------------------
echo "== 4. result.json ↔ openapi Result"
"${PYTHON}" "${HERE}/validate_result_schema.py" "${RESULT}" "${OUT_FAIL}/result.json" || FAILED=$((FAILED + 1))

echo
if [ "${FAILED}" -ne 0 ]; then
  echo "SMOKE: FAIL（${FAILED} 项）" >&2
  exit 1
fi
echo "SMOKE: PASS"

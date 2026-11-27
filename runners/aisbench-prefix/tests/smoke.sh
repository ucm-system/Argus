#!/bin/bash
# =============================================================================
# aisbench-prefix 离线冒烟（**唯一入口**）：不碰真模型、不碰真 ais_bench，
# 用 tests/mock_endpoint.py（假服务，带 /metrics 计数器）+ tests/stub_aisbench.py
# （假 ais_bench：读模型配置、打印能被 save_file.get_data 解析的汇总表、推流量）
# + tests/fakepkgs/transformers（逐字符假 tokenizer）把整条链路跑通。
#
# 两种跑法（同一份脚本）：
#   1) 在 stub 镜像里：docker run --rm --entrypoint bash <stub-img> /runner/tests/smoke.sh
#   2) 在任意 Linux 里直接跑（需要 python3 + pandas + requests）：
#        bash runners/aisbench-prefix/tests/smoke.sh
#
# 断言的八件事（都是本轮反馈里点名要修的）：
#   A 成功路径：阶梯 [2,4] → exit 0、两点指标、hit_rate 有值、artifacts 齐全
#   B 可复现：同参数同 seed 两次 → 生成的数据集逐字节相同
#   C picked_ids：记录被标满时——默认（清理）照样成功；keep_pick_state=true 则失败且非零
#   D 单点失败：ais_bench 崩 → 不再被 tee 吞掉（pipefail）；其余点仍算数
#   E 超时：run_timeout 触发 → RUNNER_TIMEOUT
#   F 参数错误：缺 concurrency / 未声明字段 → exit 2 + VALIDATION_ERROR
#   G 凭据零泄漏：产物目录与 work_path 里 grep 不到明文 key
#   H result.json 结构合法（装了 pyyaml+jsonschema 时按 openapi Result 校验）
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RUNNER_DIR="${RUNNER_DIR:-$(cd "${HERE}/.." && pwd)}"
RUN="${RUNNER_DIR}/run.sh"
TOOL_DIR="${RUNNER_TOOL_DIR:-${RUNNER_DIR}/tool}"
PYTHON="${PYTHON:-python3}"
if [[ "${RUNNER_DIR}" == /mnt/* ]]; then DEFAULT_WORK="/tmp/prefix-runner-smoke"; else DEFAULT_WORK="${RUNNER_DIR}/../../.data/prefix-runner/smoke"; fi
WORK="${SMOKE_WORK:-${DEFAULT_WORK}}"
PORT="${SMOKE_PORT:-18099}"
MODEL="mock-llm"
SECRET="sk-tes…890Q"
STUB="${RUNNER_DIR}/tests/stub_aisbench.py"
MOCK="${RUNNER_DIR}/tests/mock_endpoint.py"
FIXTURE="${RUNNER_DIR}/tests/fixtures/smoke_prefix.jsonl"

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ✓ $*"; }
bad()  { FAIL=$((FAIL+1)); echo "  ✗ $*"; }
# jqa <result.json> <path>：点号路径，`[]` 表示「对列表逐项取后面的字段」。
#   metrics[].name          → 逐行打印所有 metrics 的 name
#   error.code              → 标量
#   artifacts               → 数组长度
jqa() { "${PYTHON}" - "$1" "$2" <<'PYQA'
import json, sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
parts = sys.argv[2].split(".")


def walk(node, index):
    if index >= len(parts):
        yield node
        return
    part = parts[index]
    if part == "[]":
        items = node if isinstance(node, list) else [node]
        for item in items:
            yield from walk(item, index + 1)
    elif part == "":
        yield from walk(node, index + 1)
    else:
        if isinstance(node, list) and part.isdigit():
            node = node[int(part)]
        elif isinstance(node, dict) and part in node:
            node = node[part]
        elif part == "len()" or (index == len(parts) - 1 and isinstance(node, list)):
            yield len(node)
            return
        else:
            return
    yield from walk(node, index + 1)


out = []
for value in walk(data, 0):
    out.append(json.dumps(value, ensure_ascii=False)
               if isinstance(value, (dict, list)) else str(value))
print("\n".join(out))
PYQA
}
has_metric() {  # <result.json> <name> <labelKey=labelValue>...
    "${PYTHON}" - "$1" "$2" "${@:3}" <<'PYHAS'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
name = sys.argv[2]
want = dict(kv.split("=", 1) for kv in sys.argv[3:])
for metric in data.get("metrics", []):
    if metric.get("name") != name:
        continue
    labels = {str(k): str(v) for k, v in (metric.get("labels") or {}).items()}
    if all(labels.get(k) == v for k, v in want.items()):
        sys.exit(0)
sys.exit(1)
PYHAS
}
jlen() {  # <file> <dotted.path> → 该处数组的长度
    "${PYTHON}" - "$1" "$2" <<'JLEN'
import json, sys
node = json.load(open(sys.argv[1], encoding="utf-8"))
for part in sys.argv[2].split("."):
    if not part:
        continue
    node = node[int(part)] if isinstance(node, list) else node[part]
print(len(node) if isinstance(node, (list, dict, str)) else 0)
JLEN
}

[ -x "${RUN}" ] || [ -f "${RUN}" ] || { echo "找不到 ${RUN}"; exit 2; }
rm -rf "${WORK}"; mkdir -p "${WORK}"
export RUNNER_TOOL_DIR="${TOOL_DIR}"
export PYTHONPATH="${RUNNER_DIR}/tests/fakepkgs${PYTHONPATH:+:${PYTHONPATH}}"
export STUB_WORK_DIR=""      # 让桩靠 --config-dir 找配置（更贴近真 ais_bench）

WORK_PATH="${WORK}/ws"
mkdir -p "${WORK_PATH}" "${WORK}/model_weights"      # model_weights 只当 tokenizer 占位（假 tokenizer 不看内容）
export AGP_WORK_PATH="${WORK_PATH}"

echo "== 起 mock 服务 :${PORT} =="
ENDPOINT_API_KEY="${SECRET}" "${PYTHON}" "${MOCK}" --port "${PORT}" --model "${MODEL}" \
    >"${WORK}/mock.log" 2>&1 &
MOCK_PID=$!
trap 'kill "${MOCK_PID}" 2>/dev/null' EXIT
READY=0
for _ in $(seq 1 60); do
    if "${PYTHON}" - "${PORT}" <<'PYWAIT' 2>/dev/null
import socket, sys
with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=1):
    pass
PYWAIT
    then READY=1; break; fi
    sleep 0.25
done
if [ "${READY}" != "1" ]; then
    bad "mock 服务没起来（见 ${WORK}/mock.log）"; exit 1
fi
ok "mock 服务就绪"

# run_prefix <case-name> <params-json> [额外 env ...]
CASE_RC=0
run_prefix() {
    local name="$1" params="$2"; shift 2
    local out="${WORK}/${name}"
    mkdir -p "${out}"
    printf '%s\n' "${params}" > "${out}/params.jsonl"
    ( cd "${out}" && env PYTHON="${PYTHON}" \
        AG_OUTPUT_DIR="${out}" \
        AG_PARAMS_FILE="${out}/params.jsonl" \
        AG_SERVED_URL="http://127.0.0.1:${PORT}/v1" \
        AG_SERVED_MODEL_NAME="${MODEL}" \
        AG_MODEL_PATH="${WORK}/model_weights" \
        ENDPOINT_API_KEY="${SECRET}" \
        "$@" bash "${RUN}" ) >"${out}/stdout.log" 2>&1
    CASE_RC=$?
    CASE_OUT="${out}"
}

base_params() {   # 公共的最小一份：假 tokenizer 生成数据集 + 阶梯 + 桩 ais_bench
    # aisbench_cmd 走**输入文件**（不是 env）——顺带把「参数映射」这条路径也测了。
    local seed="${1:-1}" extra="${2:-}"
    extra="${extra#,}"
    [ -n "${extra}" ] && extra=",${extra}"
    printf '{"input_len":128,"output_len":4,"data_num":8,"request_rate":0,"concurrency":[2,4],"seed":%s,"repeat_rate":"75%%","prefix_num":1,"dp":1,"dataset_type":"prefix_cache","prefix_test":true,"work_path":"%s","aisbench_cmd":"%s %s"%s}' \
        "${seed}" "${WORK_PATH}" "${PYTHON}" "${STUB}" "${extra}"
}

echo
echo "== A 成功路径（阶梯 [2,4]，生成数据集）=="
run_prefix "success" "$(base_params 1)"
if [ "${CASE_RC}" -eq 0 ]; then ok "exit 0"; else bad "exit ${CASE_RC}（期望 0）"; tail -25 "${CASE_OUT}/stdout.log"; fi
RESULT="${CASE_OUT}/result.json"
if [ -f "${RESULT}" ]; then
    [ "$(jqa "${RESULT}" status)" = "succeeded" ] && ok "status=succeeded" || bad "status=$(jqa "${RESULT}" status)"
    has_metric "${RESULT}" ttft concurrency=4 phase=full && ok "ttft@4 有指标" || bad "缺 ttft(concurrency=4,phase=full)"
    has_metric "${RESULT}" measured_concurrency concurrency=2 && ok "measured_concurrency@2" || bad "缺 measured_concurrency@2"
    has_metric "${RESULT}" hit_rate scope=hbm phase=full && ok "hit_rate(hbm) 有值" || bad "缺 hit_rate"
    has_metric "${RESULT}" output_throughput scope=per_device && ok "单卡吞吐有值" || bad "缺 per_device 吞吐"
    [ "$(jqa "${RESULT}" runner.name)" = "aisbench-prefix" ] && ok "runner.name 正确" || bad "runner.name=$(jqa "${RESULT}" runner.name)"
    # 观察面 1：上游格式的聚合 CSV（输出根目录，一行 = 一个点的一个阶段）
    if [ -f "${CASE_OUT}/aisbench_result.csv" ]; then
        ok "聚合 CSV 在输出根目录"
        head1=$(head -n 1 "${CASE_OUT}/aisbench_result.csv")
        case "${head1}" in point,phase,*) ok "列头以 point,phase 开头（+ 上游原列序）" ;; *) bad "列头不对：${head1}" ;; esac
        rows=$(( $(wc -l < "${CASE_OUT}/aisbench_result.csv") - 1 ))
        [ "${rows}" -ge 4 ] && ok "聚合 CSV ${rows} 行（2 点 × 2 阶段）" || bad "聚合 CSV 只有 ${rows} 行"
        grep -q hit_rate_hbm "${CASE_OUT}/aisbench_result.csv" \
            && ok "命中率列在 CSV 里" || bad "缺命中率列"
        grep -q '"path": *"aisbench_result.csv"' "${RESULT}" \
            && ok "聚合 CSV 列进了 artifacts" || bad "artifacts 没列聚合 CSV"
    else
        bad "缺聚合 CSV（aisbench_result.csv）"
    fi
    # 观察面 2：result.html 对比报告（自包含，阶梯表 + 内联 SVG）
    if [ -f "${CASE_OUT}/result.html" ]; then
        ok "result.html 报告在输出根目录"
        grep -q '并发阶梯' "${CASE_OUT}/result.html" && ok "阶梯表在" || bad "缺阶梯表"
        # HTML 是单行文件，必须 grep -o 数出现次数（grep -c 数的是行数）
        n_svg=$(grep -o '<svg ' "${CASE_OUT}/result.html" | wc -l)
        [ "${n_svg}" -ge 3 ] && ok "趋势图 ${n_svg} 张" || bad "趋势图只有 ${n_svg} 张"
        grep -qE 'src="http|href="http' "${CASE_OUT}/result.html" \
            && bad "报告里引了外部资源" || ok "自包含（无外部资源引用）"
        grep -q '"path": *"result.html"' "${RESULT}" \
            && ok "result.html 列进了 artifacts" || bad "artifacts 没列 result.html"
    else
        bad "缺 result.html"
    fi
    # 观察面 3：每点的上游原始 CSV 仍在
    [ -f "${CASE_OUT}/prefix_out/points/point-4/aisbench_result.csv" ] \
        && ok "每点的上游原始 CSV 保留" || bad "缺每点 aisbench_result.csv"
    # 平台硬规则：name+statistic+labels 必须唯一，否则 Task 直接 RESULT_INVALID（kind 实测）
    dup=$("${PYTHON}" - "${RESULT}" <<'PYDUP'
import json, sys
from collections import Counter
data = json.load(open(sys.argv[1], encoding="utf-8"))
keys = Counter((m["name"], m["statistic"],
                json.dumps(m.get("labels") or {}, sort_keys=True, ensure_ascii=False))
               for m in data["metrics"])
print(sum(1 for _k, c in keys.items() if c > 1))
PYDUP
)
    [ "${dup}" = "0" ] && ok "指标键唯一（不会被判 RESULT_INVALID）" || bad "有 ${dup} 个重复指标键"
    hit_points=$("${PYTHON}" - "${RESULT}" <<'PYHITS'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
points = {m["labels"].get("point") for m in data["metrics"]
          if m["name"] == "hit_rate" and m["labels"].get("phase") == "full"}
print(len(points))
PYHITS
)
    [ "${hit_points}" -ge 2 ] && ok "两点各自都有命中率（point=${hit_points}）" || bad "命中率只覆盖 ${hit_points} 个点"
    [ "$(jqa "${RESULT}" metrics[].labels[].phase)" != "" ] && ok "metrics 带 labels"
    n_art=$(jlen "${RESULT}" artifacts)
    [ "${n_art}" -ge 4 ] && ok "artifacts ${n_art} 项" || bad "artifacts 太少（${n_art}）"
    [ -f "${CASE_OUT}/prefix_out/points/point-4/aisbench_result.csv" ] && ok "上游 CSV 落产物目录" || bad "缺 aisbench_result.csv"
    ls "${CASE_OUT}/prefix_out/datasets/c2" >/dev/null 2>&1 && ok "每点独立数据集目录" || bad "缺 datasets/c2"
else
    bad "没有 result.json"
fi

echo
echo "== A2 字段名不匹配的输入项也要落到工具（summarizer / aisbench_extra_args）=="
run_prefix "mapping" "$(base_params 1 '"summarizer":"stable_stage","aisbench_extra_args":"--max-num-workers 4","dataset_type":"normal","prefix_test":false')"
if [ "${CASE_RC}" -eq 0 ]; then ok "exit 0"; else bad "exit ${CASE_RC}"; tail -20 "${CASE_OUT}/stdout.log"; fi
grep -q 'summarizer=stable_stage' "${CASE_OUT}/stdout.log" \
    && ok "summarizer 传到了 ais_bench" || bad "summarizer 丢了（字段名→变量名映射漏了）"
grep -q -- '--max-num-workers 4' "${CASE_OUT}/stdout.log" \
    && ok "aisbench_extra_args 透传成功" || bad "aisbench_extra_args 丢了"
grep -q -- '--config-dir' "${CASE_OUT}/stdout.log" \
    && ok "run.sh 自动追加的 --config-dir 还在" || bad "--config-dir 没接上"
grep -q 'hit_rate（' "${CASE_OUT}/result.json" 2>/dev/null \
    && ok "prefix_test=false 时 notes 说明没有命中率" \
    || bad "prefix_test=false 却没在 notes 里说明 hit_rate 缺失"

echo
echo "== A3 只读文件系统安全（kind 实测踩过的 EROFS）=="
# /runner/tool 在平台 Job 里位于只读根文件系统：任何以写模式打开工具目录内文件的做法
# 都会 OSError: [Errno 30]（上游的 open('default_api.py','r+') 就是这样）。
if grep -RnoE "open\((os\.path\.join\()?[^)]*TOOL_DIR[^)]*['\"](r\+|w|a)[+]?['\"]" "${RUNNER_DIR}/tool" >/dev/null 2>&1; then
    bad "工具里有以写模式打开 TOOL_DIR 文件的地方（只读根文件系统上必然 EROFS）"
    grep -RnoE "open\((os\.path\.join\()?[^)]*TOOL_DIR[^)]*['\"](r\+|w|a)[+]?['\"]" "${RUNNER_DIR}/tool" | head -3
else
    ok "没有以写模式打开 TOOL_DIR 文件的代码"
fi
if grep -RnoE "open\(['\"][^'\"]*['\"], *['\"](r\+|w\+|a\+)['\"]" "${TOOL_DIR}/save_file.py" "${TOOL_DIR}/data_picker.py" "${TOOL_DIR}/cal_prefix_hit_rate.py" >/dev/null 2>&1; then
    bad "上游文件里有写模式 open 落在 CWD 之外？逐条核对"
else
    ok "写操作只落在 CWD（= 该点产物目录）"
fi

echo
echo "== B 可复现（同参数同 seed 两次 → 数据集逐字节相同）=="
run_prefix "repro-a" "$(base_params 5)"
run_prefix "repro-b" "$(base_params 5)"
hash_a=$(find "${WORK}/repro-a/prefix_out/datasets/c2" -name 'GSM8K-in128-*' -exec sha256sum {} \; | awk '{print $1}' | sort -u)
hash_b=$(find "${WORK}/repro-b/prefix_out/datasets/c2" -name 'GSM8K-in128-*' -exec sha256sum {} \; | awk '{print $1}' | sort -u)
if [ -n "${hash_a}" ] && [ "${hash_a}" = "${hash_b}" ]; then ok "两次数据集 sha256 一致 (${hash_a:0:12}…)"; else bad "同 seed 数据集不同：${hash_a:0:12}… vs ${hash_b:0:12}…"; fi
run_prefix "repro-c" "$(base_params 6)"
hash_c=$(find "${WORK}/repro-c/prefix_out/datasets/c2" -name 'GSM8K-in128-*' -exec sha256sum {} \; | awk '{print $1}' | sort -u)
[ -n "${hash_c}" ] && [ "${hash_c}" != "${hash_a}" ] && ok "换 seed 数据集变了" || bad "换 seed 数据集没变（seed 没生效）"

echo
echo "== C picked_ids（FAQ #1 的自动化）=="
TOTAL=$("${PYTHON}" -c "import sys;print(sum(1 for _ in open('${TOOL_DIR}/GSM8K.jsonl',encoding='utf-8')))")
STATE_DIR="$(mktemp -d "${WORK}/state-XXXX")"
fill_state() { "${PYTHON}" - "${STATE_DIR}/picked_ids.txt" "${TOTAL}" <<'PYFILL'
import sys
path, total = sys.argv[1], int(sys.argv[2])
with open(path, "w", encoding="utf-8") as handle:
    handle.write("".join("%d\n" % i for i in range(total)))
PYFILL
}
fill_state
run_prefix "pick-clean" "$(base_params 1)" AGP_STATE_DIR="${STATE_DIR}"
[ "${CASE_RC}" -eq 0 ] && ok "记录被标满 + 默认清理 → 仍然成功" || { bad "默认清理没生效（exit ${CASE_RC}）"; tail -20 "${CASE_OUT}/stdout.log"; }
fill_state
# keep_pick_state 是**建模过的输入字段**（run.sh 由它决定 AGP_RESET_PICK_STATE），
# 所以这里必须走参数文件而不是 env——env 会被 run.sh 按参数覆盖。
run_prefix "pick-keep" "$(base_params 1 '"keep_pick_state":true')" AGP_STATE_DIR="${STATE_DIR}"
if [ "${CASE_RC}" -eq 1 ]; then ok "保留记录且题库被标满 → 失败且退出码 1（不是 0）"; else bad "退出码 ${CASE_RC}（期望 1：上游在这里是 exit 0 的坑）"; fi
grep -q "生成数据集失败" "${CASE_OUT}/stdout.log" && ok "日志写清原因" || bad "日志里没有「生成数据集失败」"
[ "$(jqa "${CASE_OUT}/result.json" error.code)" = "RUNNER_NONZERO_EXIT" ] && ok "error.code=RUNNER_NONZERO_EXIT" || bad "error.code=$(jqa "${CASE_OUT}/result.json" error.code 2>/dev/null)"
rm -rf "${STATE_DIR}"

echo
echo "== D ais_bench 失败不被 tee 吞掉（pipefail）=="
run_prefix "fail-single" "$(base_params 1)" STUB_FAIL_AT=2,4
if [ "${CASE_RC}" -eq 1 ]; then ok "全点失败 → exit 1"; else bad "exit ${CASE_RC}（期望 1；=0 说明 ais_bench 的退出码又被 tee 吞了）"; fi
[ "$(jqa "${CASE_OUT}/result.json" error.code)" = "RUNNER_NONZERO_EXIT" ] && ok "error.code 正确" || bad "error.code=$(jqa "${CASE_OUT}/result.json" error.code 2>/dev/null)"
run_prefix "fail-partial" "$(base_params 1)" STUB_FAIL_AT=4
if [ "${CASE_RC}" -eq 0 ]; then ok "c=2 成功 c=4 失败 → 整体仍 exit 0"; else bad "exit ${CASE_RC}（期望 0）"; fi
grep -q "失败的并发点" "${CASE_OUT}/result.json" && ok "notes 记下落单的失败点" || bad "notes 没提失败点"

echo
echo "== E 单点超时 =="
run_prefix "timeout" "$(base_params 1 '"run_timeout":1,"data_num":4,"concurrency":[2]')" STUB_SLEEP=1
if [ "${CASE_RC}" -eq 1 ]; then ok "超时 → exit 1"; else bad "exit ${CASE_RC}"; fi
[ "$(jqa "${CASE_OUT}/result.json" error.code)" = "RUNNER_TIMEOUT" ] && ok "error.code=RUNNER_TIMEOUT" || bad "error.code=$(jqa "${CASE_OUT}/result.json" error.code 2>/dev/null)"

echo
echo "== F 参数错误 =="
run_prefix "bad-params" '{"input_len":128}'
[ "${CASE_RC}" -eq 2 ] && ok "缺 concurrency → exit 2" || bad "exit ${CASE_RC}（期望 2）"
[ "$(jqa "${CASE_OUT}/result.json" error.code)" = "VALIDATION_ERROR" ] && ok "error.code=VALIDATION_ERROR" || bad "error.code=$(jqa "${CASE_OUT}/result.json" error.code 2>/dev/null)"
run_prefix "unknown-field" '{"concurrency":[2],"nope":1}'
[ "${CASE_RC}" -eq 2 ] && ok "未声明字段 → exit 2" || bad "exit ${CASE_RC}（期望 2）"
run_prefix "bad-seed" '{"concurrency":[2],"seed":-1}'
[ "${CASE_RC}" -eq 2 ] && ok "seed 非法 → exit 2" || bad "exit ${CASE_RC}（期望 2）"
# 公共参数缺失也要**提前**报，而不是让 ais_bench 跑完压测才在汇总阶段炸（kind 实测）
run_prefix "no-model-path" "$(base_params 1)" AG_MODEL_PATH= AGP_MODEL_PATH=
[ "${CASE_RC}" -eq 2 ] && ok "缺 AG_MODEL_PATH → exit 2（提前拦，不烧压测时间）" || bad "exit ${CASE_RC}（期望 2）"
grep -qi 'tokenizer' "${CASE_OUT}/result.json" && ok "消息写明要 tokenizer 目录" || bad "消息没解释清楚"
run_prefix "ghost-model-path" "$(base_params 1)" AG_MODEL_PATH=/models/does-not-exist
[ "${CASE_RC}" -eq 2 ] && ok "model_path 不存在 → exit 2" || bad "exit ${CASE_RC}（期望 2）"

echo
echo "== G 凭据零泄漏 =="
if grep -rq "${SECRET}" "${WORK}" 2>/dev/null; then
    bad "产物里出现明文 key：$(grep -rl "${SECRET}" "${WORK}" | head -n3)"
else
    ok "全部产物目录 grep 不到 ${SECRET}（key 只在环境变量里）"
fi
grep -q 'api_key=_API_KEY' "${WORK}/repro-a/prefix_out/points/point-4/temp_api.py" 2>/dev/null \
    && ok "模型配置写的是读 env 的表达式" || bad "temp_api.py 里没有 api_key=_API_KEY"
grep -q -- "--api-key ${SECRET}" "${WORK}"/*/stdout.log 2>/dev/null \
    && bad "argv 里出现明文 key" || ok "argv 里没有明文 key"

echo
echo "== H result.json 结构（按 openapi Result 校验）=="
if [ -f "${HERE}/validate_result_schema.py" ]; then
    "${PYTHON}" "${HERE}/validate_result_schema.py" \
        "${WORK}/success/result.json" "${WORK}/fail-single/result.json" \
        "${WORK}/timeout/result.json" "${WORK}/bad-params/result.json" \
        "${WORK}/unknown-field/result.json" "${WORK}/bad-seed/result.json" \
        2>&1 | tail -n 8
    [ "${PIPESTATUS[0]}" -eq 0 ] && ok "schema 校验通过" || bad "schema 校验失败"
else
    echo "  （跳过：没有 validate_result_schema.py）"
fi

echo
echo "======================================================================"
echo "冒烟结果：PASS=${PASS} FAIL=${FAIL}（产物在 ${WORK}）"
[ "${FAIL}" -eq 0 ] || exit 1
exit 0

#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Argus
# SPDX-License-Identifier: Apache-2.0
"""agentx Runner 的离线回归：参数校验 / 命令映射 / 语料准备 / 结果归一化 / 退出码 / 密钥不落盘。

不需要镜像、不需要服务：``agentx_cmd`` 指到 ``tests/stub_agentx.py``，跑的是真实
``agentx_runner.py`` 的完整路径（解析参数 → 起子进程 → 读产物 → 写 result.json）。

跑法：
    py -3.11 runners/agentx/tests/test_result_mapping.py
    # 或容器内：python3 /runner/tests/test_result_mapping.py
退出码：0 全部通过；1 有失败。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER_DIR = HERE.parent
FIXTURE = HERE / "fixtures" / "profile_export_aiperf.json"
STUB = HERE / "stub_agentx.py"


def find_repo(start: Path) -> Path | None:
    """找到仓库根（含 server/api/openapi.yaml）。容器里只有 /runner 时返回 None。"""
    for candidate in (start, *start.parents):
        if (candidate / "server" / "api" / "openapi.yaml").is_file():
            return candidate
    return None


REPO = find_repo(RUNNER_DIR)


def scratch_root() -> Path:
    if REPO is not None:
        return REPO / ".data" / "agentx-runner" / "tests"
    return Path(os.environ.get("TMPDIR") or "/tmp") / "agentx-runner-tests"


def scratch_dir(prefix: str) -> Path:
    """优先用系统临时目录；受限环境（沙箱只允许写仓库）回落到 .data/（已 gitignore）。"""
    try:
        candidate = Path(tempfile.mkdtemp(prefix=prefix))
        probe = candidate / ".probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return candidate
    except OSError:
        fallback = scratch_root()
        shutil.rmtree(fallback, ignore_errors=True)
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


OUT_DIR = scratch_dir("agentx-runner-out-")
os.environ["AG_OUTPUT_DIR"] = str(OUT_DIR)
os.environ.pop("ENDPOINT_API_KEY", None)
os.environ.pop("STUB_EXIT", None)

spec = importlib.util.spec_from_file_location("agentx_runner", RUNNER_DIR / "agentx_runner.py")
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
sys.modules["agentx_runner"] = runner
spec.loader.exec_module(runner)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name}" + (f"：{detail}" if detail else ""))


def expect_param_error(name: str, fn) -> None:
    try:
        fn()
    except runner.ParamError as exc:
        check(name, True, str(exc))
    except Exception as exc:  # noqa: BLE001
        check(name, False, f"抛出了 {type(exc).__name__} 而不是 ParamError：{exc}")
    else:
        check(name, False, "没有抛 ParamError")


def params(**overrides) -> dict:
    base = dict(runner.DEFAULTS)
    base.update(overrides)
    return base


def read_result() -> dict:
    return json.loads((OUT_DIR / "result.json").read_text(encoding="utf-8"))


def _with_env(name: str, value: str, fn):
    """临时设一个环境变量再调用 fn()：公共参数（AG_MODEL_PATH 等）只从环境里读。"""
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        return fn()
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


_scratch_seq = 0


def scratch_subdir(prefix: str) -> Path:
    """在 OUT_DIR 下建一个普通子目录。

    不用 ``tempfile.mkdtemp(dir=...)``：Windows 上它用 0o700 建目录，受限令牌（CI 容器 /
    沙箱）跑测试时该目录随后不可写，后续用例会莫名其妙地 PermissionError。
    """
    global _scratch_seq
    _scratch_seq += 1
    path = OUT_DIR / f"{prefix}{_scratch_seq}"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- 1. 参数语义 -------------------------------------------------------------

check("默认参数可用", runner.validate(params())["concurrency"] == 8)
expect_param_error(
    "短跑未授权 → 参数错误",
    lambda: runner.validate(params(benchmark_duration_seconds=60)),
)
check(
    "短跑 + allow_short_run 放行",
    runner.validate(params(benchmark_duration_seconds=60, allow_short_run=True))["allow_short_run"],
)
expect_param_error("concurrency=0 被拒", lambda: runner.validate(params(concurrency=0)))
expect_param_error("未知 scenario 被拒", lambda: runner.validate(params(scenario="oops")))
expect_param_error(
    "两种预热互斥",
    lambda: runner.validate(params(warmup_requests_per_lane=3, agentic_cache_warmup_duration=60)),
)
expect_param_error("空语料名被拒", lambda: runner.validate(params(public_dataset=" ")))

# 分词器不是业务参数，而是公共参数 AG_MODEL_PATH（§5.4.1）：写进参数文件会被当未声明字段拒绝。
check(
    "tokenizer 不是业务参数（不进 parameters）",
    "tokenizer" not in runner.DEFAULTS,
)
_tokenizer_file = OUT_DIR / "with-tokenizer.jsonl"
_tokenizer_file.write_text('{"tokenizer": "/models/gpt2"}\n', encoding="utf-8")
expect_param_error(
    "参数文件里写 tokenizer 被拒（公共参数不进输入文件）",
    lambda: runner.load_params(str(_tokenizer_file)),
)

_saved_model_path = os.environ.pop("AG_MODEL_PATH", None)
try:
    check("未注入 AG_MODEL_PATH → 空分词器（交给 agentx 推导）", runner.resolve_tokenizer() == "")
    check("AG_MODEL_PATH=builtin 原样透传", _with_env("AG_MODEL_PATH", "builtin", runner.resolve_tokenizer) == "builtin")
    check(
        "AG_MODEL_PATH=HF 仓库名 原样透传",
        _with_env("AG_MODEL_PATH", "Qwen/Qwen3-32B", runner.resolve_tokenizer) == "Qwen/Qwen3-32B",
    )
    tokenizer_dir = scratch_subdir("tok-")
    check(
        "AG_MODEL_PATH 指向的本地目录被接受",
        _with_env("AG_MODEL_PATH", str(tokenizer_dir), runner.resolve_tokenizer) == str(tokenizer_dir),
    )
    expect_param_error(
        "AG_MODEL_PATH 指向不存在的绝对路径被拒",
        lambda: _with_env(
            "AG_MODEL_PATH",
            "/models/does-not-exist",
            runner.resolve_tokenizer,
        ),
    )
finally:
    if _saved_model_path is None:
        os.environ.pop("AG_MODEL_PATH", None)
    else:
        os.environ["AG_MODEL_PATH"] = _saved_model_path

expect_param_error("dataset_dir 不存在被拒", lambda: runner.validate(params(dataset_dir="/nope/missing")))
file_like = scratch_subdir("f-") / "traces.jsonl"
file_like.write_text("{}\n", encoding="utf-8")
expect_param_error(
    "dataset_dir 给文件被拒（要父目录）",
    lambda: runner.validate(params(dataset_dir=str(file_like))),
)
empty_dir = scratch_subdir("e-")
expect_param_error(
    "dataset_dir 既无 traces.jsonl 也无 *.json 被拒",
    lambda: runner.validate(params(dataset_dir=str(empty_dir))),
)

# child_env 返回 (env, hf_dir, mmap_dir)：缓存目录取值优先级 参数 > 环境变量 > 缺省。
env_local, hf_dir_local, mmap_dir_local = runner.child_env(params())
check("child_env 设置 HF_HOME", env_local.get("HF_HOME") == str(hf_dir_local))
check("child_env 设置 mmap 缓存", env_local.get("AIPERF_DATASET_MMAP_CACHE_DIR") == str(mmap_dir_local))
check("缓存缺省落在 <output>/.cache 下", hf_dir_local == OUT_DIR / ".cache" / "hf"
      and mmap_dir_local == OUT_DIR / ".cache" / "mmap")
check("默认不设 HF_HUB_OFFLINE", env_local.get("HF_HUB_OFFLINE") is None)

env_offline, _, _ = runner.child_env(params(offline=True))
check("offline=true 设 HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE",
      env_offline.get("HF_HUB_OFFLINE") == "1" and env_offline.get("TRANSFORMERS_OFFLINE") == "1")

custom_hf = OUT_DIR / "custom-hf"
custom_mmap = OUT_DIR / "custom-mmap"
env_custom, hf_custom, mmap_custom = runner.child_env(
    params(hf_cache_dir=str(custom_hf), mmap_cache_dir=str(custom_mmap)))
check("hf_cache_dir/mmap_cache_dir 参数生效",
      hf_custom == custom_hf and mmap_custom == custom_mmap
      and env_custom["HF_HOME"] == str(custom_hf)
      and env_custom["AIPERF_DATASET_MMAP_CACHE_DIR"] == str(custom_mmap))

# --- 2. 输入文件解析（§5.3 / §5.4） -----------------------------------------

tmp_params = scratch_dir("agentx-runner-params-")
good = tmp_params / "good.jsonl"
good.write_text(
    '{"concurrency": 4}\n\n{"benchmark_duration_seconds": 1200, "allow_short_run": true}\n',
    encoding="utf-8",
)
merged = runner.load_params(str(good))
check("多行 JSONL 后者覆盖前者", merged["concurrency"] == 4 and merged["benchmark_duration_seconds"] == 1200)

bad_field = tmp_params / "bad-field.jsonl"
bad_field.write_text('{"nope": 1}\n', encoding="utf-8")
expect_param_error("未声明字段被拒", lambda: runner.load_params(str(bad_field)))

bad_json = tmp_params / "bad-json.jsonl"
bad_json.write_text("{not json}\n", encoding="utf-8")
expect_param_error("非法 JSON 被拒", lambda: runner.load_params(str(bad_json)))

not_object = tmp_params / "array.jsonl"
not_object.write_text("[1, 2]\n", encoding="utf-8")
expect_param_error("非对象行被拒", lambda: runner.load_params(str(not_object)))

check("空输入 = 全默认", runner.load_params(None) == runner.DEFAULTS)

# --- 3. 命令映射（§5.2：平台不解析，规则只在这里） --------------------------

cmd = runner.build_command(params(), "http://svc:8000/v1", "Qwen3-32B")
check("命令含 profile 与端点类型", cmd[0] == "agentx" and "--endpoint-type" in cmd and "chat" in cmd)
check("命令固定 --ui none", "--ui" in cmd and cmd[cmd.index("--ui") + 1] == "none")
check("命令带语料与并发", "semianalysis_cc_traces_weka_062126" in cmd and "8" in cmd)
check("输出目录指向 agentx/", str(runner.ARTIFACTS) in cmd)
check("默认不加 --unsafe-override", "--unsafe-override" not in cmd)

short_cmd = runner.build_command(params(benchmark_duration_seconds=60, allow_short_run=True), "u", "m")
check("短跑自动加 --unsafe-override", "--unsafe-override" in short_cmd)

os.environ["ENDPOINT_API_KEY"] = "supersecret-value"
try:
    key_cmd = runner.build_command(params(), "u", "m")
    check("API key 以 ${} 引用进 argv", "${ENDPOINT_API_KEY}" in key_cmd)
    check("API key 明文不进 argv", "supersecret-value" not in " ".join(key_cmd))
finally:
    os.environ.pop("ENDPOINT_API_KEY", None)

extra = runner.build_command(params(extra_args="--use-server-token-count --tokenizer-trust-remote-code"), "u", "m")
check("extra_args 按 shell 拆分追加", "--use-server-token-count" in extra and extra[-1] == "--tokenizer-trust-remote-code")

# 本地语料（离线）：--input-file + 强制 override，且不再带 --public-dataset
local_dir = tmp_params / "weka"
local_dir.mkdir(exist_ok=True)
(local_dir / "trace-a.json").write_text('{"id":"a"}', encoding="utf-8")
local_cmd = runner.build_command(params(dataset_dir=str(local_dir)), "u", "m", local_dir)
check("本地语料走 --input-file", "--input-file" in local_cmd and str(local_dir) in local_cmd)
check("本地语料指定 weka_trace", local_cmd[local_cmd.index("--custom-dataset-type") + 1] == "weka_trace")
check("本地语料强制 --unsafe-override", "--unsafe-override" in local_cmd)
check("本地语料不带 --public-dataset", "--public-dataset" not in local_cmd)

# --- 4. 本地语料准备（dataset_dir → .agentx-split） -------------------------

corpus = tmp_params / "corpus"
corpus.mkdir()
jsonl = corpus / "traces.jsonl"
jsonl.write_text('{"id": "t1", "messages": []}\n{"id": "t2", "messages": []}\n', encoding="utf-8")
split = runner.prepare_local_dataset(corpus)
check("拆到 <dataset_dir>/.agentx-split", split == corpus / ".agentx-split")
check("traces.jsonl 被拆成两条", sorted(p.name for p in split.glob("*.json")) == ["t1.json", "t2.json"])
check("拆分目录里只有 trace 的 *.json", all(p.name.endswith(".json") for p in split.glob("*.json")))

tampered = split / "t1.json"
tampered.write_text('{"tampered": true}', encoding="utf-8")
again = runner.prepare_local_dataset(corpus)
check("第二次命中缓存不重写", again == split and "tampered" in tampered.read_text(encoding="utf-8"))

jsonl.write_text('{"id": "t1", "messages": []}\n{"id": "t2", "messages": []}\n{"id": "t3", "messages": []}\n',
                 encoding="utf-8")
third = runner.prepare_local_dataset(corpus)
check("源文件变化后重拆",
      sorted(p.name for p in third.glob("*.json")) == ["t1.json", "t2.json", "t3.json"]
      and "tampered" not in tampered.read_text(encoding="utf-8"))

ready = tmp_params / "ready"
ready.mkdir()
(ready / "x.json").write_text('{"id":"x"}', encoding="utf-8")
check("已拆好的目录原样使用", runner.prepare_local_dataset(ready) == ready)

# --- 5. 结果归一化（§5.9 / 附录 A） -----------------------------------------

export = json.loads(FIXTURE.read_text(encoding="utf-8"))
metrics = runner.build_metrics(export, params(concurrency=8))

by_key = {(m["name"], m["statistic"]): m for m in metrics}
check("ttft 的五个统计量都在", all(("ttft", s) in by_key for s in ("mean", "p50", "p90", "p99", "max")))
check("ttft.mean 值正确", by_key[("ttft", "mean")]["value"] == 21.41)
check("ttft 单位是 ms", by_key[("ttft", "mean")]["unit"] == "ms")
check("ttft 带样本量", by_key[("ttft", "mean")]["sample_size"] == 29)
check("吞吐单位是 tokens_per_s", by_key[("output_token_throughput", "mean")]["unit"] == "tokens_per_s")
check("请求吞吐单位是 req_per_s", by_key[("request_throughput", "mean")]["unit"] == "req_per_s")
check("前缀命中率进 percent", by_key[("prefix_cache_hit", "mean")]["value"] == 90.19)
check("request_count 用 count 统计量", by_key[("request_count", "count")]["value"] == 29.0)
check("指标都带 labels.concurrency", all(m["labels"]["concurrency"] == 8 for m in metrics))

invalid_stat = [m for m in metrics if m["statistic"] not in
                ("mean", "p50", "p90", "p95", "p99", "min", "max", "sum", "count")]
check("statistic 在平台枚举内", not invalid_stat, str(invalid_stat))
dupes = {(m["name"], m["statistic"], json.dumps(m["labels"], sort_keys=True)) for m in metrics}
check("name+statistic+labels 唯一", len(dupes) == len(metrics))

notes = runner.build_notes(export, params(benchmark_duration_seconds=60, num_dataset_entries=8), [])
check("notes 带提交有效性", "提交有效性：false" in notes)
check("notes 带短跑提示", "短跑" in notes)
check("notes 带截断语料提示", "num_dataset_entries" in notes)
check("notes 长度受限", len(runner.build_notes(export, params(), ["x" * 5000])) <= 4000)

# --- 6. 失败结果也要可读（§5.9） --------------------------------------------

runner.emit_failure("VALIDATION_ERROR", "坏参数", runner.utc_now())
failed = read_result()
check("失败结果 status=failed", failed["status"] == "failed")
check("失败结果带错误码", failed["error"]["code"] == "VALIDATION_ERROR")
check("失败结果含必填字段", all(k in failed for k in
      ("schema_version", "task_id", "attempt_no", "runner_seq", "runner", "status", "metrics")))

# --- 7. 端到端（stub 子进程） ----------------------------------------------

stub_cmd = f'"{sys.executable}" "{STUB}"'
e2e_params = tmp_params / "e2e.jsonl"
e2e_params.write_text(json.dumps({"concurrency": 4, "num_dataset_entries": 8,
                                  "agentx_cmd": stub_cmd}), encoding="utf-8")

os.environ["ENDPOINT_API_KEY"] = "supersecret-value"
code = runner.run(["--params-file", str(e2e_params), "--url", "http://127.0.0.1:8000/v1",
                   "--model_name", "mock-llm"])
os.environ.pop("ENDPOINT_API_KEY", None)
e2e = read_result()
check("stub 跑通退出码 0", code == 0, str(code))
check("stub 结果 status=succeeded", e2e["status"] == "succeeded")
check("stub 结果有指标", len(e2e["metrics"]) >= 15, str(len(e2e["metrics"])))
check("stub 结果有产物", any(a["path"].endswith("profile_export_aiperf.json") for a in e2e["artifacts"]))
check("产物路径相对输出目录", all(not a["path"].startswith("/") for a in e2e["artifacts"]))
check("runner 元信息正确",
      e2e["runner"]["name"] == "agentx" and e2e["runner"]["version"] == runner.RUNNER_VERSION)
check("workload 带并发与模型", e2e["workload"]["concurrency"] == 4
      and e2e["workload"]["model_name"] == "mock-llm")
check("临时缓存已清理（产物配额）", not (OUT_DIR / ".cache").exists())
check("结果里没有密钥明文", "supersecret-value" not in json.dumps(e2e, ensure_ascii=False))
check("日志里没有密钥明文",
      "supersecret-value" not in (OUT_DIR / "logs" / "agentx.log").read_text(encoding="utf-8", errors="replace")
      if (OUT_DIR / "logs" / "agentx.log").is_file() else True)

os.environ["STUB_EXIT"] = "1"
try:
    code = runner.run(["--params-file", str(e2e_params), "--url", "http://127.0.0.1:8000/v1",
                       "--model_name", "mock-llm"])
finally:
    os.environ.pop("STUB_EXIT", None)
failed = read_result()
check("agentx 非零退出 → 退出码 1", code == 1, str(code))
check("agentx 非零退出 → RUNNER_NONZERO_EXIT", failed["error"]["code"] == "RUNNER_NONZERO_EXIT")

bad = tmp_params / "short.jsonl"
bad.write_text(json.dumps({"benchmark_duration_seconds": 30, "agentx_cmd": stub_cmd}), encoding="utf-8")
code = runner.run(["--params-file", str(bad), "--url", "http://127.0.0.1:8000/v1",
                   "--model_name", "mock-llm"])
check("参数错误 → 退出码 2", code == 2, str(code))
check("参数错误 → VALIDATION_ERROR", read_result()["error"]["code"] == "VALIDATION_ERROR")

missing = tmp_params / "no-url.jsonl"
missing.write_text(json.dumps({"agentx_cmd": stub_cmd}), encoding="utf-8")
code = runner.run(["--params-file", str(missing), "--model_name", "mock-llm"])
check("缺 AG_SERVED_URL → 退出码 2", code == 2, str(code))

print()
if FAILURES:
    print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
    sys.exit(1)
print("ALL PASS")

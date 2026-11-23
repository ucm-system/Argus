#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Argus
# SPDX-License-Identifier: Apache-2.0
"""Argus Runner「agentx」的运行期实现。

三步：解析并校验业务参数 → 调用 ``agentx profile`` → 把 agentx 的
``profile_export_aiperf.json`` 归一化成平台契约的 ``result.json``（附录 A）。

设计约束来源：
  * 契约面（入口 / 输入三通道 / 输出目录 / 退出码）由 ``run.sh`` 处理，本文件只吃它的 argv。
  * 参数语义（范围 / 枚举 / 互斥）归 Runner（ARCHITECTURE §5.4）。
  * 结果归一化义务见 §5.9：``name`` 只放指标，``statistic`` / ``unit`` 走字段，维度进 ``labels``。
  * 错误码只能用附录 D 的枚举（``server/internal/domain/codes.go``），自造码会被平台静默降级。
  * 凭据只走环境变量（§12.4）：API key 以 ``${ENDPOINT_API_KEY}`` 引用的形式进 argv，
    明文不出现、不落日志、不进 result.json。
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUNNER_NAME = "agentx"
RUNNER_VERSION = "1.0.14"

OUT = Path(os.environ.get("AG_OUTPUT_DIR") or "/workspace/output")
LOGS = OUT / "logs"
RAW = OUT / "raw"
ARTIFACTS = OUT / "agentx"
RESULT = OUT / "result.json"
RUNNER_LOG = LOGS / "runner.log"
AGENTX_LOG = LOGS / "agentx.log"

# 业务参数缺省值（与 runner.yaml 的 parameters[].default 保持一致；平台只校类型与 required，
# 缺省值语义由本文件兜底，独立运行时也一样生效）。
DEFAULTS: dict[str, Any] = {
    "concurrency": 8,
    "benchmark_duration_seconds": 1800,
    "public_dataset": "semianalysis_cc_traces_weka_062126_256k",
    "dataset_dir": "cc_traces_weka_062126-256k",
    "num_dataset_entries": 0,
    "max_context_length": 0,
    "random_seed": 42,
    "scenario": "agentx",
    "warmup_requests_per_lane": 0,
    "agentic_cache_warmup_duration": 0,
    "allow_short_run": False,
    "offline": True,
    "hf_cache_dir": "",
    "mmap_cache_dir": "",
    "keep_cache": False,
    "agentx_cmd": "agentx",
    "extra_args": "",
}

INT_KEYS = (
    "concurrency",
    "benchmark_duration_seconds",
    "num_dataset_entries",
    "max_context_length",
    "random_seed",
    "warmup_requests_per_lane",
    "agentic_cache_warmup_duration",
)
STR_KEYS = (
    "public_dataset",
    "dataset_dir",
    "scenario",
    "hf_cache_dir",
    "mmap_cache_dir",
    "agentx_cmd",
    "extra_args",
)
BOOL_KEYS = ("allow_short_run", "keep_cache", "offline")


SCENARIOS = ("agentx", "inferencex-agentx-mvp")
# scenario 的硬下限：稳态需要时间，短跑只是噪声（AgentX 场景规则，不是平台约束）。
MIN_DURATION_SECONDS = 900


class ParamError(Exception):
    """业务参数不合法：写 result.json(status=failed/VALIDATION_ERROR) 后退出码 2。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    """本 Runner 自己的日志：同时进 stderr（平台采集）与 logs/runner.log。"""
    line = f"[{utc_now()}] {message}"
    print(line, file=sys.stderr, flush=True)
    try:
        with RUNNER_LOG.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def as_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ParamError(f"{key} 必须是整数（收到 {type(value).__name__}）")
    return value


def as_bool(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise ParamError(f"{key} 必须是布尔值 true/false（收到 {type(value).__name__}）")
    return value


def as_str(value: Any, key: str) -> str:
    if not isinstance(value, str):
        raise ParamError(f"{key} 必须是字符串（收到 {type(value).__name__}）")
    return value.strip()


def load_params(path: str | None) -> dict[str, Any]:
    """按 §5.3 逐行解析 JSONL：每行必须是对象；未声明的字段从严拒绝。"""
    params = dict(DEFAULTS)
    if not path:
        return params
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ParamError(f"输入文件不可读：{path}（{exc}）") from exc

    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ParamError(f"输入第 {lineno} 行不是合法 JSON：{exc.msg}") from exc
        if not isinstance(row, dict):
            raise ParamError(f"输入第 {lineno} 行不是 JSON 对象：{line}")
        unknown = sorted(set(row) - set(DEFAULTS))
        if unknown:
            raise ParamError(
                f"输入第 {lineno} 行含未声明字段：{', '.join(unknown)}"
                f"（本 Runner 只接受 {', '.join(sorted(DEFAULTS))}）"
            )
        params.update(row)
    return params


def validate(params: dict[str, Any]) -> dict[str, Any]:
    """范围 / 枚举 / 互斥校验；平台只做了类型与 required（§5.4）。"""
    clean: dict[str, Any] = {}
    for key in INT_KEYS:
        clean[key] = as_int(params[key], key)
    for key in BOOL_KEYS:
        clean[key] = as_bool(params[key], key)
    for key in STR_KEYS:
        clean[key] = as_str(params[key], key)

    if clean["concurrency"] < 1:
        raise ParamError("concurrency 必须 >= 1")
    if clean["benchmark_duration_seconds"] < 1:
        raise ParamError("benchmark_duration_seconds 必须 >= 1")
    if clean["num_dataset_entries"] < 0:
        raise ParamError("num_dataset_entries 必须 >= 0（0 = 全量语料）")
    if clean["max_context_length"] < 0:
        raise ParamError("max_context_length 必须 >= 0（0 = 不过滤）")
    if clean["random_seed"] < 0:
        raise ParamError("random_seed 必须 >= 0")
    if clean["warmup_requests_per_lane"] < 0:
        raise ParamError("warmup_requests_per_lane 必须 >= 0")
    if clean["agentic_cache_warmup_duration"] < 0:
        raise ParamError("agentic_cache_warmup_duration 必须 >= 0")
    if clean["warmup_requests_per_lane"] and clean["agentic_cache_warmup_duration"]:
        raise ParamError(
            "warmup_requests_per_lane 与 agentic_cache_warmup_duration 互斥："
            "选请求制预热或时长制预热其中一个"
        )
    if clean["scenario"] not in SCENARIOS:
        raise ParamError(f"scenario 只能是 {' / '.join(SCENARIOS)}（收到 {clean['scenario']}）")
    if clean["dataset_dir"]:
        path = resolve_dataset_dir(clean["dataset_dir"])
        if not path.exists():
            raise ParamError(
                f"dataset_dir 对应的目录不存在：{path}"
                f"（dataset_dir={clean['dataset_dir']}；数据集名按 {DATASET_ROOT}/<名字> 解析，挂载了吗？）"
            )
        if not path.is_dir():
            raise ParamError(
                f"dataset_dir 要是目录，不是文件：{path}（该目录下应有 {DATASET_JSONL_NAME}）"
            )
        if not (path / DATASET_JSONL_NAME).is_file() and not any(path.glob("*.json")):
            raise ParamError(
                f"dataset_dir 里既没有 {DATASET_JSONL_NAME}，也没有已拆好的 *.json：{path}"
            )
        # 拆分要写 <目录>/.agentx-split/：不可写时提前给出明确原因（平台已授予读写权限）
        if not os.access(path, os.W_OK):
            raise ParamError(f"dataset_dir 目录不可写（需要在其中生成 .agentx-split/）：{path}")
    elif not clean["public_dataset"]:
        raise ParamError("public_dataset 不能为空（语料身份是可比性的前提）")

    if clean["benchmark_duration_seconds"] < MIN_DURATION_SECONDS and not clean["allow_short_run"]:
        raise ParamError(
            f"benchmark_duration_seconds={clean['benchmark_duration_seconds']} 低于 scenario 下限 "
            f"{MIN_DURATION_SECONDS} 秒；短跑只用于冒烟，需显式 allow_short_run=true"
            "（结果会标 submission_valid=false）"
        )
    if not clean["agentx_cmd"]:
        raise ParamError("agentx_cmd 不能为空")
    return clean


# 平台把语料根目录挂在容器内的 /data；dataset_dir 只写数据集名，例如 "cc-traces-weka-062126-256k"。
DATASET_ROOT = Path("/data")
# 语料文件名；拆好的结果放在 dataset_dir 下的隐藏子目录里（不污染语料目录，也不会被
# weka_trace loader 的顶层 `*.json` glob 误读）。
DATASET_JSONL_NAME = "traces.jsonl"
SPLIT_SUBDIR = ".agentx-split"
# 拆行缓存的 manifest 与格式版本：源文件（size+mtime）或拆分逻辑变了就重拆。
SPLIT_CACHE_MANIFEST = "manifest"
SPLIT_CACHE_VERSION = 1


def _split_cache_key(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "version": SPLIT_CACHE_VERSION,
        "source": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def resolve_dataset_dir(value: str) -> Path:
    """dataset_dir 的取值：数据集名（相对 /data）或绝对路径。

    平台把语料根挂到 /data，用户只写名字（如 cc-traces-weka-062126-256k），
    对应 /data/cc-traces-weka-062126-256k/traces.jsonl；以 / 开头的仍按绝对路径处理
    （本地调试方便）。
    """
    if value.startswith("/"):
        return Path(value)
    return DATASET_ROOT / value


def prepare_local_dataset(dataset_dir: Path) -> Path:
    """把 `dataset_dir` 准备成 `--input-file` 需要的形态（目录 + 一条 trace 一个 .json）。

    识别顺序：

    1. `dataset_dir` 顶层已经有 `*.json`（没给 traces.jsonl）→ 认为语料已经拆好，原样使用；
    2. 有 `traces.jsonl` → 拆到 `dataset_dir/.agentx-split/`，并按 `manifest` 复用：
       源文件（绝对路径 + size + mtime）与拆分版本都没变就直接跳过拆分；
    3. 两者都没有 → 由 validate 提前拒掉。

    所以拆分只付一次：语料目录挂成可写，第二次运行就是"复用"，不再读写 1.85GB。
    """
    source = dataset_dir / DATASET_JSONL_NAME
    if not source.is_file():
        return dataset_dir  # 已拆好的目录：直接用

    target = dataset_dir / SPLIT_SUBDIR
    manifest_path = target / SPLIT_CACHE_MANIFEST
    key = _split_cache_key(source)

    if manifest_path.is_file():
        try:
            cached = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None
        if isinstance(cached, dict) and {k: cached.get(k) for k in key} == key:
            count = cached.get("count")
            if isinstance(count, int) and len(list(target.glob("*.json"))) == count:
                log(f"复用已拆好的语料：{target}（{count} 条 trace，跳过拆分）")
                return target
        log(f"拆分缓存已失效（源文件或拆分版本变了），重新拆分：{target}")

    # 先拆到目标下的临时子目录，再逐个 move 进来（manifest 最后写，半途失败不会被当成可用缓存）
    staging = target / ".staging"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        staging.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ParamError(
            f"无法在 dataset_dir 下创建拆分目录：{target}（{exc}）；"
            "把语料目录挂成可写，或预先在该目录下放好拆好的 *.json"
        ) from exc

    log(f"拆分本地语料 {source} -> {target}（每行一条 trace，一个 .json）")
    written = 0
    with source.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ParamError(f"语料第 {lineno} 行不是合法 JSON：{exc.msg}") from exc
            if not isinstance(row, dict):
                raise ParamError(f"语料第 {lineno} 行不是 JSON 对象")
            trace_id = str(row.get("id") or row.get("trace_id") or f"trace-{lineno}")
            out = staging / f"{trace_id}.json"
            if out.exists():
                raise ParamError(f"语料里有重复的 trace id：{trace_id}（第 {lineno} 行）")
            out.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
            written += 1

    # 清掉上一次可能残留的 *.json，再发布本次结果（manifest 是提交标记）
    for stale in target.glob("*.json"):
        stale.unlink(missing_ok=True)
    manifest_path.unlink(missing_ok=True)
    for staged in staging.glob("*.json"):
        staged.replace(target / staged.name)
    manifest_path.write_text(json.dumps({**key, "count": written}), encoding="utf-8")
    shutil.rmtree(staging, ignore_errors=True)
    log(f"拆分完成：{written} 条 trace")
    return target


def parse_argv(argv: list[str]) -> tuple[str | None, str, str]:
    """收拾 run.sh 传来的参数：--params-file 已由 run.sh 归一，这里只认独立运行的公共参数。"""
    params_file: str | None = None
    url = os.environ.get("AG_SERVED_URL", "").strip()
    model = os.environ.get("AG_SERVED_MODEL_NAME", "").strip()
    unknown: list[str] = []

    index = 0
    while index < len(argv):
        arg = argv[index]
        index += 1
        if arg == "--params-file":
            if index >= len(argv):
                raise ParamError("--params-file 缺少路径")
            params_file = argv[index]
            index += 1
        elif arg.startswith("--params-file="):
            params_file = arg.split("=", 1)[1]
        elif arg in ("--url", "--model_name", "--model_path"):
            if index >= len(argv):
                raise ParamError(f"{arg} 缺少值")
            value = argv[index]
            index += 1
            if arg == "--url":
                url = value.strip()
            elif arg == "--model_name":
                model = value.strip()
            # --model_path 是公共参数保留字（§5.4.1）：接受并忽略，不得因它存在改变行为
        elif arg.startswith(("--url=", "--model_name=", "--model_path=")):
            key, value = arg.split("=", 1)
            if key == "--url":
                url = value.strip()
            elif key == "--model_name":
                model = value.strip()
        elif arg in ("-h", "--help"):
            raise ParamError("用法：run.sh [--params-file <jsonl>] [--url=<base URL>] [--model_name=<name>]")
        else:
            unknown.append(arg)
    if unknown:
        raise ParamError(f"无法识别的参数：{' '.join(unknown)}（只接受 --params-file / --url / --model_name）")
    return params_file, url, model


def resolve_tokenizer() -> str:
    """分词器路径由平台注入的 AG_MODEL_PATH 决定（公共参数，不写进参数文件）。

    - 绝对路径（模型权重或分词器目录）：路径不存在直接判参数错误，避免跑到数据集构建才失败；
    - `builtin` 或 HF 仓库名：原样透传（builtin 零依赖，仓库名需要 Hub/本地缓存）；
    - 未注入：返回空串，agentx 会按模型名自行推导（离线环境通常推导不到，建议平台注入）。
    """
    value = (os.environ.get("AG_MODEL_PATH") or "").strip()
    if value.startswith("/") and not Path(value).exists():
        raise ParamError(
            f"AG_MODEL_PATH 指向的路径不存在：{value}"
            "（模型权重目录/分词器目录挂载了吗？也可设为 builtin）"
        )
    return value


def build_command(
    params: dict[str, Any],
    url: str,
    model: str,
    local_dataset: Path | None = None,
    tokenizer: str = "",
) -> list[str]:
    """把参数映射成 agentx 命令。工具的内部 CLI 规则只活在这里（平台不解析，§5.2）。"""
    cmd = shlex.split(params["agentx_cmd"]) + [
        "profile",
        "--url",
        url,
        "--endpoint-type",
        "chat",
        "--model",
        model,
        "--scenario",
        params["scenario"],
    ]
    if local_dataset is not None:
        # 本地语料：走 weka_trace loader（目录 + 一条 trace 一个 .json）。
        cmd += ["--custom-dataset-type", "weka_trace", "--input-file", str(local_dataset)]
    else:
        cmd += ["--public-dataset", params["public_dataset"]]
    cmd += [
        "--concurrency",
        str(params["concurrency"]),
        "--benchmark-duration",
        str(params["benchmark_duration_seconds"]),
        "--random-seed",
        str(params["random_seed"]),
        "--output-artifact-dir",
        str(ARTIFACTS),
        # 平台跑在无 TTY 的容器里：dashboard 会崩，none 只写文件与控制台表。
        "--ui",
        "none",
    ]
    if params["num_dataset_entries"]:
        cmd += ["--num-dataset-entries", str(params["num_dataset_entries"])]
    if params["max_context_length"]:
        cmd += ["--max-context-length", str(params["max_context_length"])]
    if tokenizer:
        cmd += ["--tokenizer", tokenizer]
    if params["warmup_requests_per_lane"]:
        cmd += ["--warmup-requests-per-lane", str(params["warmup_requests_per_lane"])]
    if params["agentic_cache_warmup_duration"]:
        cmd += ["--agentic-cache-warmup-duration", str(params["agentic_cache_warmup_duration"])]
    if local_dataset is not None:
        # 本地语料不是"钉住的公开语料"，scenario 规则要求显式放行（结果固定 submission_valid=false）。
        if "--unsafe-override" not in cmd:
            cmd += ["--unsafe-override"]
    elif params["benchmark_duration_seconds"] < MIN_DURATION_SECONDS:
        # allow_short_run 已在 validate 里放行；agentx 侧必须显式打开 override 才会跑。
        cmd += ["--unsafe-override"]
    if os.environ.get("ENDPOINT_API_KEY"):
        # ${VAR} 由 agentx 在配置解析期展开：argv 里只有引用，没有明文。
        cmd += ["--api-key", "${ENDPOINT_API_KEY}"]
    if params["extra_args"]:
        cmd += shlex.split(params["extra_args"])
    return cmd


def resolve_cache_dir(param_value: str, env_name: str, default: Path,
                      label: str) -> tuple[Path, str]:
    """缓存目录取值顺序：params.jsonl 的参数 > 环境变量 > 缺省（<output>/.cache 下）。

    以前只有缺省值，挂载好的 PVC 只能靠环境变量传；现在参数优先，写 params.jsonl 就能生效。
    """
    if param_value:
        return Path(param_value), f"参数 {label}"
    if os.environ.get(env_name):
        return Path(os.environ[env_name]), f"环境变量 {env_name}"
    return default, "缺省"


def child_env(params: dict[str, Any]) -> tuple[dict[str, str], Path, Path]:
    """子进程环境 + 语料/重建缓存目录。

    平台只给 /tmp（256MiB，execution §2.1）与输出目录可写，而公共语料是 1.85GB ——
    缓存缺省落在输出目录下的临时子目录，跑完删掉（keep_cache=true 时保留）；挂载了 PVC 就
    用 hf_cache_dir / mmap_cache_dir 参数指过去。
    """
    env = dict(os.environ)
    cache_root = OUT / ".cache"
    hf_dir, hf_src = resolve_cache_dir(
        params["hf_cache_dir"], "HF_HOME", cache_root / "hf", "hf_cache_dir")
    mmap_dir, mmap_src = resolve_cache_dir(
        params["mmap_cache_dir"], "AIPERF_DATASET_MMAP_CACHE_DIR", cache_root / "mmap", "mmap_cache_dir")
    for path, label in ((hf_dir, "HF 缓存"), (mmap_dir, "数据集重建缓存")):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ParamError(f"{label}目录不可创建/不可写：{path}（{exc}）") from exc
    log(f"HF 缓存：{hf_dir}（来源：{hf_src}）；重建缓存：{mmap_dir}（来源：{mmap_src}）")
    env["HF_HOME"] = str(hf_dir)
    env["AIPERF_DATASET_MMAP_CACHE_DIR"] = str(mmap_dir)
    env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    env.setdefault("PYTHONUNBUFFERED", "1")
    # agentx 的冷启动配置窗口默认 300s，重建 393 条 trace 会超；scenario 预设已抬到 1800s，
    # 这里再兜一层，避免换 scenario 后踩同一个坑。
    env.setdefault("AIPERF_DATASET_CONFIGURATION_TIMEOUT", "1800")
    env.setdefault("AIPERF_SERVICE_PROFILE_CONFIGURE_TIMEOUT", "1800")

    if params["offline"]:
        # 明确声明「不许出网」：任何漏掉的 Hub 调用都会立刻失败，而不是静默下载几百 MB。
        # （典型漏网场景：dataset_dir 走本地语料，但 tokenizer 留空 → 拿模型名去 Hub 解析分词器。）
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        env["HF_HUB_DISABLE_TELEMETRY"] = "1"
        log("offline=true：已设 HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE，本次执行不会访问 HuggingFace")

    return env, hf_dir, mmap_dir


def run_agentx(cmd: list[str], env: dict[str, str], log_path: Path) -> tuple[int, bool]:
    """流式跑 agentx：输出同时进平台日志与 logs/agentx.log；SIGTERM 视为取消。"""
    state = {"cancelled": False}
    holder: dict[str, subprocess.Popen[str] | None] = {"proc": None}

    def _on_signal(signum: int, _frame: Any) -> None:
        state["cancelled"] = True
        log(f"收到信号 {signum}：终止 agentx 子进程（Task 取消）")
        proc = holder["proc"]
        if proc is not None and proc.poll() is None:
            proc.terminate()

    with log_path.open("a", encoding="utf-8") as sink:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        holder["proc"] = proc
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, _on_signal)
        assert proc.stdout is not None
        for line in proc.stdout:
            sink.write(line)
            sink.flush()
            sys.stdout.write(line)
            sys.stdout.flush()
        code = proc.wait()

    return code, state["cancelled"]


# --- 结果归一化 -------------------------------------------------------------

def metric_value(export: dict[str, Any], key: str, stat: str) -> tuple[float | None, int | None]:
    node = export.get(key)
    if not isinstance(node, dict):
        return None, None
    value = node.get(stat)
    sample = node.get("count")
    number = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return number, (int(sample) if isinstance(sample, int) and not isinstance(sample, bool) else None)


# (指标名, 单位, statistic, 导出字段, 在该字段里取的键)
METRIC_MAP: tuple[tuple[str, str, str, str, str], ...] = (
    ("ttft", "ms", "mean", "time_to_first_token", "avg"),
    ("ttft", "ms", "p50", "time_to_first_token", "p50"),
    ("ttft", "ms", "p90", "time_to_first_token", "p90"),
    ("ttft", "ms", "p99", "time_to_first_token", "p99"),
    ("ttft", "ms", "max", "time_to_first_token", "max"),
    ("time_to_second_token", "ms", "mean", "time_to_second_token", "avg"),
    ("inter_token_latency", "ms", "mean", "inter_token_latency", "avg"),
    ("inter_token_latency", "ms", "p90", "inter_token_latency", "p90"),
    ("inter_token_latency", "ms", "p99", "inter_token_latency", "p99"),
    ("request_latency", "ms", "mean", "request_latency", "avg"),
    ("request_latency", "ms", "p50", "request_latency", "p50"),
    ("request_latency", "ms", "p90", "request_latency", "p90"),
    ("request_latency", "ms", "p99", "request_latency", "p99"),
    ("request_latency", "ms", "max", "request_latency", "max"),
    ("request_throughput", "req_per_s", "mean", "request_throughput", "avg"),
    ("output_token_throughput", "tokens_per_s", "mean", "output_token_throughput", "avg"),
    ("total_token_throughput", "tokens_per_s", "mean", "total_token_throughput", "avg"),
    ("output_token_throughput_per_user", "tokens_per_s", "mean", "output_token_throughput_per_user", "avg"),
    ("input_sequence_length", "tokens", "mean", "input_sequence_length", "avg"),
    ("output_sequence_length", "tokens", "mean", "output_sequence_length", "avg"),
    # 前缀缓存命中率是 AgentX 的核心特征：没有它，吞吐数字无法与别的服务对比。
    ("prefix_cache_hit", "percent", "mean", "theoretical_prefix_cache_hit", "avg"),
    ("request_count", "count", "count", "request_count", "avg"),
    ("benchmark_duration", "s", "max", "benchmark_duration", "avg"),
)


def build_metrics(export: dict[str, Any], params: dict[str, Any]) -> list[dict[str, Any]]:
    labels = {"concurrency": params["concurrency"]}
    metrics: list[dict[str, Any]] = []
    for name, unit, statistic, key, stat_key in METRIC_MAP:
        value, sample = metric_value(export, key, stat_key)
        if value is None:
            continue
        item: dict[str, Any] = {
            "name": name,
            "value": value,
            "unit": unit,
            "statistic": statistic,
            "labels": labels,
        }
        if sample is not None:
            item["sample_size"] = sample
        metrics.append(item)
    return metrics


def build_artifacts() -> list[dict[str, Any]]:
    wanted = (
        "profile_export_aiperf.json",
        "profile_export_aiperf.csv",
        "profile_export.jsonl",
        "profile_export_console.txt",
        "profile_export_aiperf_timeslices.csv",
        "profile_export_aiperf_timeslices.json",
        "server_metrics_export.json",
        "server_metrics_export.csv",
    )
    artifacts: list[dict[str, Any]] = []
    for name in wanted:
        path = ARTIFACTS / name
        if path.is_file():
            artifacts.append(
                {
                    "name": path.stem,
                    "path": str(path.relative_to(OUT)),
                    "size_bytes": path.stat().st_size,
                }
            )
    return artifacts


def build_notes(export: dict[str, Any] | None, params: dict[str, Any], extra: list[str]) -> str:
    notes: list[str] = list(extra)
    if export is not None:
        meta = export.get("metadata") if isinstance(export.get("metadata"), dict) else {}
        if meta.get("submission_valid") is False:
            reasons = meta.get("submission_invalid_reasons") or []
            notes.append("AgentX 提交有效性：false（" + (", ".join(map(str, reasons)) or "未给出原因") + "）")
        elif meta.get("submission_valid") is True:
            notes.append("AgentX 提交有效性：true（规则内运行）")
        mismatch, _ = metric_value(export, "osl_mismatch_count", "avg")
        if mismatch:
            notes.append(
                f"输出长度不一致的请求数：{int(mismatch)}"
                "（客户端 tokenizer 与服务端不一致时出现，可加 extra_args=\"--use-server-token-count\"）"
            )
        errors = export.get("error_summary")
        if errors:
            notes.append("agentx error_summary：" + str(errors)[:300])
    if params["benchmark_duration_seconds"] < MIN_DURATION_SECONDS:
        notes.append(
            f"短跑：benchmark_duration_seconds={params['benchmark_duration_seconds']} "
            f"< {MIN_DURATION_SECONDS}，仅用于冒烟，结论不可与正式数据比较"
        )
    if params["num_dataset_entries"]:
        notes.append(
            f"只回放了前 {params['num_dataset_entries']} 条 trace（num_dataset_entries），"
            "改变被回放的工作负载，结论不可与全量语料的运行比较"
        )
    if params["dataset_dir"]:
        notes.append(
            "使用本地语料目录（--custom-dataset-type weka_trace / --input-file）："
            "scenario 的「必须钉住公开语料」规则被 --unsafe-override 放行，"
            "结果固定 submission_valid=false，不可与公开语料的运行比较"
        )
    text = "；".join(n for n in notes if n)
    return text[:4000]


def write_result(payload: dict[str, Any]) -> None:
    tmp = RESULT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(RESULT)


def base_payload(status: str, started: str) -> dict[str, Any]:
    return {
        "schema_version": "2.0",
        "task_id": os.environ.get("AG_TASK_ID") or "local",
        "attempt_no": _env_int("AG_ATTEMPT", 1),
        "runner_seq": _env_int("AG_RUNNER_SEQ", 1),
        "runner": {
            "name": RUNNER_NAME,
            "version": RUNNER_VERSION,
            "image_ref": os.environ.get("AG_RUNNER_IMAGE") or "unknown",
        },
        "status": status,
        "started_at": started,
        "finished_at": utc_now(),
        "metrics": [],
        "error": None,
        "artifacts": [],
        "notes": "",
        "metadata": {"tool": "agentx-harness"},
    }


def _env_int(name: str, fallback: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    return int(raw) if raw.isdigit() else fallback


def _elapsed_ms(started: str) -> int:
    try:
        begin = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0
    return int((datetime.now(timezone.utc) - begin).total_seconds() * 1000)


def emit_failure(code: str, message: str, started: str, retryable: bool = False,
                 detail: dict[str, Any] | None = None, notes: str = "") -> None:
    payload = base_payload("failed", started)
    payload["error"] = {
        "code": code,
        "message": message[:1000],
        "detail": detail or {},
        "retryable": retryable,
    }
    payload["notes"] = notes
    payload["duration_ms"] = _elapsed_ms(started)
    write_result(payload)
    log(f"失败：{code} {message}")


def _tail(path: Path, lines: int) -> list[str]:
    if not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return [line.rstrip() for line in handle.readlines()[-lines:]]
    except OSError:
        return []


def run(argv: list[str]) -> int:
    started = utc_now()
    # run.sh 负责建目录，但独立调用（测试 / 直接 python 执行）时也要能跑。
    for path in (OUT, LOGS, RAW):
        path.mkdir(parents=True, exist_ok=True)

    # 1) 参数（§5.3 输入文件 + §5.4 语义校验）
    try:
        params_file, url, model = parse_argv(argv)
        if not url:
            raise ParamError("缺少被测服务地址（--url 或环境变量 AG_SERVED_URL，§5.4.1）")
        if not model:
            raise ParamError("缺少模型名（--model_name 或 AG_SERVED_MODEL_NAME，§5.4.1）")
        params = validate(load_params(params_file))
    except ParamError as exc:
        emit_failure("VALIDATION_ERROR", str(exc), started, notes="参数校验未通过，未执行任何压测")
        return 2

    log(
        "开始：scenario={scenario} concurrency={concurrency} duration={duration}s "
        "dataset={dataset} seed={seed}".format(
            scenario=params["scenario"],
            concurrency=params["concurrency"],
            duration=params["benchmark_duration_seconds"],
            dataset=params["public_dataset"],
            seed=params["random_seed"],
        )
    )

    # 2) 本地语料（可选）：把 traces.jsonl 拆成 --input-file 需要的「一 trace 一 .json」
    local_dataset: Path | None = None
    if params["dataset_dir"]:
        try:
            local_dataset = prepare_local_dataset(resolve_dataset_dir(params["dataset_dir"]))
        except ParamError as exc:
            emit_failure("VALIDATION_ERROR", str(exc), started, notes="本地语料不可用")
            return 2

    if not (os.environ.get("AG_MODEL_PATH") or "").strip():
        log(
            "提示：未注入 AG_MODEL_PATH —— agentx 会拿模型名去 HuggingFace 解析分词器（离线会失败）。"
            "离线请由平台注入 AG_MODEL_PATH（如 /models/Qwen3-32B），或设 AG_MODEL_PATH=builtin。"
        )

    # 3) 调用 agentx（tokenizer 来自平台注入的 AG_MODEL_PATH）
    try:
        tokenizer = resolve_tokenizer()
    except ParamError as exc:
        emit_failure("VALIDATION_ERROR", str(exc), started, notes="模型路径不可用")
        return 2
    env, hf_dir, mmap_dir = child_env(params)
    cmd = build_command(params, url, model, local_dataset, tokenizer)
    # 命令里只有 ${ENDPOINT_API_KEY} 引用形态的密钥，可以安全落日志。
    log("命令：" + " ".join(shlex.quote(part) for part in cmd))
    log(f"实际使用：HF_HOME={env['HF_HOME']}，AIPERF_DATASET_MMAP_CACHE_DIR={env['AIPERF_DATASET_MMAP_CACHE_DIR']}")

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    try:
        code, cancelled = run_agentx(cmd, env, AGENTX_LOG)
    finally:
        if not params["keep_cache"]:
            shutil.rmtree(OUT / ".cache", ignore_errors=True)

    if cancelled:
        emit_failure("CANCELED", f"执行被取消（agentx exit={code}）", started,
                     notes="Task 取消，结果不完整")
        return 1

    # 3) 结果归一化
    export_path = ARTIFACTS / "profile_export_aiperf.json"
    export: dict[str, Any] | None = None
    transcript: list[str] = []
    if export_path.is_file():
        try:
            loaded = json.loads(export_path.read_text(encoding="utf-8"))
            export = loaded if isinstance(loaded, dict) else None
        except (OSError, json.JSONDecodeError) as exc:
            transcript.append(f"profile_export_aiperf.json 解析失败：{exc}")

    if code != 0:
        tail = _tail(AGENTX_LOG, 40)
        emit_failure(
            "RUNNER_NONZERO_EXIT",
            f"agentx 退出码 {code}：" + (" / ".join(tail[-6:]) if tail else "见 logs/agentx.log"),
            started,
            detail={"agentx_exit_code": code, "artifact_dir": "agentx"},
        )
        return 1

    if export is None:
        emit_failure(
            "RESULT_INVALID",
            "agentx 退出码 0 但没有可解析的 agentx/profile_export_aiperf.json",
            started,
            detail={"artifact_dir": "agentx"},
        )
        return 1

    payload = base_payload("succeeded", started)
    payload["duration_ms"] = _elapsed_ms(started)
    payload["metrics"] = build_metrics(export, params)
    payload["artifacts"] = build_artifacts()
    payload["workload"] = {
        "base_url": url,
        "model_name": model,
        "scenario": params["scenario"],
        "dataset_source": "local" if params["dataset_dir"] else "public",
        "concurrency": params["concurrency"],
        "benchmark_duration_seconds": params["benchmark_duration_seconds"],
        "random_seed": params["random_seed"],
    }
    if params["dataset_dir"]:
        payload["workload"]["dataset_dir"] = params["dataset_dir"]
        payload["workload"]["dataset_path"] = str(resolve_dataset_dir(params["dataset_dir"]))
    else:
        payload["workload"]["public_dataset"] = params["public_dataset"]
    if params["num_dataset_entries"]:
        payload["workload"]["num_dataset_entries"] = params["num_dataset_entries"]
    if params["max_context_length"]:
        payload["workload"]["max_context_length"] = params["max_context_length"]
    if tokenizer:
        payload["workload"]["tokenizer"] = tokenizer
    meta = export.get("metadata") if isinstance(export.get("metadata"), dict) else {}
    payload["metadata"] = {
        "tool": "agentx-harness",
        "tool_version": str(export.get("aiperf_version") or "unknown"),
        "scenario": meta.get("scenario") or params["scenario"],
        "submission_valid": meta.get("submission_valid"),
        "submission_invalid_reasons": meta.get("submission_invalid_reasons") or [],
        "hf_cache_dir": str(hf_dir),
        "mmap_cache_dir": str(mmap_dir),
        "dataset_cache_kept": bool(params["keep_cache"]),
        "command": " ".join(shlex.quote(part) for part in cmd),
    }
    payload["notes"] = build_notes(export, params, transcript)
    write_result(payload)

    log(
        "完成：status=succeeded metrics={metrics} artifacts={artifacts} duration={seconds:.1f}s".format(
            metrics=len(payload["metrics"]),
            artifacts=len(payload["artifacts"]),
            seconds=payload["duration_ms"] / 1000,
        )
    )
    return 0


def main(argv: list[str]) -> int:
    started = utc_now()
    try:
        code = run(argv)
    except Exception as exc:  # noqa: BLE001 - Runner 内部异常也要留下可读结果（§5.9）
        emit_failure("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", started)
        code = 1
    finally:
        # 兜底清理：正常/失败/取消路径都不把 1.85GB 缓存留在产物里。
        shutil.rmtree(OUT / ".cache", ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

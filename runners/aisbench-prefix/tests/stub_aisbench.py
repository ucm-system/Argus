#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线冒烟用的假 `ais_bench`（**测试夹具，不进发布镜像**，见 tests/Dockerfile.stub）。

它只做两件真 ais_bench 对本 Runner 有意义的事：

1. **从模型配置文件里读回本次真正生效的参数**（`batch_size` / `max_out_len` / `url` /
   `request_rate` / `model`）。工具（vendored `aisbench_test.py`）在每跑一段之前都会重写
   `<work_path>/ais_bench/benchmark/configs/models/vllm_api/vllm_api_chat_temp.py`
   并 `--config-dir` 指过去，所以「桩认为自己压的是几并发」= 管道认为自己在压几并发。
   顺带一条硬断言：该文件里**不得出现明文 api_key**（凭据只走 env，§12.4）。
2. **打印能被 `save_file.get_data()` 解析的汇总表**，并把「这一次真实压测量」推给
   mock 服务（`POST /__sim__/traffic`），让 /metrics 的 prefix_cache 计数器动起来 ——
   命中率差分因此有数可算。

表格式照抄 ais_bench 3.x 的 `│` 表：首个单元格必须是**裸指标名**（`TTFT` 而不是
`TTFT (ms)`）——`save_file._get_metric_row` 是按第一个单元格等于关键字来匹配的；
TTFT/TPOT/E2EL 三行取第 6 个数当 p90、第 1 个当 avg，所以列顺序是 load-bearing。

环境变量（都可选）：

    STUB_WORK_DIR       兜底的影子根（找不到 --config-dir 时用）
    STUB_FAIL_AT        逗号分隔的 batch_size，命中则打印半截表后 exit 1
    STUB_SLEEP          每个请求之间的休眠秒数（测 run_timeout）
    STUB_REPEAT_RATE    推给 mock 的命中率（默认从数据集文件名 repeatRate<x> 解析）
    STUB_TRAFFIC_URL    mock 基地址兜底（默认取模型配置里的 url/host）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

CFG_REL = "ais_bench/benchmark/configs/models/vllm_api/vllm_api_chat_temp.py"
DATASET_REL = "ais_bench/datasets/gsm8k/test.jsonl"

_RE_BATCH = re.compile(r"batch_size\s*[=:]\s*['\"]?(\d+)")
_RE_OUTLEN = re.compile(r"max_out_len\s*[=:]\s*['\"]?(\d+)")
_RE_URL = re.compile(r"\burl\s*[=:]\s*['\"]([^'\"]*)['\"]")
_RE_HOST = re.compile(r"host_ip\s*[=:]\s*['\"]([^'\"]*)['\"]")
_RE_PORT = re.compile(r"host_port\s*[=:]\s*['\"]?(\d+)")
_RE_MODEL = re.compile(r"\bmodel\s*[=:]\s*['\"]([^'\"]*)['\"]")
_RE_RATE = re.compile(r"request_rate\s*[=:]\s*['\"]?([0-9.]+)")
_RE_PLAINTEXT_KEY = re.compile(r"api_key\s*[=:]\s*['\"]sk-")


def warn(msg):
    sys.stderr.write(f"[stub-aisbench] {msg}\n")


def parse_args():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--models", default="vllm_api_chat_temp")
    parser.add_argument("--datasets", default="gsm8k_gen_0_shot_cot_str_perf")
    parser.add_argument("--mode", default="perf")
    parser.add_argument("--summarizer", default="default_perf")
    parser.add_argument("--work-dir", dest="work_dir", default="outputs/default")
    parser.add_argument("--config-dir", dest="config_dir", default="")
    parser.add_argument("--num-warmups", dest="num_warmups", default=None)
    parser.add_argument("--dump-eval-details", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args, unknown = parser.parse_known_args()
    if unknown:
        warn(f"未识别的参数被忽略：{unknown}")
    return args


def find_config(args):
    """定位工具刚写下的模型配置（找不到就退回影子根/默认路径）。"""
    roots = []
    if args.config_dir:
        roots.append(Path(args.config_dir))
    for env_name in ("STUB_WORK_DIR", "AIS_BENCH_DATASETS_CACHE"):
        value = os.environ.get(env_name)
        if value:
            roots.append(Path(value) / "ais_bench/benchmark/configs")
    roots.append(Path("/benchmark/ais_bench/benchmark/configs"))
    for root in roots:
        candidate = root / "models" / "vllm_api" / f"{args.models}.py"
        if candidate.is_file():
            return candidate
    return None


def read_params(config_path):
    text = config_path.read_text(encoding="utf-8", errors="replace") if config_path else ""
    if text and _RE_PLAINTEXT_KEY.search(text):
        # 明文 key 落盘 = 凭据泄漏（§12.4 / 评审 #62 硬问题 1）。桩直接判失败。
        warn("模型配置里出现明文 api_key（形如 api_key=\"sk-…\"），拒绝继续")
        raise SystemExit(9)

    def grab(pattern, default=None, cast=str):
        match = pattern.search(text)
        if not match:
            return default
        try:
            return cast(match.group(1))
        except ValueError:
            return default

    url = grab(_RE_URL, "") or ""
    host = grab(_RE_HOST, "127.0.0.1")
    port = grab(_RE_PORT, 8000, int)
    if not url:
        url = f"http://{host}:{port}"
    return {
        "batch_size": grab(_RE_BATCH, 1, int),
        "max_out_len": grab(_RE_OUTLEN, 8, int),
        "url": url.rstrip("/"),
        "model": grab(_RE_MODEL, "stub-model"),
        "request_rate": grab(_RE_RATE, 0.0, float),
        "config_text": text,
    }


def read_dataset():
    """读工具挂进 gsm8k 目录的 test.jsonl（假 tokenizer：1 字符 = 1 token）。"""
    root = os.environ.get("AIS_BENCH_DATASETS_CACHE") or os.environ.get("STUB_WORK_DIR") or ""
    if not root:
        return 0, 0, ""
    path = Path(root) / DATASET_REL
    if not path.is_file():
        warn(f"数据集不存在：{path}")
        return 0, 0, str(path)
    lines = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    longest = 0
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        longest = max(longest, len(str(obj.get("question", ""))))
    return len(lines), longest, str(path)


def push_traffic(base, queries, hits):
    ext_queries = queries // 20
    ext_hits = int(ext_queries * 0.25)
    base = re.sub(r"/v1/?$", "", (base or "").rstrip("/"))
    url = (f"{base}/__sim__/traffic?queries={queries}&hits={hits}"
           f"&ext_queries={ext_queries}&ext_hits={ext_hits}")
    request = urlrequest.Request(url, data=b"", method="POST")
    try:
        with urlrequest.urlopen(request, timeout=5) as response:  # noqa: S310 - 本地 mock
            warn(f"traffic pushed queries={queries} hits={hits} -> {response.status}")
    except (urlerror.URLError, OSError, ValueError) as exc:
        warn(f"traffic 推送失败（{type(exc).__name__}: {exc}）；汇总表仍然有效")


def latency_model(concurrency):
    """确定性的假延时分位数（列顺序被 save_file 的解析规则依赖，别乱改）。"""
    avg = 40.0 + 8.0 * concurrency
    return {
        "avg": avg,
        "min": avg * 0.6,
        "max": avg * 3.0,
        "median": avg * 1.02,
        "p75": avg * 1.2,
        "p90": avg * 1.5,
        "p99": avg * 2.2,
    }


def row(name, stats, unit="ms", n=0):
    cells = " │ ".join(f"{stats[key]:>10.2f} {unit}" for key in
                       ("avg", "min", "max", "median", "p75", "p90", "p99"))
    return f"│ {name:<24} │ total   │ {cells} │ {n:>3}  │"


def main():
    args = parse_args()
    config_path = find_config(args)
    params = read_params(config_path)
    data_num, input_len, dataset_path = read_dataset()
    if config_path is None:
        warn("找不到模型配置文件，退化用默认参数（管道没接通 config-dir）")
    if data_num == 0:
        warn(f"数据集为空/缺失：{dataset_path or '<no cache root>'}（--dataset 没给对？）")

    batch = int(params["batch_size"] or 1)
    out_len = int(params["max_out_len"] or 1)
    input_len = input_len or 128
    data_num = data_num or 4
    exp_folder = str(Path(args.work_dir) / time.strftime("%Y%m%d_%H%M%S"))
    (Path(exp_folder) / "logs" / "infer").mkdir(parents=True, exist_ok=True)
    (Path(exp_folder) / "results").mkdir(parents=True, exist_ok=True)

    print(f"Current exp folder: {exp_folder}")
    print(f"[stub] models={args.models} datasets={args.datasets} mode={args.mode} "
          f"summarizer={args.summarizer} batch_size={batch} max_out_len={out_len} "
          f"dataset={dataset_path}")
    print(f"[stub] config={config_path} url={params['url']} model={params['model']} "
          f"request_rate={params['request_rate']}")

    fail_at = {item.strip() for item in
               re.split(r"[,\s]+", os.environ.get("STUB_FAIL_AT", "")) if item.strip()}
    sleep_s = float(os.environ.get("STUB_SLEEP", "0") or 0)

    per_request_ms = latency_model(batch)["avg"] + (6.0 + 0.6 * batch) * out_len
    duration_ms = max(1.0, per_request_ms * data_num / max(1, batch))
    if str(batch) in fail_at or "all" in fail_at:
        # 只打一半就退出非零：上游那种 `| tee` 结尾的 shell 命令会把真实退出码吞掉，
        # 本 Runner 走 bash -o pipefail，所以这里必须以失败传出去（smoke 断言这点）。
        warn(f"STUB_FAIL_AT 命中 batch_size={batch}，打印半截表后 exit 1")
        print(row("TTFT", latency_model(batch), n=data_num))
        sys.exit(1)

    if sleep_s > 0:
        time.sleep(sleep_s * data_num)

    repeat_rate = os.environ.get("STUB_REPEAT_RATE")
    if not repeat_rate:
        match = re.search(r"repeatRate([0-9.]+)", dataset_path or "")
        repeat_rate = match.group(1) if match else "0.5"
    queries = data_num * input_len
    hits = int(round(queries * float(repeat_rate)))
    push_traffic(os.environ.get("STUB_TRAFFIC_URL") or params["url"], queries, hits)

    ttft = latency_model(batch)
    tpot = latency_model(batch * 0.15)
    e2el = latency_model(batch * 0.5)
    duration_s = max(1e-6, duration_ms / 1000.0)
    total_in = data_num * input_len
    total_out = data_num * out_len

    print(f"│ {'E2EL':<24} │ total   │ " + " │ ".join(
        f"{e2el[key]:>10.2f} ms" for key in ("avg", "min", "max", "median", "p75", "p90", "p99"))
        + f" │ {data_num:>3}  │")
    print(row("TTFT", ttft, n=data_num))
    print(row("TPOT", tpot, n=data_num))
    print(f"│ {'Max Concurrency':<24} │ {batch} │")
    # 实测并发是浮点（真 ais_bench 给的是跑出来的均值）；save_file 的 TTFT/Concurrency
    # 规则用的是 (\d+\.\d+)，整数会匹配不到 → 留哨兵值。
    print(f"│ {'Concurrency':<24} │ {float(batch) * 0.92:.2f} │")
    print(f"│ {'Benchmark Duration':<24} │ {duration_ms:.2f} │")
    print(f"│ {'Total Requests':<24} │ {data_num} │")
    print(f"│ {'InputTokens':<24} │ {total_in} │")
    print(f"│ {'OutputTokens':<24} │ {total_out} │")
    print(f"│ {'Output Token Throughput':<24} │ {total_out / duration_s:.2f} tok/s │")
    print(f"│ {'Input Token Throughput':<24} │ {total_in / duration_s:.2f} tok/s │")
    print(f"│ {'Total Token Throughput':<24} │ {(total_in + total_out) / duration_s:.2f} tok/s │")
    print(f"│ {'Prefill Token Throughput':<24} │ {total_in / max(1e-6, (ttft['avg'] / 1000.0) * data_num / max(1, batch)):.2f} tok/s │")
    print(f"│ {'Request Throughput':<24} │ {data_num / duration_s:.2f} req/s │")

    (Path(exp_folder) / "results" / "stub.json").write_text(
        json.dumps({"batch_size": batch, "data_num": data_num, "input_len": input_len,
                    "output_len": out_len, "duration_ms": duration_ms,
                    "url": params["url"], "model": params["model"]},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (Path(exp_folder) / "logs" / "infer" / "stub.out").write_text(
        "stub ais_bench run\n", encoding="utf-8")
    print("[stub] 汇总表打印完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())

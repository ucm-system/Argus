#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Argus
# SPDX-License-Identifier: Apache-2.0
"""离线 stub：冒充 ``agentx profile``，把 fixture 当成本次运行的产物。

为什么需要：真实 agentx 要下载 1.85GB 语料并跑几分钟，回归与 CI 不可能每轮都跑。
stub 只验证 Runner 侧的契约行为（参数映射、产物定位、结果归一化、退出码），
不验证 agentx 自己的压测逻辑 —— 后者由真实运行的冒烟覆盖。

用法（由 agentx_cmd 参数指定）：
    {"agentx_cmd": "python3 /runner/tests/stub_agentx.py"}
环境变量：
    STUB_EXIT=1  让 stub 以 1 退出（模拟 agentx 失败）
    STUB_EXIT=2  让 stub 以 2 退出（模拟参数错误）

约定：控制台输出只用 ASCII。真实 agentx 用 rich 打印 Unicode 表格，而 AgentX Runner 以
``text=True`` 读子进程 stdout，宿主 locale 不是 UTF-8 时（中文 Windows 的 GBK）会解码崩溃；
stub 不该把这个宿主环境问题带进回归。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "profile_export_aiperf.json"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("command", nargs="?", default="profile")
    parser.add_argument("--url")
    parser.add_argument("--endpoint-type", dest="endpoint_type")
    parser.add_argument("--model")
    parser.add_argument("--output-artifact-dir", dest="output_artifact_dir", required=True)
    parser.add_argument("--scenario")
    parser.add_argument("--concurrency")
    parser.add_argument("--benchmark-duration", dest="benchmark_duration")
    parser.add_argument("--random-seed", dest="random_seed")
    parser.add_argument("--public-dataset", dest="public_dataset")
    parser.add_argument("--custom-dataset-type", dest="custom_dataset_type")
    parser.add_argument("--input-file", dest="input_file")
    parser.add_argument("--num-dataset-entries", dest="num_dataset_entries")
    parser.add_argument("--max-context-length", dest="max_context_length")
    parser.add_argument("--tokenizer")
    parser.add_argument("--warmup-requests-per-lane", dest="warmup_requests_per_lane")
    parser.add_argument("--agentic-cache-warmup-duration", dest="agentic_cache_warmup_duration")
    parser.add_argument("--api-key", dest="api_key")
    parser.add_argument("--ui")
    parser.add_argument("--unsafe-override", dest="unsafe_override", action="store_true")
    args, unknown = parser.parse_known_args(argv)

    # 只打「收到了什么」，方便回归核对 Runner 的命令映射；不打印 api-key 的值（可能是引用形态）。
    print(
        "[stub] agentx {cmd} url={url} endpoint={endpoint} model={model} scenario={scenario} "
        "concurrency={conc} duration={dur} seed={seed} ui={ui} override={ov}".format(
            cmd=args.command,
            url=args.url,
            endpoint=args.endpoint_type,
            model=args.model,
            scenario=args.scenario,
            conc=args.concurrency,
            dur=args.benchmark_duration,
            seed=args.random_seed,
            ui=args.ui,
            ov=bool(args.unsafe_override),
        ),
        flush=True,
    )
    print(
        "[stub] dataset public={pub} custom={custom} input_file={inp} "
        "entries={entries} max_ctx={ctx} tokenizer={tok} warmup_req={wreq} "
        "warmup_dur={wdur} api_key={has_key}".format(
            pub=args.public_dataset,
            custom=args.custom_dataset_type,
            inp=args.input_file,
            entries=args.num_dataset_entries,
            ctx=args.max_context_length,
            tok=args.tokenizer,
            wreq=args.warmup_requests_per_lane,
            wdur=args.agentic_cache_warmup_duration,
            has_key="yes" if args.api_key else "no",
        ),
        flush=True,
    )
    if unknown:
        print("[stub] ignored extra args: " + " ".join(unknown), flush=True)
    # 被压测地址缺失：与真 agentx 一样按参数错误退出。
    if args.command != "profile":
        print(f"[stub] unsupported command {args.command}", file=sys.stderr, flush=True)
        return 2
    if not args.url or not args.model:
        print("[stub] missing --url / --model", file=sys.stderr, flush=True)
        return 2

    forced = os.environ.get("STUB_EXIT")
    if forced:
        print(f"[stub] exiting with {forced} as requested", file=sys.stderr, flush=True)
        return int(forced)

    out = Path(args.output_artifact_dir)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE, out / "profile_export_aiperf.json")
    (out / "profile_export_aiperf.csv").write_text(
        "metric,avg\nrequest_throughput,0.323\n", encoding="utf-8")
    (out / "profile_export.jsonl").write_text(
        json.dumps({"metadata": {"session_num": 0}, "metrics": {}}) + "\n", encoding="utf-8")
    (out / "profile_export_console.txt").write_text(
        "stub console table\n", encoding="utf-8")

    print("+-----------------------------------+", flush=True)
    print("| stub | LLM Metrics                 |", flush=True)
    print("+-----------------------------------+", flush=True)
    print("| Time to First Token (ms)  | 21.41 |", flush=True)
    print("+---------------------------+-------+", flush=True)
    print(f"JSON Export: {out / 'profile_export_aiperf.json'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

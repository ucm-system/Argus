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
    parser.add_argument("--model")
    parser.add_argument("--output-artifact-dir", required=True)
    parser.add_argument("--concurrency")
    parser.add_argument("--benchmark-duration")
    parser.add_argument("--tokenizer")
    parser.add_argument("--public-dataset")
    parser.add_argument("--scenario")
    parser.add_argument("--api-key")
    parser.add_argument("--ui")
    parser.add_argument("--unsafe-override", action="store_true")
    args, unknown = parser.parse_known_args(argv)

    print(f"[stub] agentx {args.command} url={args.url} model={args.model} "
          f"concurrency={args.concurrency} duration={args.benchmark_duration} "
          f"scenario={args.scenario} dataset={args.public_dataset} ui={args.ui}")
    if unknown:
        print(f"[stub] 忽略的额外参数：{' '.join(unknown)}")
    # 被压测地址缺失：与真 agentx 一样按参数错误退出。
    if args.command != "profile":
        print(f"[stub] 不支持的命令 {args.command}", file=sys.stderr)
        return 2
    if not args.url or not args.model:
        print("[stub] 缺少 --url / --model", file=sys.stderr)
        return 2

    forced = os.environ.get("STUB_EXIT")
    if forced:
        print(f"[stub] 按要求以 {forced} 退出", file=sys.stderr)
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

    print("┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓")
    print("┃ stub | LLM Metrics                ┃")
    print("┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩")
    print("│ Time to First Token (ms) │ 21.41 │")
    print("└──────────────────────────┴───────┘")
    print(f"JSON Export: {out / 'profile_export_aiperf.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

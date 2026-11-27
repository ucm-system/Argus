#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 vendored 工具的 sidecar 事件汇成 Argus 的 `result.json`（Runner 自有代码，非上游）。

契约：design/ARCHITECTURE.md 附录 A（schema 2.0）、§5.3（产物写 AG_OUTPUT_DIR）、
§5.8（退出码）、§8.3 / openapi `ResultMetric`（**名字只放指标本身**，统计量走
`statistic`、单位走 `unit`、维度走 `labels`）。

输入（由 run.sh 准备好）：

* `$AG_OUTPUT_DIR/prefix_out/points/point-<c>/events.jsonl` —— vendored 工具
  `prefix_env.emit()` 写出的结构化事件（`config` / `metrics` / `hit` / `aisbench`）；
* 环境变量 `AG_TASK_ID` / `AG_ATTEMPT` / `AG_RUNNER_SEQ` / `AG_RUNNER_IMAGE` /
  `AG_STARTED` / `AG_START_EPOCH` / `AG_STATUS` / `AG_CODE` / `AG_REASON` / `AG_RC` /
  `AG_EXTRA_NOTE` / `AG_RETRYABLE` / `AG_TOOL_VERSION` / `AG_PARAMS_SUMMARY`。

为什么不解析日志文本：`get_data()` 用正则从 aisbench.log 里抠数，抠不到时留的是
`DEFAULT_METRICS` 的哨兵值（99999 / 9999）——「没测到」和「测出来就是这个数」在 CSV 里
长得一样。事件里带的是同一份 dict，但**多了 phase 与 batch_size**，能分清哪个并发点、
哪个阶段；哨兵值在这里统一按「缺失」处理，并在 `notes` 写明缺了哪几项（不编数）。
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
import time

MAX_ARTIFACT_BYTES = 50 * 1024 * 1024      # 单产物上限：超了保留末尾（日志尾部才是现场）
MAX_ARTIFACT_COUNT = 300                   # 产物条数上限：长上下文一轮能出上千个小文件
# save_file.DEFAULT_METRICS 里「没解析到」的哨兵值。原样写进 result.json 会被下游
# 当成真实性能，所以一律丢弃并记进 notes。
SENTINELS = {99999, 9999, "99999", "9999", None, ""}

# (metrics 字段, 指标名, 单位, 统计量, 附加 labels)
METRIC_MAP = [
    ("TTFT avg", "ttft", "ms", "mean", {}),
    ("TTFT P90", "ttft", "ms", "p90", {}),
    ("TPOT avg", "tpot", "ms", "mean", {}),
    ("TPOT SLO_P90", "tpot", "ms", "p90", {}),
    ("E2EL avg", "e2el", "ms", "mean", {}),
    ("E2EL P90", "e2el", "ms", "p90", {}),
    ("output_throughput", "output_throughput", "tokens_per_s", "mean", {"scope": "total"}),
    ("single_output_throughput", "output_throughput", "tokens_per_s", "mean",
     {"scope": "per_device"}),
    ("input_token_throughput", "input_token_throughput", "tokens_per_s", "mean", {}),
    ("prefill_token_throughput", "prefill_token_throughput", "tokens_per_s", "mean", {}),
    ("E2E_throughput", "total_token_throughput", "tokens_per_s", "mean", {"scope": "total"}),
    ("single_E2E_throughput", "total_token_throughput", "tokens_per_s", "mean",
     {"scope": "per_device"}),
    ("qps", "request_throughput", "req_per_s", "mean", {}),
    ("cc", "measured_concurrency", "count", "mean", {}),
    ("max_cc", "target_concurrency", "count", "count", {}),
    ("total_req", "requests_total", "count", "count", {}),
]


def as_number(value):
    if isinstance(value, bool) or value in SENTINELS:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def read_events(points_root):
    """→ [(point_dir, [event, ...])]，按 point 序号升序。"""
    found = []
    for path in glob.glob(os.path.join(points_root, "point-*", "events.jsonl")):
        rows = []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        rows.append({"kind": "broken", "raw": line[:200]})
        except OSError as exc:
            print(f"[summarize] 警告：读不到 {path}: {exc}", file=sys.stderr)
        point = os.path.basename(os.path.dirname(path))
        order = int(point.split("-")[-1]) if point.split("-")[-1].isdigit() else 10 ** 9
        found.append((order, point, rows))
    found.sort(key=lambda item: item[0])
    return [(point, rows) for _order, point, rows in found]


def hit_events(events):
    """每个 point 的 hit 事件（同一 phase 取最后一条），→ [(concurrency, event, point)]。"""
    picked = {}
    for point, rows in events:
        for event in rows:
            if event.get("kind") == "hit" and event.get("phase") == "full":
                picked[point] = event        # 同一目录里后写的覆盖先写的
    return picked


def point_index(point):
    """point-8 → 8；拿不到数字时退回目录名本身（只用于 labels.point 溯源）。"""
    tail = point.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else point


def add_metric(metrics, seen, entry, dropped):
    """写指标前先保证**键唯一**（附录 A：name+statistic+labels 不得重复）。

    平台侧会因此判 `RESULT_INVALID`（kind 真跑实测：两点阶梯的预热阶段并发都是
    `prefix_num`，labels 完全撞车）。撞了就留第一条、丢后来的并记进 notes——
    绝不为了"看起来有数"而造一个假维度。
    """
    key = (entry["name"], entry["statistic"],
           json.dumps(entry.get("labels") or {}, sort_keys=True, ensure_ascii=False))
    if key in seen:
        dropped.append("%s[%s]%s" % (entry["name"], entry["statistic"],
                                     sorted(entry.get("labels", {}).items())))
        return False
    seen.add(key)
    metrics.append(entry)
    return True


def build_metrics(events):
    """sidecar → metrics[]，返回 (metrics, missing_notes, point_summaries)。"""
    metrics = []
    missing = []
    points = []
    seen = set()
    missing_duplicates = []
    for point, rows in events:
        config = next((r for r in rows if r.get("kind") == "config"), {})
        for row in rows:
            if row.get("kind") != "metrics":
                continue
            phase = row.get("phase") or "full"
            values = row.get("values") or {}
            concurrency = row.get("concurrency")
            # labels.point 是**必需的维度**：两点阶梯的预热阶段并发相同（都= prefix_num 或 dp），
            # 少了它平台就判「metrics[i] 与前面重复」→ 整个 Task RESULT_INVALID（kind 实测）。
            labels = {"concurrency": concurrency, "phase": phase, "point": point_index(point)}
            if labels.get("concurrency") is None:
                labels["concurrency"] = "unknown"
            added = 0
            absent = []
            for key, name, unit, statistic, extra in METRIC_MAP:
                value = as_number(values.get(key))
                if value is None:
                    absent.append("%s[%s]" % (name, statistic))
                    continue
                metric_labels = dict(labels)
                metric_labels.update(extra)
                entry = {"name": name, "value": round(value, 3), "unit": unit,
                         "statistic": statistic, "labels": metric_labels}
                sample = as_number(values.get("total_req"))
                if sample is not None and unit in ("ms", "tokens_per_s", "req_per_s"):
                    entry["sample_size"] = int(sample)
                if add_metric(metrics, seen, entry, missing_duplicates):
                    added += 1
            points.append({"point": point, "phase": phase, "concurrency": concurrency,
                           "metrics": added,
                           "exp_folder": row.get("exp_folder") or ""})
            if added == 0:
                missing.append(
                    f"{point}/{phase}：aisbench.log 里没解析到任何性能统计量"
                    f"（ais_bench 可能没跑完，见该点的 aisbench.log）")
            elif absent:
                # 哨兵值（99999/9999）= 工具没从汇总表里抠到这个数。宁可写明「缺哪项」，
                # 也不把哨兵当测量值写进 metrics。
                missing.append("%s/%s 未解析到：%s" % (point, phase, ", ".join(absent)))
    # 命中率：**每个并发点都要有自己的命中率**（只取最后一点会把阶梯里其余点的证据丢掉）
    picks = hit_events(events)
    if not picks:
        missing.append("hit_rate（工具没算出命中率快照：--prefix_test 关闭，"
                       "或目标服务 /metrics 没有 vllm:prefix_cache_* 计数器）")
    for point, event in sorted(picks.items()):
        rows = event.get("rows") or []
        reason = event.get("reason") or ""
        all_pods = next((r for r in rows if r.get("scope") == "ALL_PODS"), None)
        labels_base = {"concurrency": event.get("concurrency", point),
                       "phase": "full", "point": point_index(point)}
        if all_pods is None:
            missing.append("hit_rate@%s（无可用快照：%s）" % (point, reason or "没有 ALL_PODS 行"))
            continue
        external_ok = bool(all_pods.get("external_available") or
                           event.get("external_available"))
        for scope, hits_key, queries_key in (
                ("hbm", "hbm_hits", "hbm_queries"),
                ("external", "external_hits", "external_queries")):
            if scope == "external" and not external_ok:
                # vLLM 默认不暴露 external_prefix_cache_*：没有计数器 ≠ 命中率 0%
                missing.append("hit_rate[external]@%s（目标服务没有 external_prefix_cache_* "
                               "计数器，未上报而不是记 0）" % point)
                continue
            hits = as_number(all_pods.get(hits_key))
            queries = as_number(all_pods.get(queries_key))
            if queries is None or queries <= 0:
                missing.append("hit_rate[%s]@%s（queries=0，命中率无定义）" % (scope, point))
                continue
            add_metric(metrics, seen, {
                "name": "hit_rate",
                "value": round(hits / queries * 100.0, 2),
                "unit": "percent",
                "statistic": "mean",
                "labels": dict(labels_base, scope=scope),
                "sample_size": int(queries),
            }, missing_duplicates)
    if missing_duplicates:
        missing.append("键重复被丢弃的指标（name+statistic+labels 必须唯一，附录 A）："
                       + "; ".join(missing_duplicates[:6]))
    return metrics, missing, points


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tail_truncate(path, limit):
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        handle.seek(size - limit)
        data = handle.read()
    tmp = path + ".truncated"
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)
    return size


def collect_artifacts(out):
    """列产物：每点的日志/CSV/配置/事件 + ais_bench 的 work-dir；datasets 只列名不列内容。"""
    patterns = [
        "prefix_out/points/point-*/events.jsonl",
        "prefix_out/points/point-*/aisbench.log",
        "prefix_out/points/point-*/aisbench_all.log",
        "prefix_out/points/point-*/aisbench_result.csv",
        "prefix_out/points/point-*/temp_api.py",
        "prefix_out/points/point-*/work/**",
        "prefix_out/run.log",
    ]
    candidates, seen = [], set()
    for pattern in patterns:
        for path in glob.glob(os.path.join(out, pattern), recursive=True):
            if not os.path.isfile(path) or path in seen:
                continue
            seen.add(path)
            candidates.append(path)
    artifacts, truncated, skipped = [], [], 0
    for path in sorted(candidates):
        rel = os.path.relpath(path, out).replace(os.sep, "/")
        if len(artifacts) >= MAX_ARTIFACT_COUNT:
            skipped += 1
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        was_truncated = False
        if size > MAX_ARTIFACT_BYTES:
            original = tail_truncate(path, MAX_ARTIFACT_BYTES)
            truncated.append("%s（%d 字节 → 保留末尾 %d 字节）" % (rel, original, MAX_ARTIFACT_BYTES))
            size = os.path.getsize(path)
            was_truncated = True
        entry = {"name": os.path.basename(path), "path": rel, "size_bytes": size,
                 "sha256": sha256_of(path)}
        if was_truncated:
            entry["truncated"] = True
        artifacts.append(entry)
    return artifacts, truncated, skipped


def env(name, default=""):
    value = os.environ.get(name)
    return default if value is None else value


def env_int(name, default=0):
    try:
        return int(str(env(name, "")).strip())
    except ValueError:
        return default


def env_float(name, default=0.0):
    try:
        return float(str(env(name, "")).strip())
    except ValueError:
        return default


def main(argv=None):
    parser = argparse.ArgumentParser(description="aisbench-prefix sidecar → result.json")
    parser.add_argument("--out", default=env("AG_OUTPUT_DIR", "/workspace/output"))
    parser.add_argument("--write", default="1", help="1=写 result.json，0=只打印摘要")
    args = parser.parse_args(argv)

    out = args.out
    points_root = os.path.join(out, "prefix_out", "points")
    events = read_events(points_root)

    status = env("AG_STATUS", "failed") or "failed"
    code = env("AG_CODE").strip()
    reason = env("AG_REASON").strip()
    extra_note = env("AG_EXTRA_NOTE").strip()
    retryable = env("AG_RETRYABLE", "false").strip() == "true"
    tool_rc = env("AG_RC").strip()

    configs = [r for _p, rows in events for r in rows if r.get("kind") == "config"]
    config = configs[0] if configs else {}

    metrics, missing, point_rows = build_metrics(events)

    # 成功但一条指标都没有 = 结果不可用（§5.8：执行完成要有可消费的结果）
    if status == "succeeded" and not metrics:
        status, code = "failed", "RESULT_INVALID"
        reason = reason or "工具退出码 0 但没汇出任何指标（sidecar 里没有 metrics 事件）"

    artifacts, truncated, skipped = collect_artifacts(out)

    notes = [item for item in (reason, extra_note) if item]
    if configs:
        notes.append("口径：target=%s model=%s input_len=%s output_len=%s data_num=%s "
                     "repeat_rate=%s prefix_num=%s dp=%s seed=%s dataset_type=%s"
                     % (config.get("target"), config.get("model_name"),
                        config.get("input_len"), config.get("output_len"),
                        config.get("data_num"), config.get("repeat_rate"),
                        config.get("prefix_num"), config.get("dp"), config.get("seed"),
                        config.get("dataset_type")))
    else:
        missing.append("config（sidecar 里没有 config 事件：工具在写事件之前就退出了）")
    if point_rows:
        notes.append("探测点：" + "，".join(
            "%s(%s,batch=%s,%d 项)" % (row["point"], row["phase"], row["concurrency"],
                                       row["metrics"]) for row in point_rows))
    if skipped:
        notes.append(f"产物条目超过 {MAX_ARTIFACT_COUNT} 条，已省略 {skipped} 个文件"
                     f"（完整内容仍在产物目录里，只是不再逐个列进 artifacts）")
    if truncated:
        notes.append("产物超 50MB 已截断（保留末尾）：" + "；".join(truncated))
    if missing:
        notes.append("未写入的指标：" + "；".join(missing))
    notes_text = "；".join(n for n in notes if n)
    if len(notes_text) > 4000:
        notes_text = notes_text[:3999] + "…"

    workload = {}
    for key in ("input_len", "output_len", "data_num", "request_rate", "model_name",
                "model_path", "dataset_type", "prefix_num", "repeat_rate", "seed", "dp",
                "dataset", "dataset_path", "work_path", "summarizer", "test_type",
                "npu_num", "length_mean", "length_std", "length_min", "length_max"):
        if config.get(key) not in (None, "", "none"):
            workload[key] = config[key]
    # env/argv 传下来都是字符串，报告里按数值看更顺手（且与参数 schema 的声明一致）
    for key in ("input_len", "output_len", "data_num", "prefix_num", "seed", "dp",
                "repeat", "npu_num", "length_mean", "length_min", "length_max"):
        value = workload.get(key)
        if isinstance(value, str) and value.replace(".", "", 1).isdigit():
            workload[key] = int(float(value))
    rate = workload.get("request_rate")
    if isinstance(rate, str):
        try:
            workload["request_rate"] = float(rate)
        except ValueError:
            pass
    workloads = env("AG_WORKLOAD_JSON").strip()
    if workloads:
        try:
            workload = {**json.loads(workloads), **workload}
        except ValueError:
            pass
    if point_rows:
        workload["concurrency"] = point_rows[-1]["concurrency"]
    # base_url 给**干净的服务地址**；target 里带的是 "(url) / host_ip" 这类寻址模式说明，
    # 那是给人看的，放 notes 就够了。
    workload["base_url"] = config.get("base_url") or config.get("target", "")

    error_obj = None
    if status != "succeeded":
        error_obj = {
            "code": code or "RUNNER_NONZERO_EXIT",
            "message": (reason or "aisbench-prefix 执行失败")[:1000],
            "detail": {
                "tool_exit_code": int(tool_rc) if tool_rc.lstrip("-").isdigit() else tool_rc,
                "points": point_rows,
                "sidecar": "prefix_out/points/point-*/events.jsonl",
            },
            "retryable": retryable,
        }

    payload = {
        "schema_version": "2.0",
        "task_id": env("AG_TASK_ID", "local") or "local",
        "attempt_no": env_int("AG_ATTEMPT", 1) or 1,
        "runner_seq": env_int("AG_RUNNER_SEQ", 1) or 1,
        "runner": {"name": "aisbench-prefix",
                   "version": env("AG_RUNNER_VERSION", "0.1.0"),
                   "image_ref": env("AG_RUNNER_IMAGE", "unknown")},
        "status": status,
        "started_at": env("AG_STARTED"),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_ms": max(0, int((time.time() - (env_float("AG_START_EPOCH")
                                                  or time.time())) * 1000)),
        "workload": workload,
        "metrics": metrics,
        "error": error_obj,
        "artifacts": artifacts,
        "notes": notes_text,
        "metadata": {
            "tool": "aisbench-prefix",
            "tool_version": env("AG_TOOL_VERSION", "0.1.0"),
            "tool_source": "vendored aisbench_auto_tools_prefix@"
                           "d9c24f3822dfdcf01545388561d6138e059851e4 + 本地改动"
                           "（见 tool/VENDORED.md）",
            "aisbench_version": env("AG_AISBENCH_VERSION", "unknown"),
            "output_dir": "prefix_out",
        },
    }

    if args.write == "0":
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    result = os.path.join(out, "result.json")
    tmp = result + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, result)
    print("[runner] result.json: status=%s metrics=%d artifacts=%d"
          % (status, len(metrics), len(artifacts)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

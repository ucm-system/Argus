#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`summarize.py`：sidecar 事件 → result.json 的映射（离线，不需要镜像）。

重点锁四件事：
1. 指标名**不编码维度**（附录 A / openapi ResultMetric）：统计量走 `statistic`、
   单位走 `unit`、并发与阶段走 `labels`；
2. `DEFAULT_METRICS` 的哨兵值（99999 / 9999）= 「没解析到」，绝不能当成测量值写出去，
   要落到 `notes` 的「未写入的指标」里；
3. 命中率按 `ALL_PODS` 差分给出，hbm 与 external 用 `labels.scope` 区分；
4. 退出码 0 但一条指标都没有 ⇒ `RESULT_INVALID`（§5.8「执行完成」要有可消费的结果）。

跑法：`py -3.11 runners/aisbench-prefix/tests/test_summarize.py`
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER_DIR = os.path.dirname(HERE)
SUMMARIZE = os.path.join(RUNNER_DIR, "summarize.py")

FULL_METRICS = {
    "input_len": 2048, "output_len": 1024, "total_req": 64, "max_cc": 8, "cc": 7.6,
    "rr": 0, "repeat_rate": 0.9,
    "TTFT avg": 120.5, "TTFT P90": 310.25, "TPOT avg": 8.4, "TPOT SLO_P90": 15.75,
    "E2E_time": 99999, "E2EL avg": 980.0, "E2EL P90": 1500.0,
    "output_throughput": 512.25, "single_output_throughput": 64.03,
    "E2E_throughput": 1580.0, "single_E2E_throughput": 197.5,
    "qps": 0.5, "qpm": 30.0, "input_token_throughput": 1024.5,
    "prefill_token_throughput": 9999,          # 哨兵：= 没解析到
}

CONFIG_EVENT = {
    "kind": "config", "input_len": 2048, "output_len": 1024, "data_num": 64,
    "request_rate": 0, "target": "http://mock:18080/v1（url）", "model_name": "mock-llm",
    "model_path": "/models/mock", "dataset_type": "prefix_cache", "prefix_num": 1,
    "repeat_rate": 0.9, "seed": 1, "dp": 1, "dataset": "none",
    "dataset_path": "/tmp/ds/c8", "work_path": "/tmp/prefix-ws",
    "summarizer": "default_perf", "test_type": "stream", "npu_num": 8,
}

HIT_EVENT = {
    "kind": "hit", "phase": "full", "concurrency": "8",
    "rows": [
        {"scope": "10.0.0.1:18080/engine0", "hbm_hits": 900, "hbm_queries": 1000,
         "external_hits": 10, "external_queries": 100},
        {"scope": "ALL_PODS", "hbm_hits": 900, "hbm_queries": 1000,
         "external_hits": 20, "external_queries": 200},
    ],
    "skipped_pods": [], "reason": "",
}


def write_events(root, point, events):
    directory = os.path.join(root, "prefix_out", "points", "point-%s" % point)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "events.jsonl"), "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    # 两个产物文件，验证 artifacts 的相对路径与大小
    with open(os.path.join(directory, "aisbench.log"), "w", encoding="utf-8") as handle:
        handle.write("│ TTFT │ total │ 1.00 ms │\n")
    return directory


def run_summarize(root, env_extra=None):
    env = dict(os.environ)
    env.update({"AG_TASK_ID": "t-test", "AG_ATTEMPT": "3", "AG_RUNNER_SEQ": "2",
                "AG_STATUS": "succeeded", "AG_CODE": "", "AG_REASON": "",
                "AG_RC": "0", "AG_RETRYABLE": "false", "AG_EXTRA_NOTE": "",
                "AG_RUNNER_VERSION": "0.1.0", "AG_RUNNER_IMAGE": "img:0.1.0",
                "AG_TOOL_VERSION": "0.1.0"})
    env.update(env_extra or {})
    result = subprocess.run([sys.executable, SUMMARIZE, "--out", root],
                            capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    with open(os.path.join(root, "result.json"), "r", encoding="utf-8") as handle:
        return json.load(handle)


class SummarizeTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="prefix-sum-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def metrics_by(self, data, name, **labels):
        out = []
        for metric in data["metrics"]:
            if metric["name"] != name:
                continue
            if all(metric.get("labels", {}).get(k) == v for k, v in labels.items()):
                out.append(metric)
        return out

    # ------------------------------------------------------------------ 用例
    def test_happy_path_maps_metrics(self):
        write_events(self.root, "8", [
            dict(CONFIG_EVENT),
            {"kind": "metrics", "phase": "prefix", "concurrency": "1",
             "values": dict(FULL_METRICS, max_cc=1, cc=1.0)},
            HIT_EVENT,
            {"kind": "metrics", "phase": "full", "concurrency": "8",
             "values": FULL_METRICS},
        ])
        data = run_summarize(self.root)
        self.assertEqual(data["status"], "succeeded")
        self.assertEqual(data["schema_version"], "2.0")
        self.assertEqual(data["task_id"], "t-test")
        self.assertEqual(data["attempt_no"], 3)
        self.assertEqual(data["runner"]["name"], "aisbench-prefix")
        self.assertIsNone(data["error"])

        ttft = self.metrics_by(data, "ttft", concurrency="8", phase="full")
        stats = {item["statistic"]: item for item in ttft}
        self.assertEqual(stats["mean"]["value"], 120.5)
        self.assertEqual(stats["mean"]["unit"], "ms")
        self.assertEqual(stats["p90"]["value"], 310.25)
        self.assertEqual(stats["mean"]["sample_size"], 64)

        throughput = self.metrics_by(data, "output_throughput", concurrency="8",
                                     phase="full", scope="per_device")
        self.assertEqual(throughput[0]["value"], 64.03)

        # 名字不编码维度：同一个 name 靠 labels 区分并发/阶段
        names = {m["name"] for m in data["metrics"]}
        self.assertNotIn("ttft_p90_ms", names)
        self.assertIn("measured_concurrency", names)

        hits = self.metrics_by(data, "hit_rate", phase="full")
        scopes = {item["labels"]["scope"]: item["value"] for item in hits}
        self.assertEqual(scopes["hbm"], 90.0)         # 900 / 1000
        self.assertEqual(scopes["external"], 10.0)    # 20 / 200

        self.assertIn("prefill_token_throughput", str(data["notes"]))

    def test_sentinels_are_not_reported_as_measurements(self):
        """全是哨兵值 ⇒ 一条指标都不写，并在 notes 说明。"""
        write_events(self.root, "8", [
            dict(CONFIG_EVENT),
            {"kind": "metrics", "phase": "full", "concurrency": "8",
             "values": {"input_len": 99999, "output_len": 99999, "total_req": 99999,
                        "max_cc": 99999, "cc": 99999, "TTFT avg": 99999,
                        "E2EL avg": 9999, "qps": 9999,
                        "output_throughput": 99999, "single_output_throughput": 99999,
                        "input_token_throughput": 9999, "prefill_token_throughput": 9999,
                        "E2E_throughput": 9999, "single_E2E_throughput": 9999}},
        ])
        data = run_summarize(self.root)
        self.assertEqual(data["metrics"], [])
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["error"]["code"], "RESULT_INVALID")
        self.assertIn("没解析到任何性能统计量", data["notes"])

    def test_no_events_is_result_invalid(self):
        os.makedirs(os.path.join(self.root, "prefix_out", "points"), exist_ok=True)
        data = run_summarize(self.root, {"AG_STATUS": "succeeded"})
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["error"]["code"], "RESULT_INVALID")

    def test_failed_status_keeps_error_object(self):
        write_events(self.root, "8", [dict(CONFIG_EVENT),
                                      {"kind": "metrics", "phase": "full",
                                       "concurrency": "8", "values": FULL_METRICS}])
        data = run_summarize(self.root, {"AG_STATUS": "failed",
                                         "AG_CODE": "RUNNER_NONZERO_EXIT",
                                         "AG_REASON": "2 个并发点全部失败",
                                         "AG_RC": "4"})
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["error"]["code"], "RUNNER_NONZERO_EXIT")
        self.assertEqual(data["error"]["message"], "2 个并发点全部失败")
        self.assertEqual(data["error"]["detail"]["tool_exit_code"], 4)

    def test_hit_rate_absent_is_explained(self):
        write_events(self.root, "8", [
            dict(CONFIG_EVENT),
            {"kind": "metrics", "phase": "full", "concurrency": "8", "values": FULL_METRICS},
            {"kind": "hit", "phase": "full", "concurrency": "8", "rows": [],
             "skipped_pods": ["10.0.0.1:18080"], "reason": "快照不完整"},
        ])
        data = run_summarize(self.root)
        self.assertEqual(self.metrics_by(data, "hit_rate"), [])
        self.assertIn("hit_rate", data["notes"])

    def test_artifacts_are_relative_and_hashed(self):
        directory = write_events(self.root, "8", [dict(CONFIG_EVENT)])
        data = run_summarize(self.root)
        paths = {item["path"] for item in data["artifacts"]}
        self.assertIn("prefix_out/points/point-8/events.jsonl", paths)
        self.assertIn("prefix_out/points/point-8/aisbench.log", paths)
        for item in data["artifacts"]:
            self.assertFalse(os.path.isabs(item["path"]))
            self.assertEqual(len(item["sha256"]), 64)
            self.assertGreater(item["size_bytes"], 0)
        self.assertTrue(os.path.isabs(directory))

    def test_workload_keeps_common_params(self):
        write_events(self.root, "8", [dict(CONFIG_EVENT),
                                      {"kind": "metrics", "phase": "full",
                                       "concurrency": "8", "values": FULL_METRICS}])
        data = run_summarize(self.root)
        workload = data["workload"]
        self.assertEqual(workload["concurrency"], "8")
        self.assertEqual(workload["model_path"], "/models/mock")
        self.assertEqual(workload["seed"], 1)
        self.assertNotIn("dataset", workload)      # "none" 不进 workload


if __name__ == "__main__":
    unittest.main(verbosity=2)

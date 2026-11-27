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
    "current_time": "2026-11-27 12:00:00",
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
    "external_available": True,
    "rows": [
        {"scope": "10.0.0.1:18080/engine0", "hbm_hits": 900, "hbm_queries": 1000,
         "external_hits": 10, "external_queries": 100, "external_available": True},
        {"scope": "ALL_PODS", "hbm_hits": 900, "hbm_queries": 1000,
         "external_hits": 20, "external_queries": 200, "external_available": True},
    ],
    "skipped_pods": [], "reason": "",
}

# vLLM 默认只有 vllm:prefix_cache_*（external_* 是连接器才有的）——kind 真跑就是这个形态：
# 上游会因为 8 个字典不齐而把整张命中率表丢掉，现在只丢 external 那一列。
HIT_EVENT_HBM_ONLY = {
    "kind": "hit", "phase": "full", "concurrency": "8", "external_available": False,
    "rows": [
        {"scope": "ALL_PODS", "hbm_hits": 500, "hbm_queries": 1000,
         "external_hits": 0, "external_queries": 0, "external_available": False},
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

    def test_hit_rate_hbm_only_when_external_missing(self):
        """只有 vllm:prefix_cache_* 的服务（kind 实测形态）：hbm 出数，external 说明而不是 0。"""
        import copy
        write_events(self.root, "8", [
            dict(CONFIG_EVENT),
            {"kind": "metrics", "phase": "full", "concurrency": "8", "values": FULL_METRICS},
            copy.deepcopy(HIT_EVENT_HBM_ONLY),
        ])
        data = run_summarize(self.root)
        hbm = self.metrics_by(data, "hit_rate", scope="hbm")
        ext = self.metrics_by(data, "hit_rate", scope="external")
        self.assertEqual(len(hbm), 1)
        self.assertEqual(hbm[0]["value"], 50.0)          # 500 / 1000
        self.assertEqual(ext, [], "external 没有计数器就不该编一个 0%")
        self.assertIn("external_prefix_cache", data["notes"])

    def test_hit_rate_per_point_in_sweep(self):
        """阶梯里每个并发点都要有自己的命中率（kind 实测发现只取了最后一点）。"""
        import copy
        write_events(self.root, "2", [
            dict(CONFIG_EVENT),
            {"kind": "metrics", "phase": "full", "concurrency": "2", "values": FULL_METRICS},
            dict(copy.deepcopy(HIT_EVENT_HBM_ONLY), concurrency="2"),
        ])
        write_events(self.root, "4", [
            dict(CONFIG_EVENT),
            {"kind": "metrics", "phase": "full", "concurrency": "4", "values": FULL_METRICS},
            dict(copy.deepcopy(HIT_EVENT_HBM_ONLY), concurrency="4"),
        ])
        data = run_summarize(self.root)
        hits = self.metrics_by(data, "hit_rate", scope="hbm")
        self.assertEqual(len(hits), 2, "两点阶梯应出两条命中率")
        self.assertEqual(sorted(str(item["labels"]["concurrency"]) for item in hits), ["2", "4"])

    def test_metric_keys_are_unique(self):
        """平台侧硬规则：`name+statistic+labels` 在同一 result.json 内必须唯一，
        否则整个 Task 判 RESULT_INVALID（kind 真跑就是这么翻车的）。
        两点阶梯的预热阶段并发相同（都= prefix_num），必须靠 labels.point 区分。
        """
        import copy
        import json
        from collections import Counter
        for point, conc in (("2", "2"), ("4", "4")):
            write_events(self.root, point, [
                dict(CONFIG_EVENT),
                {"kind": "metrics", "phase": "prefix", "concurrency": "1",
                 "values": dict(FULL_METRICS, max_cc=1, cc=1.0)},
                copy.deepcopy(HIT_EVENT_HBM_ONLY) | {"concurrency": conc},
                {"kind": "metrics", "phase": "full", "concurrency": conc,
                 "values": dict(FULL_METRICS, max_cc=int(conc))},
            ])
        data = run_summarize(self.root)
        keys = Counter((m["name"], m["statistic"],
                        json.dumps(m.get("labels") or {}, sort_keys=True, ensure_ascii=False))
                       for m in data["metrics"])
        dupes = [key for key, count in keys.items() if count > 1]
        self.assertEqual(dupes, [], f"有重复的指标键：{dupes[:2]}")
        self.assertGreater(len(data["metrics"]), 30)
        self.assertEqual(data["status"], "succeeded")

    def test_upstream_csv_is_aggregated(self):
        """观察面：输出根目录的 aisbench_result.csv 要保持**上游列序**，
        一行 = 一个并发点的一个阶段；哨兵值留空而不是照抄 99999。"""
        import copy
        import csv as csv_mod
        for point, conc in (("2", "2"), ("4", "4")):
            write_events(self.root, point, [
                dict(CONFIG_EVENT, model_path="/models/mini"),
                {"kind": "metrics", "phase": "prefix", "concurrency": "1",
                 "values": dict(FULL_METRICS, max_cc=1, cc=1.0,
                                **{"TTFT P90": 99999, "prefill_token_throughput": 9999})},
                copy.deepcopy(HIT_EVENT_HBM_ONLY) | {"concurrency": conc},
                {"kind": "metrics", "phase": "full", "concurrency": conc,
                 "values": dict(FULL_METRICS, max_cc=int(conc))},
            ])
        data = run_summarize(self.root)
        path = os.path.join(self.root, "aisbench_result.csv")
        self.assertTrue(os.path.isfile(path), "聚合 CSV 没写到输出根目录")
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv_mod.DictReader(handle))
        self.assertEqual(len(rows), 4, "2 点 × 2 阶段 = 4 行")
        # 列序 = 上游列序（前面只多 point/phase，后面只多两列命中率）
        self.assertEqual(rows[0]["model_name"], "mini")       # 上游规则：MODEL_PATH 的最后一段
        self.assertIn("TTFT avg", list(rows[0].keys()))
        self.assertEqual(float(rows[0]["TTFT avg"]), 120.5)
        # 阶段与点
        self.assertEqual([r["phase"] for r in rows], ["prefix", "full", "prefix", "full"])
        self.assertEqual([r["point"] for r in rows], ["2", "2", "4", "4"])
        # 哨兵值 → 留空（TTFT P90=99999 / prefill=9999 只出现在 prefix 行）
        self.assertEqual(rows[0]["TTFT P90"], "")
        self.assertEqual(rows[0]["prefill_token_throughput"], "")
        self.assertNotEqual(rows[1]["TTFT P90"], "")
        # 命中率列：prefix/full 都取各自阶段的快照
        self.assertEqual(float(rows[1]["hit_rate_hbm"]), 50.0)
        self.assertEqual(rows[1]["hit_rate_external"], "")   # 没有计数器 → 留空
        # artifacts 里列了这份 CSV
        self.assertIn("aisbench_result.csv", {a["path"] for a in data["artifacts"]})
        self.assertIn("aisbench_result.csv", data["notes"])

    def test_html_report_is_generated(self):
        """观察面：result.html —— 自包含对比报告（阶梯表 + 内联 SVG 趋势图）。
        多组数据的横向对比靠它；不引任何外部资源（产物会被下载到任意机器看）。"""
        import copy
        import re
        for point, conc in (("2", "2"), ("4", "4"), ("8", "8")):
            write_events(self.root, point, [
                dict(CONFIG_EVENT),
                {"kind": "metrics", "phase": "prefix", "concurrency": "1",
                 "values": dict(FULL_METRICS, max_cc=1, cc=1.0)},
                copy.deepcopy(HIT_EVENT_HBM_ONLY) | {"concurrency": conc},
                {"kind": "metrics", "phase": "full", "concurrency": conc,
                 "values": dict(FULL_METRICS, max_cc=int(conc),
                                **{"TTFT avg": 100.0 + int(conc) * 5,
                                   "output_throughput": 100.0 + int(conc) * 30})},
            ])
        data = run_summarize(self.root)
        path = os.path.join(self.root, "result.html")
        self.assertTrue(os.path.isfile(path), "result.html 没生成")
        doc = open(path, encoding="utf-8").read()
        # 自包含：不引外部资源（用户数据里的 URL 字符串不算——负载表本来就要显示被测地址）
        self.assertNotIn('src="http', doc)
        self.assertNotIn('href="http', doc)
        self.assertNotIn("url(", doc.replace("url（", ""))   # css @import/url() 也不允许
        self.assertNotIn("<script", doc)
        # 阶梯表 3 行（full）+ 图 2 张（TTFT/TPOT/吞吐 3 张里，TPOT full 行有值 → 也该有）
        self.assertEqual(len(re.findall(r'<h2>并发阶梯', doc)), 1)
        self.assertGreaterEqual(len(re.findall(r'<svg ', doc)), 3)
        # full 行的目标并发都在
        for conc in ("2", "4", "8"):
            self.assertIn(f"c={conc}", doc)
        # 命中率进表
        self.assertIn("命中率 HBM", doc)
        # 哨兵值（E2E_time=99999）→ 显示为 — 而非数字
        self.assertIn("—", doc)
        # artifacts 里列了 result.html
        self.assertIn("result.html", {a["path"] for a in data["artifacts"]})

    def test_workload_base_url_is_clean(self):
        write_events(self.root, "8", [
            dict(CONFIG_EVENT, base_url="http://10.0.0.9:8000/",
                 target="http://10.0.0.9:8000/（url）", output_len="16", seed="7"),
            {"kind": "metrics", "phase": "full", "concurrency": "8", "values": FULL_METRICS},
        ])
        data = run_summarize(self.root)
        self.assertEqual(data["workload"]["base_url"], "http://10.0.0.9:8000/")
        self.assertEqual(data["workload"]["output_len"], 16)
        self.assertEqual(data["workload"]["seed"], 7)

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

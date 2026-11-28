#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""report.py（SLA 阶梯 HTML 报告）+ run.sh 结果映射的离线测试。

不需要 docker / ais_bench：直接用 sla_summary.json 夹具跑 report.py，
断言报告内容；再用 run.sh 的 PYRESULT 逻辑等价物（fixtures → result.json 字段）
锁住映射口径。跑法：`py -3.11 runners/aisbench-sla/tests/test_report.py`。
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER_DIR = os.path.dirname(HERE)
REPORT = os.path.join(RUNNER_DIR, "report.py")

FIXTURE_SUMMARY = {
    "tool": "aisbench-sla 0.7.0",
    "status": "completed",
    "exit_code": 0,
    "note": "搜索收敛：二分 2 轮",
    "error": None,
    "max_ok_concurrency": 8,
    "max_ok_concurrency_measured": 4,
    "measured_concurrency_min_ratio": 0.8,
    "degraded_points": [{"concurrency": 8, "measured_concurrency": 5.5,
                         "measured_concurrency_ratio": 0.6875, "reason": "实测并发未达 80%"}],
    "sla": {"ttft_avg_ms": 1200, "output_token_throughput_avg": 500},
    "sla_labels": ["TTFT avg ≤ 1200ms", "输出吞吐 ≥ 500 tok/s"],
    "search": {"start": 1, "max": 16, "tolerance": 1, "max_probes": 20,
               "max_bisect": 5, "prefix_warmup": "per-point"},
    "target": {"base_url": "http://mock:18080", "model": "mock-llm", "pods": ["mock:18080"]},
    "workload": {"input_len": 2048, "output_len": 32, "data_num": 64,
                 "request_rate": 0, "prefix_num": 1, "repeat_rate": "90%",
                 "seed": 1, "dp": 1, "dataset_type": "prefix_cache"},
    "dataset": {"source": "generated", "path": "/tmp/ds"},
    "duration_s": 123.4,
    "warnings": ["request_rate=3 且目标并发高时可能压不满"],
    "throughput": {"peak": {"value": 512.3, "concurrency": 8},
                   "best_within_sla": {"value": 380.1, "concurrency": 4}},
    "points": [
        {"kind": "preflight", "concurrency": 1, "seed": 1, "output_len": 32,
         "verdict": "PASS", "degraded": False, "degraded_reason": None,
         "measured_concurrency_ratio": 1.0, "reason": "",
         "output_token_throughput": 41.5, "request_throughput_qps": 3.2,
         "request_rate": 0, "total_requests": 64,
         "ttft_avg_ms": 142.7, "ttft_p90_ms": 142.7,
         "tpot_avg_ms": -1, "tpot_p90_ms": -1,
         "e2el_avg_ms": 151.0, "e2el_p90_ms": 151.0,
         "measured_concurrency": 1.0, "hbm_hit_rate": 0.0, "ext_hit_rate": None,
         "hit_rate_note": "", "elapsed_s": 12.1, "log_path": "logs"},
        {"kind": "probe", "concurrency": 2, "seed": 2, "output_len": 32,
         "verdict": "PASS", "degraded": False, "degraded_reason": None,
         "measured_concurrency_ratio": 0.99, "reason": "",
         "output_token_throughput": 91.5, "request_throughput_qps": 5.7,
         "request_rate": 0, "total_requests": 64,
         "ttft_avg_ms": 194.9, "ttft_p90_ms": 215.7,
         "tpot_avg_ms": 10.0, "tpot_p90_ms": 11.1,
         "e2el_avg_ms": 344.9, "e2el_p90_ms": 381.9,
         "measured_concurrency": 1.97, "hbm_hit_rate": 0.427, "ext_hit_rate": None,
         "hit_rate_note": "", "elapsed_s": 30.5, "log_path": "logs"},
        {"kind": "probe", "concurrency": 4, "seed": 3, "output_len": 32,
         "verdict": "PASS", "degraded": False, "degraded_reason": None,
         "measured_concurrency_ratio": 0.93, "reason": "",
         "output_token_throughput": 195.1, "request_throughput_qps": 12.2,
         "request_rate": 0, "total_requests": 64,
         "ttft_avg_ms": 173.5, "ttft_p90_ms": 196.5,
         "tpot_avg_ms": 8.9, "tpot_p90_ms": 10.2,
         "e2el_avg_ms": 307.6, "e2el_p90_ms": 347.1,
         "measured_concurrency": 3.72, "hbm_hit_rate": 0.6611, "ext_hit_rate": None,
         "hit_rate_note": "", "elapsed_s": 45.2, "log_path": "logs"},
        {"kind": "probe", "concurrency": 8, "seed": 4, "output_len": 32,
         "verdict": "FAIL", "degraded": True, "degraded_reason": "实测并发未达 80%",
         "measured_concurrency_ratio": 0.6875, "reason": "tpot_p90_ms 超阈值",
         "output_token_throughput": 313.4, "request_throughput_qps": 20.1,
         "request_rate": 0, "total_requests": 64,
         "ttft_avg_ms": 201.1, "ttft_p90_ms": 240.0,
         "tpot_avg_ms": 12.4, "tpot_p90_ms": 18.9,
         "e2el_avg_ms": 402.0, "e2el_p90_ms": 460.0,
         "measured_concurrency": 5.5, "hbm_hit_rate": 0.75, "ext_hit_rate": None,
         "hit_rate_note": "", "elapsed_s": 60.0, "log_path": "logs"},
    ],
}


def run_report(out_dir, summary, env_extra=None):
    summary_path = os.path.join(out_dir, "sla_summary.json")
    with open(summary_path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False)
    env = dict(os.environ)
    env.update({
        "AG_STATUS": "succeeded", "AG_CODE": "", "AG_REASON": "",
        "AG_TASK_ID": "t-test", "AG_ATTEMPT": "2", "AG_RUNNER_SEQ": "3",
        "AG_RUNNER_VERSION": "0.6.0", "AG_DURATION_MS": "76825",
        "AG_OUTPUT_DIR": out_dir,
    })
    env.update(env_extra or {})
    result = subprocess.run([sys.executable, REPORT, "--summary", summary_path,
                             "--out", out_dir],
                            capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    return os.path.join(out_dir, "result.html")


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sla-report-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_ladder_report_content(self):
        path = run_report(self.root, FIXTURE_SUMMARY)
        self.assertTrue(os.path.isfile(path), "result.html 没生成")
        doc = io.open(path, encoding="utf-8").read()
        # 自包含：无外部资源引用（用户数据里的 URL 不算）
        self.assertNotIn('src="http', doc)
        self.assertNotIn('href="http', doc)
        self.assertNotIn("<script", doc)
        # 结论卡：配置口径 / 实测口径分开
        self.assertIn("max_ok_concurrency_measured", doc)
        # 阶梯表 4 行（含 FAIL 点 + 预检行），verdict 徽标
        self.assertEqual(len(re.findall(r'<span class="v-(?:pass|fail)">', doc)), 4)
        self.assertIn("降级", doc)
        # SLA 约束表
        self.assertIn("ttft_avg_ms", doc)
        self.assertIn("输出吞吐 ≥ 500 tok/s", doc)
        # 趋势图 ≥ 3 张（TTFT/TPOT/E2EL/吞吐/命中率）
        self.assertGreaterEqual(len(re.findall(r"<svg ", doc)), 3)
        # SLA 阈值虚线（吞吐图 500、延迟图 1200——_fmt 千分位逗号）
        self.assertIn("SLA 阈值 500", doc)
        self.assertIn("SLA 阈值 1,200", doc)
        # 高亮行：c=4 是实测口径的最大可行并发
        self.assertIn('class="chosen"', doc)
        # 命中率按百分数显示（0.427 → 42.7）
        self.assertIn("42.7", doc)
        # 哨兵 → —（preflight 行 tpot=-1）
        self.assertIn("—", doc)
        # 降级点单独列出
        self.assertIn("c=8", doc)

    def test_failed_status_renders(self):
        summary = dict(FIXTURE_SUMMARY, status="failed",
                       error="SLA 无法满足：预检不达标", max_ok_concurrency=None)
        path = run_report(self.root, summary, {"AG_STATUS": "failed",
                                               "AG_CODE": "SLA_PRECHECK_FAILED",
                                               "AG_REASON": "预检不达标"})
        doc = io.open(path, encoding="utf-8").read()
        self.assertIn('class="badge bad">failed', doc)
        self.assertIn("工具报错：SLA 无法满足", doc)

    def test_single_point_no_charts(self):
        summary = dict(FIXTURE_SUMMARY, points=[FIXTURE_SUMMARY["points"][0]])
        path = run_report(self.root, summary)
        doc = io.open(path, encoding="utf-8").read()
        self.assertNotIn("<svg ", doc)
        self.assertIn("探测点不足 2 个", doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)

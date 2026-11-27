#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`prefix_env.parse_target` 的离线单测（不需要镜像、不需要第三方依赖）。

跑法：`py -3.11 runners/aisbench-prefix/tests/test_prefix_env.py`
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tool"))

import prefix_env  # noqa: E402


class TargetTests(unittest.TestCase):
    def fields(self, text):
        return prefix_env.parse_target(text).model_fields()

    def test_bare_ipv4_uses_host_fields(self):
        """裸 IP 走 host_ip/host_port（最贴合 ais_bench 原生用法）。"""
        fields = self.fields("141.9.9.9:8004")
        self.assertEqual(fields["host_ip"], "141.9.9.9")
        self.assertEqual(fields["host_port"], 8004)
        self.assertEqual(fields["url"], "")

    def test_hostname_must_not_go_into_host_ip(self):
        """集群内 DNS 名会被 ais_bench 的 host_ip 校验拒掉 → 必须改走 url 字段。"""
        fields = self.fields("http://sla-mock.argus-mock.svc:18080/v1")
        self.assertEqual(fields["host_ip"], "localhost", "非 IP 时 host_ip 只能写 localhost")
        self.assertEqual(fields["host_port"], 18080)
        self.assertEqual(fields["url"], "http://sla-mock.argus-mock.svc:18080",
                         "url 必须是服务根：ais_bench 0630 会给 path 补尾斜杠，给 .../v1 就变双 /v1")

    def test_trailing_slash_is_dropped(self):
        """带尾斜杠时 urljoin 会变成 …/v1/v1/chat/completions（实测过）。"""
        target = prefix_env.parse_target("http://h:8000/")
        self.assertEqual(target.base_url(), "http://h:8000")
        self.assertFalse(target.base_url().endswith("/"))

    def test_path_prefix_keeps_trailing_slash(self):
        """非 /v1 的路径前缀必须以 '/' 结尾，否则 urljoin 会把前缀整个吃掉。"""
        from urllib.parse import urljoin
        target = prefix_env.parse_target("http://gw.internal/openai")
        self.assertTrue(target.base_url().endswith("/"))
        # base url 恒带显式端口（省得 ais_bench 那边再猜默认端口）
        self.assertEqual(urljoin(target.base_url(), "v1/chat/completions"),
                         "http://gw.internal:80/openai/v1/chat/completions")

    def test_chat_url_never_double_v1(self):
        """ais_bench 拼的是 urljoin(base, "v1/chat/completions")：各种写法都得对。

        回归的就是 kind 真跑踩到的那个坑：ais_bench 3.1.20260630 的
        `base_api._get_base_url()` 会给非空 path 补尾斜杠，`.../v1` 于是变成
        `.../v1/v1/chat/completions`（404 Not Found）。所以 url 字段给**服务根**。
        """
        from urllib.parse import urljoin
        expected = "http://h:8000/v1/chat/completions"
        for base in ("http://h:8000", "http://h:8000/", "http://h:8000/v1",
                     "http://h:8000/v1/", "h:8000/v1", "http://h:8000/openai/v1"):
            target = prefix_env.parse_target(base)
            joined = urljoin(target.base_url(), "v1/chat/completions")
            if "openai" in base:
                self.assertEqual(joined, "http://h:8000/openai/v1/chat/completions",
                                 f"网关前缀场景 base={base}")
            else:
                self.assertEqual(joined, expected, f"base={base} 拼出的接口不对")

    def test_openai_version_segment_is_stripped(self):
        self.assertEqual(prefix_env.parse_target("http://h:8000/v1").base_url(), "http://h:8000")
        self.assertEqual(prefix_env.parse_target("http://h:8000/v1/").base_url(), "http://h:8000")
        # /metrics 的 host:port 不受影响
        self.assertEqual(prefix_env.parse_target("http://h:8000/v1").metrics_endpoint(), "h:8000")

    def test_ipv6_bracketed(self):
        target = prefix_env.parse_target("http://[::1]:8000/v1")
        self.assertEqual(target.host, "::1")
        self.assertTrue(target.is_ip)
        self.assertEqual(target.base_url(), "http://[::1]:8000")
        self.assertEqual(target.metrics_endpoint(), "[::1]:8000")

    def test_default_port_follows_scheme(self):
        self.assertEqual(prefix_env.parse_target("http://svc").metrics_endpoint(), "svc:80")
        self.assertEqual(prefix_env.parse_target("https://svc").metrics_endpoint(), "svc:443")

    def test_metrics_endpoint_uses_real_host_not_localhost(self):
        """/metrics 是普通 HTTP，DNS 名合法——不能被 host_ip 的规避值污染。"""
        target = prefix_env.parse_target("http://vllm.ns.svc:8000/v1")
        self.assertEqual(target.model_fields()["host_ip"], "localhost")
        self.assertEqual(target.metrics_endpoint(), "vllm.ns.svc:8000")

    def test_bad_port_rejected(self):
        with self.assertRaises(ValueError):
            prefix_env.parse_target("http://h:70000/v1")

    def test_sidecar_disabled_without_env(self):
        os.environ.pop("AGP_EVENTS", None)
        prefix_env.emit("metrics", values={"x": 1})  # 不该抛、也不该落盘
        self.assertEqual(prefix_env.env("AGP_EVENTS"), "")

    def test_reset_pick_state(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["AGP_STATE_DIR"] = tmp
            path = prefix_env.picked_ids_path()
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("1\n2\n")
            self.assertEqual(prefix_env.reset_pick_state(), [path])
            self.assertFalse(os.path.exists(path))
            os.environ["AGP_RESET_PICK_STATE"] = "0"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("3\n")
            self.assertEqual(prefix_env.reset_pick_state(), [], "关掉开关就不该删")
            del os.environ["AGP_RESET_PICK_STATE"]
            del os.environ["AGP_STATE_DIR"]


if __name__ == "__main__":
    unittest.main(verbosity=2)

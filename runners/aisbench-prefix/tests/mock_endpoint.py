#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线冒烟用的假推理服务端（**测试夹具**）。

只提供本 Runner 链路真正会打的三个接口：

* `GET  /v1/models`            → {"data":[{"id": <model>}]}（run.sh 的可达性预检 + 探活）
* `POST /v1/chat/completions`  → 一个能过 OpenAI 客户端形状的最短响应
* `GET  /metrics`              → vllm 风格的前缀缓存计数器（**累计值**）
* `POST /__sim__/traffic?queries&hits&ext_queries&ext_hits` → 累加计数器
  （由 tests/stub_aisbench.py 在每次「压测」后推送，模拟真实跑完后的增量）

计数器必须**单调累计**：vendored 工具算命中率靠「测后快照 − 测前快照」，
给成固定值就永远差分不出东西（或者差出 0）。标签形状也要贴合
`cal_prefix_hit_rate._get_prefix_cache_metrics()` 的过滤条件：
一行里同时含 `model_name` 与 `prefix_cache_{queries,hits}_total`，且带 `engine="N"`。

用法：`python3 mock_endpoint.py --port 18080 --model mock-llm [--log /tmp/mock.log]`
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

LOCK = threading.Lock()
COUNTERS = {
    "vllm:prefix_cache_queries_total": 0,
    "vllm:prefix_cache_hits_total": 0,
    "vllm:external_prefix_cache_queries_total": 0,
    "vllm:external_prefix_cache_hits_total": 0,
}
STATS = {"requests": 0, "started": time.time()}
MODEL = "mock-llm"
ENGINE = 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 安静：默认会往 stderr 刷一行/请求
        sys.stderr.write("[mock] %s\n" % (fmt % args))

    def _send(self, status, body, content_type="application/json"):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/v1/models":
            self._send(200, json.dumps({"object": "list",
                                        "data": [{"id": MODEL, "object": "model"}]}))
            return
        if path == "/metrics":
            with LOCK:
                lines = []
                for name, value in COUNTERS.items():
                    lines.append(f"# TYPE {name} counter")
                    lines.append(f'{name}{{model_name="{MODEL}",engine="{ENGINE}"}} {value}')
                lines.append(f'# HELP stub_requests_total total chat requests seen')
                lines.append(f'stub_requests_total{{model_name="{MODEL}",engine="{ENGINE}"}} '
                             f'{STATS["requests"]}')
                body = "\n".join(lines) + "\n"
            self._send(200, body, "text/plain; version=0.0.4")
            return
        if path == "/__sim__/state":
            with LOCK:
                self._send(200, json.dumps({"counters": COUNTERS, "stats": STATS}))
            return
        self._send(404, json.dumps({"error": "not found"}))

    # ----------------------------------------------------------------- POST
    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        # 允许带路径前缀的部署：模型配置里的 url 常是 …/v1，桩拼出的就是
        # /v1/__sim__/traffic；按后缀匹配省得再猜 base。
        if parsed.path.endswith("/__sim__/traffic"):
            query = parse_qs(parsed.query)

            def num(key):
                try:
                    return int(float(query[key][0]))
                except (KeyError, IndexError, ValueError):
                    return 0

            with LOCK:
                COUNTERS["vllm:prefix_cache_queries_total"] += num("queries")
                COUNTERS["vllm:prefix_cache_hits_total"] += num("hits")
                COUNTERS["vllm:external_prefix_cache_queries_total"] += num("ext_queries")
                COUNTERS["vllm:external_prefix_cache_hits_total"] += num("ext_hits")
                snapshot = dict(COUNTERS)
            self._send(200, json.dumps(snapshot))
            return
        body = {}
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            pass
        if parsed.path.endswith("/chat/completions"):
            with LOCK:
                STATS["requests"] += 1
            prompt = ""
            for message in body.get("messages") or []:
                prompt += str(message.get("content") or "")
            completion = "42"
            usage = {"prompt_tokens": len(prompt), "completion_tokens": len(completion),
                     "total_tokens": len(prompt) + len(completion)}
            self._send(200, json.dumps({
                "id": "stub-%d" % STATS["requests"], "object": "chat.completion",
                "model": body.get("model", MODEL),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": completion}}],
                "usage": usage}))
            return
        self._send(404, json.dumps({"error": "not found"}))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--model", default="mock-llm")
    args = parser.parse_args(argv)
    global MODEL
    MODEL = re.sub(r"[\"\\\\]", "", args.model)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[mock] listening on http://{args.host}:{args.port} model={MODEL}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

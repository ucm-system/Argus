# -*- coding: utf-8 -*-
# 【本文件是 Runner 侧改写版】上游 config.py 的每一项都在这里，但**都可以用环境变量覆盖**：
# Runner 跑在只读根文件系统的容器里（§6.8），不允许「先编辑 config.py 再跑命令」这种用法；
# 平台注入的公共参数（AG_SERVED_URL / AG_SERVED_MODEL_NAME / AG_MODEL_PATH，§5.4.1）与
# Task 级 secrets（ENDPOINT_API_KEY，§12.4）也只能走环境变量。
# 逐条改动见 ../tool/VENDORED.md（本文件在 vendored 目录内，路径为 tool/config.py）。
import os

import prefix_env


def _env(name, default=""):
    return prefix_env.env(name, default)


# 数据集文件夹路径，需可访问(请使用绝对路径)
# Runner 里由 run.sh 指到 $AG_OUTPUT_DIR/prefix_out/datasets（产物目录，可写、会被平台收集）
DATASET_PATH = _env("AGP_DATASET_PATH", "/home/dataset")

# aisbench 工作路径, 为 git clone aisbench 后得到的 benchmark 目录的绝对路径
# 可通过命令 `pip show ais-bench-benchmark` 查询location
# Runner 里默认 /tmp/prefix-ws：平台根文件系统只读，写不进镜像里的 /benchmark，
# run.sh 会用 AIS_BENCH_DATASETS_CACHE + --config-dir 把这个「影子源码根」接给 ais_bench。
WORK_PATH = _env("AGP_WORK_PATH", "/tmp/prefix-ws")

# 服务化配置的模型名称（公共参数 AG_SERVED_MODEL_NAME）
MODEL_NAME = _env("AGP_MODEL_NAME", "ds")
# 模型权重路径, 用于读取 tokenizer（公共参数 AG_MODEL_PATH，由平台统一共享挂载提供，D-63）
# 缺省给 /models（镜像里建好了）：ais_bench 校验 `path` 必须存在，上游那个
# /home/weights/model_weights 占位在容器里必然 UTILS-CFG-002。
MODEL_PATH = _env("AGP_MODEL_PATH", "/models")

# 目标服务：AGP_URL 优先（平台注入的 AG_SERVED_URL 原样给出，形如 http://host:port/v1）。
# 上游只认 HOST_IP/HOST_PORT，而 ais_bench 校验 host_ip 必须是合法 IP —— 集群内 DNS 名会被拒，
# 所以这里按 URL 归一化：裸 IP 写 host_ip/host_port，其余写 url 字段并把 host_ip 置 localhost。
_TARGET = prefix_env.resolve_target()
_HOST_FIELDS = _TARGET.model_fields()

# 请求目的 IP
HOST_IP = _env("AGP_HOST_IP", str(_HOST_FIELDS["host_ip"]))
# 请求目的端口
HOST_PORT = _env("AGP_HOST_PORT", str(_HOST_FIELDS["host_port"]))

# 模型baseUrl, 配置后会忽略HOST_IP、HOST_PORT配置
URL = _HOST_FIELDS["url"]

## 鉴权信息
# 凭据**不从这里进**：§12.4 要求密钥只走环境变量，且模型配置落盘时也不能出现明文。
# 生成的 temp_api.py 写的是 `api_key=_API_KEY`（读 ENDPOINT_API_KEY 的表达式），
# 所以 API_KEY 只为兼容上游用法保留，Runner 路径上不生效。
API_KEY = ""
ENDPOINT_API_KEY = _env("ENDPOINT_API_KEY")

# 如果使用稳态测试请将该字段设置为 "stable_stage"
DEFAULT_PERFORMANCE_TEST = _env("AGP_SUMMARIZER", "default_perf")

# aisbench输出日志保存路径（run.sh 指到 $AG_OUTPUT_DIR/prefix_out/points/point-<c>/work）
OUTPUT_DIR = _env("AGP_OUTPUT_DIR", "./outputs/default")

# 各节点信息，格式为 ["{ip}:{port}"]
# 用于查询vllm metrics计算各个dp域的prefix cache命中率，不配置默认为目标服务的 host:port
# PD分离场景请填写各个P节点的IP和对应dp域的port（AGP_POD_INFO="ip:port,ip:port"）
# 注意默认值用的是**URL 里的真实 host:port**，不是被规避成 localhost 的 HOST_IP：
# 命中率走 /metrics（普通 HTTP，主机名合法），不受 host_ip 校验影响。
POD_INFO = [item.strip() for item in _env("AGP_POD_INFO").split(",") if item.strip()]
POD_INFO_DEFAULT = _TARGET.metrics_endpoint()

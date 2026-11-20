# Argus Runner: agentx —— 镜像构建定义
#
# 只装「跑 profile」需要的东西：agentx-harness（含 agentx/aiperf 命令）+ python3 运行时。
# 平台调用唯一入口 /runner/run.sh（runner.yaml 的 entry 数组指向它，ARCHITECTURE §5.2）。
#
# 构建上下文 = Runner 目录（不是仓库根）：
#   cd runners/agentx
#   TAG=1.0.0
#   docker build -f build/Dockerfile -t registry.dev.huawei.com/argus/agentx:${TAG} .
#   docker push registry.dev.huawei.com/argus/agentx:${TAG}
# 构建由 CI 执行（§6.2，release.yml 自动发现 runners/*/build/Dockerfile）；平台不 build。
#
# 网络受限环境（国内/内网）用两个 build-arg，不要把镜像源写死进 Dockerfile：
#   --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.13-slim
#   --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
#
# agentx-harness 版本用 --build-arg AGENTX_VERSION= 覆盖（默认 1.0.14）。
# 体积：完整安装约 1.8GB。profile 路径从不 import 绘图/调参栈，所以把它们在**同一层**
# 卸掉（跨层删除只是遮蔽，镜像不会变小）——落到约 1.0GB。需要 agentx plot / sweep 的场景
# 请用 --build-arg PRUNE= 关掉裁剪。
ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

ARG AGENTX_VERSION=1.0.14
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PRUNE="plotly dash dash-bootstrap-components seaborn matplotlib kaleido fonttools optuna sqlalchemy alembic"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    HOME=/tmp \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken_cache

# PSA restricted（runAsNonRoot + seccomp + caps drop）：镜像必须非 root 运行。
RUN set -eux; \
    addgroup --gid 10001 runner; \
    adduser --uid 10001 --gid 10001 --disabled-password --gecos "" runner; \
    mkdir -p /workspace/input /workspace/output /runner /opt/tiktoken_cache; \
    chown -R 10001:10001 /workspace /runner /opt/tiktoken_cache

# 依赖安装 + 裁剪 + 词表预缓存放在同一层：删除的文件不会留在下层。
#
# tiktoken 预缓存只服务 `AG_MODEL_PATH=builtin`（Runner 把它原样交给 --tokenizer，下载 o200k_base 词表）：
#   * 构建机连不上 openaipublic.blob.core.windows.net 时**不会让构建失败**，只打警告——
#     生产上通常注入模型权重目录（`AG_MODEL_PATH=/models/Qwen3-32B` → `--tokenizer /models/Qwen3-32B`），
#     根本不需要它；
#   * 想彻底跳过这次下载：`--build-arg TIKTOKEN_CACHE=0`（离线/可复现构建推荐）。
ARG TIKTOKEN_CACHE=1

# aiperf server-metrics /v1 修复补丁（上游 agentx-harness 在 agentx-v1.0.14 出的）。
#
# 问题：`--url` 给的是 OpenAI 风格 base（`http://host:8000/v1`）时，aiperf 的自动发现拼出
#   `http://host:8000/v1/metrics` → 404 → 日志只有 "no Prometheus endpoints reachable"，
#   server metrics 静默关闭，实际拿不到指标。
# 补丁做两件事：1) 归一化时丢掉尾部 API 版本段（`/v1`、`/v2`…），serving prefix 保留；
#              2) 推导路径仍非 200 时回退探测一次 origin-root `/metrics`，并把 HTTP 状态
#                 写进 disabled 原因（`<url> (HTTP 404)`）。
#
# 补丁按 **上游仓库布局**（`a/src/aiperf/...`）编写，而 wheel 里只有 `src/` 的内容，
# 且 docs/ 与 tests/ 不进镜像——所以先用 awk 抽出 `src/aiperf/` 的 hunk，再 `patch -p2`
# 落到 site-packages。补丁打不上会让构建**失败**（含 ``*.orig/*.rej`` 残留检查与行为断言）。
#
# agentx-harness 升到已内置该修复的版本后，用 `--build-arg APPLY_METRICS_PATCH=0` 关掉。
ARG APPLY_METRICS_PATCH=1
COPY build/aiperf-server-metrics-v1-url-fix.patch /tmp/

RUN set -eux; \
    pip install --index-url "${PIP_INDEX_URL}" "agentx-harness==${AGENTX_VERSION}"; \
    SP=/usr/local/lib/python3.13/site-packages; \
    if [ "${APPLY_METRICS_PATCH}" = "1" ]; then \
      apt-get update; \
      apt-get install -y --no-install-recommends patch; \
      awk '/^diff --git /{ keep = ($0 ~ /^diff --git a\/src\/aiperf\//) } keep' \
          /tmp/aiperf-server-metrics-v1-url-fix.patch > /tmp/aiperf-metrics-src.patch; \
      test -s /tmp/aiperf-metrics-src.patch; \
      patch -p2 -t -N --no-backup-if-mismatch -d "${SP}" -i /tmp/aiperf-metrics-src.patch; \
      test -z "$(find "${SP}/aiperf" -name '*.orig' -o -name '*.rej')"; \
      python -c "from aiperf.common.metric_utils import normalize_metrics_endpoint_url as n; assert n('http://h:8000/v1') == 'http://h:8000/metrics', n('http://h:8000/v1'); import aiperf.server_metrics.data_collector as d; assert hasattr(d.ServerMetricsDataCollector, '_probe_root_metrics')"; \
      rm -f /tmp/aiperf-metrics-src.patch /tmp/aiperf-server-metrics-v1-url-fix.patch; \
      apt-get purge -y patch; \
      apt-get autoremove -y; \
      rm -rf /var/lib/apt/lists/*; \
    fi; \
    if [ -n "${PRUNE}" ]; then pip uninstall -y ${PRUNE}; fi; \
    find "${SP}" -type d -name __pycache__ -prune -exec rm -rf {} +; \
    find "${SP}" -maxdepth 3 -type d -name tests -prune -exec rm -rf {} +; \
    rm -rf "${SP}"/pip "${SP}"/pip-*.dist-info "${SP}"/setuptools "${SP}"/setuptools-*.dist-info "${SP}"/pkg_resources; \
    rm -rf /usr/local/lib/python3.13/idlelib /usr/local/lib/python3.13/turtledemo \
           /usr/local/lib/python3.13/tkinter /usr/local/lib/python3.13/ensurepip \
           /usr/local/lib/python3.13/lib2to3 /usr/local/lib/python3.13/test; \
    mkdir -p /opt/tiktoken_cache; \
    if [ "${TIKTOKEN_CACHE}" = "1" ]; then \
      TIKTOKEN_CACHE_DIR=/opt/tiktoken_cache python -c "import tiktoken; tiktoken.get_encoding('o200k_base')" \
        || echo "WARN: tiktoken 词表下载失败：--tokenizer builtin 不可用；传本地 tokenizer 目录不受影响" >&2; \
    else \
      echo "INFO: TIKTOKEN_CACHE=0，跳过 builtin 词表预缓存"; \
    fi; \
    chown -R 10001:10001 /opt/tiktoken_cache

# 相对于构建上下文（= Runner 根目录）。运行时逻辑：run.sh（契约面）+ agentx_runner.py（实现）。
# runner.yaml 不进镜像（注册表/平台侧读它），所以镜像 tag 与 runner.yaml.version 靠发版流程对齐。
COPY run.sh agentx_runner.py /runner/
RUN chmod 0755 /runner/run.sh /runner/agentx_runner.py

# 非 root 运行。挂载目录属主需由挂载方保证：
#   平台 K8s Executor -> securityContext.fsGroup: 10001
#   自己 docker run    -> -u "$(id -u):$(id -g)" 或 chown
USER 10001:10001

WORKDIR /workspace

# 与 runner.yaml 的 entry 数组一致（唯一权威是 runner.yaml，§5.2）。
ENTRYPOINT ["/runner/run.sh"]

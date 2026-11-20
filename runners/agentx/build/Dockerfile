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
RUN set -eux; \
    pip install --index-url "${PIP_INDEX_URL}" "agentx-harness==${AGENTX_VERSION}"; \
    if [ -n "${PRUNE}" ]; then pip uninstall -y ${PRUNE}; fi; \
    SP=/usr/local/lib/python3.13/site-packages; \
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

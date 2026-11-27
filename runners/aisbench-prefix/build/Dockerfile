# Argus Runner: aisbench-prefix —— **唯一发布镜像**（多架构，真 ais_bench）
#
# 唯一入口 /runner/run.sh（§5.2）。构建上下文 = **Runner 目录**（`runners/aisbench-prefix`），
# 不是仓库根——这是发版路径：.github/workflows/release.yml 给每个 Runner 的 context 就是
# `runners/<name>`、dockerfile 是 `runners/<name>/build/Dockerfile`。
#   docker build -f runners/aisbench-prefix/build/Dockerfile \
#     -t ghcr.io/ucm-system/argus-runner-aisbench-prefix:0.1.0 runners/aisbench-prefix
#
# ---------------------------------------------------------------------------
# 为什么不 FROM 官方 AISBench 镜像（与 aisbench-sla 不同，这是本 Runner 的取舍）
# ---------------------------------------------------------------------------
# 官方 ghcr.io/aisbench/aisbench_benchmark 的 tag 是**单架构各一份**
# （…-x86_64 / …-aarch64，见 benchmark 仓库 docker/OVERVIEW.md 的命名规则
# `{tag}-{OS}-{py}-{arch}`），不是多架构 manifest list。而 release.yml 发版时对每个
# Runner 镜像都跑 `docker buildx imagetools inspect` 并要求清单里同时有
# linux/amd64 与 linux/arm64（"不许静默降级成单架构"）。FROM 单架构基镜像的构建
# 会在那一步直接红。
#
# 所以这里照上游 docker/ubuntu/Dockerfile.py310.ubuntu22.04 的做法自己打，但更省：
#   python:3.10-slim-bookworm（多架构官方镜像）
#   + CPU 版 torch（避开 x86 的 CUDA 依赖，镜像从 ~6.4GB 降到 ~1.5GB）
#   + pip install ais-bench-benchmark==<钉死版本>（PyPI 上的 wheel 是 py3-none-any，
#     架构无关；依赖里的 tree_sitter_languages 有 manylinux aarch64 wheel，所以
#     arm64 侧不需要源码编译）
# 依据：本 Runner 只走 ais_bench 的**API 压测**路径（ais_bench --models vllm_api_chat_temp
# --mode perf，HTTP 打被测服务），不需要官方镜像里那套 NPU/CANN/mindie 栈——
# 那些是本地模型评测用的。
#
# 版本必须钉死（§6.2 可追溯）：`ARG AIS_BENCH_VERSION`，禁止 latest。
# 构建期**不 clone 任何仓库**（vendored 的工具在本目录 tool/ 下，见 tool/VENDORED.md），
# 只有 pip 下载依赖这一步联网。
#
# ---------------------------------------------------------------------------
# work_path 为什么在 /tmp（run.sh 里还会各接一根线）
# ---------------------------------------------------------------------------
# 平台 Executor 固定 ReadOnlyRootFilesystem，只有 /workspace/output 与 /tmp 可写，
# 所以工具写模型配置/数据集软链的「影子源码根」只能是 /tmp 下的目录（默认
# /tmp/prefix-ws），靠 run.sh 导出的 AIS_BENCH_DATASETS_CACHE + --config-dir 接给 ais_bench。
# 详见 runners/aisbench-prefix/README.md「work_path 接线」。
FROM python:3.10-slim-bookworm

ARG AIS_BENCH_VERSION=3.1.20260630
ARG RUNNER_UID=10001
ARG RUNNER_GID=10001
# 镜像源可按环境覆盖（内网构建时指到镜像站；默认 PyPI）。
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

ENV HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    HF_HOME=/tmp/hf \
    HF_DATASETS_CACHE=/tmp/hf/datasets \
    TRANSFORMERS_CACHE=/tmp/hf/transformers \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# 非 root（§13.2：平台按 10001:10001 + fsGroup 挂产物卷；PSA restricted 要求 runAsNonRoot）。
# /models 建出来只是给 D-63 的统一共享挂载一个落脚处（内容由 PVC 提供）。
RUN groupadd -g ${RUNNER_GID} runner \
 && useradd -u ${RUNNER_UID} -g ${RUNNER_GID} -M -d /tmp -s /bin/bash runner \
 && mkdir -p /runner /models /workspace/output /tmp/prefix-ws /tmp/prefix-state \
 && chown -R ${RUNNER_UID}:${RUNNER_GID} /runner /models /workspace /tmp/prefix-ws /tmp/prefix-state

# ---- 执行入口与产物汇总（本仓库自有代码）--------------------------------------
COPY run.sh summarize.py /runner/

# ---- vendored 工具（只读副本；含题库 GSM8K.jsonl；本地改动见 tool/VENDORED.md）--
COPY tool /runner/tool

# ---- 离线冒烟用的小数据集（不在发布镜像里放桩程序，见 tests/Dockerfile.stub）----
COPY tests/fixtures /runner/fixtures

# CRLF 归一到 LF：Windows 检出时 *.py / *.sh 可能带 CRLF，带 CRLF 的 shebang 在
# Linux 里会报 `/usr/bin/env: 'python3\r'`。在镜像里擦一遍比要求人人配 .gitattributes 稳。
RUN sed -i 's/\r$//' /runner/run.sh /runner/summarize.py \
 && find /runner/tool /runner/fixtures -type f -exec sed -i 's/\r$//' {} + \
 && chmod 0755 /runner/run.sh \
 && chown -R ${RUNNER_UID}:${RUNNER_GID} /runner

# ---- ais_bench：先 CPU 版 torch，再装 ais-bench-benchmark ----------------------
# torch 单独先装的原因：PyPI 上的 x86_64 torch wheel 会拖 CUDA 全家桶（数 GB），
# 而本 Runner 只用 API 压测。装完 torch 后 ais_bench 的依赖检查会直接满足。
RUN python -m pip install --index-url "${TORCH_INDEX_URL}" "torch" \
 && python -m pip install --index-url "${PIP_INDEX_URL}" \
      --extra-index-url "${TORCH_INDEX_URL}" \
      "ais-bench-benchmark==${AIS_BENCH_VERSION}" \
      "pandas" "requests" "tqdm"

# 构建期自检（能红才有意义）：命令在、版本对得上、模型配置目录可读、非 root 可写 /tmp。
# 上游 docker/build_image.sh 也是这么验的（它跑 `ais_bench --models … --search`）。
USER ${RUNNER_UID}:${RUNNER_GID}
RUN set -eu; \
    command -v ais_bench >/dev/null; \
    ais_bench --help >/dev/null; \
    python -c "import ais_bench.benchmark as m, sys; \
v = getattr(m, '__version__', ''); \
print('ais_bench', v, 'python', sys.version.split()[0]); \
sys.exit(0 if v.startswith('3.1') else 1)"; \
    test -w /tmp/prefix-ws; test -w /workspace/output; \
    test -f /runner/tool/GSM8K.jsonl; test -x /runner/run.sh; \
    id -u | grep -qx "${RUNNER_UID}"

WORKDIR /workspace

# 平台 Job Spec 的 command 也是 /runner/run.sh，二者等价（entry 是 exec form，§5.2）。
ENTRYPOINT ["/runner/run.sh"]

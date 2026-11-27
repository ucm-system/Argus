# syntax=docker/dockerfile:1
# Build context: runners/aisbench-perf
ARG TARGETARCH
ARG BASE_AMD64=ghcr.io/aisbench/aisbench_benchmark:v3.1-20260330-master_x86_64_py_310
ARG BASE_ARM64=ghcr.io/aisbench/aisbench_benchmark:v3.1-20260330-master_aarch64_py_310
FROM ${BASE_AMD64} AS base-amd64
FROM ${BASE_ARM64} AS base-arm64
FROM base-${TARGETARCH} AS runtime
ARG PIP_INDEX_URL=https://pypi.org/simple
USER root
WORKDIR /runner
COPY build/requirements.txt /runner/build/requirements.txt
# Install only at build time; the source archive is pinned in requirements.txt.
RUN python3 -m pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r /runner/build/requirements.txt
COPY . /runner
RUN groupadd --gid 10001 aisbench \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /bin/bash aisbench \
 && chmod 0755 /runner/run.sh /runner/run-local.sh \
 && mkdir -p /workspace/output /data /models /benchmark/ais_bench/datasets \
 && chown 10001:10001 /workspace/output
ENV HOME=/tmp \
    SHELL=/bin/bash \
    USER=aisbench \
    LOGNAME=aisbench \
    TORCH_HOME=/tmp/torch \
    TORCHINDUCTOR_CACHE_DIR=/tmp/torchinductor \
    TRITON_CACHE_DIR=/tmp/triton \
    AIS_BENCH_DATASETS_CACHE=/benchmark \
    XDG_CACHE_HOME=/tmp/.cache \
    HF_HOME=/tmp/hf \
    HF_DATASETS_CACHE=/tmp/hf/datasets \
    HF_HUB_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/runner \
    LANG=C.UTF-8
USER 10001:10001
WORKDIR /workspace
# Exercise CLI imports with the same user as the running container.
RUN python3 -c "import os, pwd; user = pwd.getpwuid(os.getuid()); assert user.pw_name == 'aisbench'; assert user.pw_shell == os.environ['SHELL'] == '/bin/bash'; assert os.access(user.pw_shell, os.X_OK); from testplane_model import Chat; from ais_bench.benchmark.cli.workers import WORK_FLOW"
ENTRYPOINT ["/runner/run.sh"]

# Argus Runner: aisbench-sla —— **唯一发布镜像**（多架构，pip 装 ais_bench）
#
# 唯一入口 /runner/run.sh（ARCHITECTURE §5.2 Single Entry Point）。
#
# 构建上下文 = **Runner 目录**（`runners/aisbench-sla`），不是仓库根：
#   docker build -f runners/aisbench-sla/build/Dockerfile runners/aisbench-sla
# 这是**发版路径**：.github/workflows/release.yml 的矩阵给每个 Runner 的 context 就是
# `runners/<name>`（与 endpoint-probe 同一约定），所以这里的 COPY 全部相对 Runner 目录。
# 历史上写的「context = 仓库根」已作废 —— 用仓库根做 context 会去找
# runners/aisbench-sla/runners/aisbench-sla/run.sh，构建必失败（评审 #62 硬问题 3）。
#
# 构建由 CI 执行（§6.2）；runner.yaml 的 image 写产物引用即可（tag / digest 均可）。
# vendored 来源与 commit 见 runners/aisbench-sla/tool/VENDORED.md。
# 这里刻意**不**联网 clone 上游仓库：构建输入必须只是本仓库的字节（§6.2 可追溯）；
# 只有 pip 下载依赖这一步联网（版本已钉死，见下）。
#
# ---------------------------------------------------------------------------
# 镜像路线（2026-11-28 改）：与 aisbench-prefix 统一——自打多架构，不再 FROM 官方镜像
# ---------------------------------------------------------------------------
# 0.2.0 起本镜像 FROM 官方 ghcr.io/aisbench/aisbench_benchmark:v3.1-20260330-…
# （单架构 x86_64 tag，6.4GB），并在 README 里写明「钉 0330 避免与 0630 行为漂移」。
# 2026-11-28 改为 prefix 同款自打路线（python slim + pip ais-bench-benchmark），理由：
#   1. aisbench-prefix 已在 kind 上用 pip 安装的 ais_bench 20260630 真跑通整条链路
#      （Task t-20261127-TMXF，三点阶梯 succeeded），「pip 路线没验证过」的前提不再成立；
#   2. 0330 vs 0630 的已知行为差异只有 base_api 给非空 path 补尾斜杠（双 /v1），
#      而 sla 的 run.sh 一直按「服务根」归一化 url（去 /v1）——两种版本行为一致，无漂移风险；
#   3. 官方镜像只有 x86_64 tag，arm64 发版腿要靠 binfmt 模拟跑 x86 的 RUN 步骤（慢）；
#      python slim + PyPI wheel（py3-none-any）+ aarch64 依赖 wheel 全部原生可得；
#   4. 6.4GB → ~1.5GB，官方镜像里的 NPU/CANN/mindie 栈本 Runner 用不上。
# 版本漂移的接受理由：PyPI 最早只发到 20260611，钉不住 0330；20260630 是 kind 真跑
# 验证过的版本，TTFT/TPOT/E2EL 汇总表格式（parse.py 依赖）在该版本上解析正常。
#
# ---------------------------------------------------------------------------
# 只有一种发布镜像（用户 2026-11-09 拍板，stub 只留在 tests/）
# ---------------------------------------------------------------------------
# stub 只留在 `tests/Dockerfile.stub`（离线冒烟用）；发布矩阵只扫 `build/Dockerfile*`，
# 所以 tests/ 下的 stub 镜像**不会**进发布。
#
# ---------------------------------------------------------------------------
# 关键设计：为什么 ais_bench 的工作目录是 /tmp/aisbench-ws 而不是 /benchmark
# ---------------------------------------------------------------------------
# ais_bench 的源码根（pip 装的就是它，root:root 0755）。
# 本 Runner 的 vendored 工具会往 **work_path 下**写两样东西：
#   <work_path>/ais_bench/benchmark/configs/models/vllm_api/vllm_api_chat_temp.py  （模型配置）
#   <work_path>/ais_bench/datasets/gsm8k/{train,test}.jsonl                        （数据集挂载）
# 参考工具是靠「<work_path> 恰好就是源码根」让 ais_bench 找到它们的。平台的 Executor
# 用 ReadOnlyRootFilesystem=true 跑 Job（只有 /workspace/output 与 /tmp 可写），所以
# 平台上不可能写 /benchmark；把 /benchmark chown 给 10001 也只解决本地、解决不了平台。
#
# 于是这里改成「/tmp 下的影子源码根」，用 ais_bench 自己的两个开关接上：
#   * 数据集：ais_bench 的相对数据集路径是拼在 AIS_BENCH_DATASETS_CACHE 下的
#     （datasets/utils/datasets.py: get_cache_dir() 读该环境变量，默认才是源码根），
#     所以 run.sh 导出 AIS_BENCH_DATASETS_CACHE=<work_path> 即可让
#     path='ais_bench/datasets/gsm8k' 落到 <work_path>/ais_bench/datasets/gsm8k。
#   * 模型配置：ais_bench 支持 --config-dir，会在 <config-dir>/models 下找模型配置，
#     run.sh 在 work_path 不是 /benchmark 时自动追加
#     --config-dir <work_path>/ais_bench/benchmark/configs。
# 这两处都在 run.sh（本仓库自己的入口）里做，**没有改 vendored 的 sla_tool/**。
#
# ---------------------------------------------------------------------------
# 镜像**不带** tokenizer / 数据集 / 权重（这是设计，不是缺陷）
# ---------------------------------------------------------------------------
# 真机需要 tokenizer 或权重时，走 D-63 的统一共享挂载（默认 `/models`，只读 PVC），
# 或用 `--model_path` 指一个**已经存在**的容器内路径（Profile 挂载 / 外部提供）。
# 镜像里没有 `/models/*` 内容 —— 平台不替 Runner 分发模型；缺了由 run.sh/Runner 明确报错。
#
# 0.6.0 起另有前置校验：AG_MODEL_PATH 缺失/目录不存在 → 秒级 VALIDATION_ERROR
# （ais_bench 的 default_perf 汇总器要 load_tokenizer，等压测跑完才报太晚）。
FROM python:3.10-slim-bookworm

ARG AIS_BENCH_VERSION=3.1.20260630
# torch 钉死到 CPU 轮子（两个架构都有：torch-2.14.1+cpu-cp310-cp310-manylinux_2_28_{x86_64,aarch64}.whl）。
# 用 `==<ver>+cpu` 而不是裸 `torch`：裸名在 PyPI 上是 CUDA 版（x86 会拖 nvidia-* 数 GB）。
ARG TORCH_VERSION=2.14.1
ARG RUNNER_UID=10001
ARG RUNNER_GID=10001
# 镜像源可按环境覆盖（内网构建时指到镜像站；默认 PyPI）。
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

ENV HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    HF_HOME=/tmp/hf \
    HF_DATASETS_CACHE=/tmp/hf/datasets \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# 非 root 用户（§13.2 平台按 10001:10001 + fsGroup 挂产物卷）。
# /tmp 属于 10001：work_path 的默认位置就在这下面（平台只保证 /tmp 可写）。
# /models 建出来只是为了让"统一共享挂载点"有个落脚处（内容由 D-63 的 PVC 提供）。
RUN groupadd -g ${RUNNER_GID} runner \
 && useradd -u ${RUNNER_UID} -g ${RUNNER_GID} -M -d /tmp -s /bin/bash runner \
 && mkdir -p /runner /models /workspace/output /tmp/aisbench-ws \
 && chown -R ${RUNNER_UID}:${RUNNER_GID} /runner /models /workspace /tmp/aisbench-ws

# ---- 执行入口 + 阶梯报告（本仓库自有代码）--------------------------------------
COPY run.sh report.py /runner/

# ---- vendored 工具（只读副本；本地改动见 tool/VENDORED.md）--------------------
COPY tool /runner/tool

# ---- 离线冒烟用的小数据集（8 行 × 512 字符）-----------------------------------
COPY tests/fixtures /runner/fixtures

# CRLF 归一到 LF：本仓库在 Windows 上检出时 `tool/**/*.py` 可能带 CRLF，
# 带 CRLF 的 shebang 在 Linux 里会报 `/usr/bin/env: 'python3\r'`。
# 在镜像里擦一遍，比要求每个开发者配 .gitattributes 稳。
RUN sed -i 's/\r$//' /runner/run.sh /runner/report.py \
 && find /runner/tool -name '*.py' -type f -exec sed -i 's/\r$//' {} +

RUN chmod 0755 /runner/run.sh /runner/tool/sla_search.py \
 && chown -R ${RUNNER_UID}:${RUNNER_GID} /runner

# ---- ais_bench：先 CPU 版 torch，再装 ais-bench-benchmark ----------------------
# torch 单独先装的原因：PyPI 上的 x86_64 torch wheel 会拖 CUDA 全家桶（数 GB），
# 而 sla 与 prefix 一样只走 API 压测路径。装完 torch 后 ais_bench 的依赖检查直接满足。
#
# **两条索引都要给**：只写 `--index-url ${TORCH_INDEX_URL}` 时，torch 的依赖
# （typing-extensions / filelock / fsspec …）也只能从 pytorch 索引找，那里某些版本
# 只有 sdist → pip 转去 build，而 build backend（flit_core）同样只在 PyPI 上，
# 于是 `No matching distribution found for flit_core<4,>=3.11` 直接构建失败（本机踩过）。
# `+cpu` 这个本地版本号 PyPI 上没有，所以主索引给 pytorch、补充索引给 PyPI 是安全的。
#
# transformers 随 ais-bench wheel 依赖带入（数据集生成时 sla_tool/dataset.py 惰性加载
# AutoTokenizer）；指定 --dataset 时不需要 tokenizer，也不需要 transformers 真正加载。
RUN python -m pip install \
      --index-url "${TORCH_INDEX_URL}" --extra-index-url "${PIP_INDEX_URL}" \
      "torch==${TORCH_VERSION}+cpu" \
 && python -m pip install --index-url "${PIP_INDEX_URL}" \
      --extra-index-url "${TORCH_INDEX_URL}" \
      "ais-bench-benchmark==${AIS_BENCH_VERSION}" \
      "tqdm"

# 构建期自检（能红才有意义）：命令在、版本对得上、非 root 可写关键目录、
# 工具入口在。上游 docker/build_image.sh 也是这么验的（它跑 `ais_bench --models … --search`）。
# 注意不检查 GSM8K.jsonl——sla 的语料走 --gsm8k-source CLI 参数（外部提供），
# vendored 工具不带题库（那是 aisbench-prefix 的做法）。
USER ${RUNNER_UID}:${RUNNER_GID}
RUN set -eu; \
    echo "[check] uid=$(id -u)"; \
    command -v ais_bench; \
    ais_bench --help >/dev/null; \
    python -c "import ais_bench.benchmark as m, sys; \
v = getattr(m, '__version__', ''); \
print('ais_bench', v, 'python', sys.version.split()[0]); \
sys.exit(0 if v.startswith('3.1') else 1)"; \
    test -w /tmp/aisbench-ws && echo "[check] /tmp/aisbench-ws writable"; \
    test -w /workspace/output && echo "[check] /workspace/output writable"; \
    test -x /runner/tool/sla_search.py && echo "[check] tool entry present"; \
    test -x /runner/run.sh && echo "[check] run.sh executable"; \
    echo "[check] all passed"

WORKDIR /workspace

# 平台 Job Spec 的 command 也是 /runner/run.sh，二者等价。
ENTRYPOINT ["/runner/run.sh"]

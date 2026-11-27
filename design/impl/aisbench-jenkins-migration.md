# Jenkins AISBench 测试迁移（0.3.0）

覆盖 reference/K8s-AISBench-Test.Jenkinsfile 的 acc、perf、prefix 三条测试路径、参数和产物。平台状态机、表结构、API 不变；Pod、挂载和制品保存沿用平台现有能力。

旧 Jenkins 的 acc 并不限于 GSM8K：AISBENCH_DATASETS 是 AISBench 配置名列表。新增 `datasets` 参数，精度走原生 `--mode all --merge-ds --summarizer example`，普通性能走 `--mode perf --summarizer default_perf`。保留各数据集自己的提示词、预处理和 evaluator，不统一换成 GSM8K。实际可用数据集由容器配置、数据文件以及聊天模型支持的推理方式决定，不保证所有 AISBench 数据集都适用。

两种入口互斥：原有 `dataset` 是 Tester 自己处理的 GSM8K/JSONL 路径；新增 `datasets` 是配置名，接受逗号分隔字符串或 JSON 数组。`config_dir` 可指定原有 aisbench_configs 目录；`working_dir` 设置子进程工作目录，默认调用者当前目录，但不替代 AISBench 的数据根目录。模型配置从容器的 vllm_api_stream_chat 模板加载，仅在本次输出目录生成覆盖配置，不修改安装文件。没有版本或源码校验。

2026-11-26 挂载约定（0.3.1）：平台将数据集根目录挂到 `/benchmark/ais_bench/datasets`，其下按数据集组织，例如 `gsm8k/train.jsonl`、`gsm8k/test.jsonl`。两个镜像设置 `AIS_BENCH_DATASETS_CACHE=/benchmark`，使配置中的 `ais_bench/datasets/gsm8k` 指向该挂载。该变量是原生数据集的查找根目录，与 `/tmp/hf/datasets` 中的 Hugging Face 中间缓存不同。数据不打包进镜像，挂载内容需可被运行用户读取。绝对数据路径仍按配置原值使用；自定义配置不采用 `ais_bench/datasets/...` 布局时需填写正确的绝对路径。本地 run-local.sh 将 DATA_DIR 同时挂到固定路径和兼容路径 /data；直接使用已有容器时需自行设置此环境变量。

Docker Hub 发布：独立 workflow `aisbench-dockerhub.yml` 只由手动触发，构建上下文分别为 `runners/aisbench-acc`、`runners/aisbench-perf`，使用其中 `build/Dockerfile`。使用仓库 Secrets `DOCKERHUB_USERNAME`、`DOCKERHUB_TOKEN` 登录，发布 `<username>/argus-runner-aisbench-{acc,perf}:<tag>` 及 `sha-<完整提交 SHA>`；tag 留空时读取各自 runner.yaml 的 version，不更新 latest。测试使用本地模拟服务，不要求 GPU、真实数据集或 UCM，也不代表真实服务验收。镜像发布不会自动修改平台注册信息，接入 Docker Hub 时需按版本规则更新 runner.yaml 的 image/version 并重启平台。原有 GHCR release workflow 保持独立。操作步骤见 [Docker Hub 发布说明](../../docs/aisbench-dockerhub.md)。

Docker Hub 的构建源码固定来自远端 `temp` 分支。每次 workflow 先解析一次 temp 的提交 SHA，再让四个构建任务检出同一 SHA，避免构建期间分支更新导致镜像使用不同代码。镜像 sha 标签、revision 标签和 Summary 都记录该源码 SHA，不使用触发 workflow 所在分支的 github.sha。

构建矩阵名称使用专用环境变量 `AISBENCH_RUNNER`，不得覆盖 GitHub 内置的 `RUNNER_NAME`（执行机器名称）。读取 runner.yaml 和生成镜像仓库名都使用该专用变量。

0.3.6 多架构构建：两个 Dockerfile 用 BuildKit 的 TARGETARCH 选择 base-amd64/base-arm64；BASE_AMD64、BASE_ARM64 默认指向 AISBench v3.1-20260330-master 对应的 x86_64/aarch64 Python 3.10 镜像，支持通过构建参数覆盖；PIP_INDEX_URL 可指定依赖镜像源。保留 temp 的固定依赖安装策略，genet 的华为基础镜像适配不合入本分支。无 TARGETARCH 默认值，构建仅支持 amd64/arm64。两个平台使用相同 AISBench 源码和业务代码。

CI 固定一次 temp 源码 SHA，展开 acc/perf × amd64/arm64 四个任务，分别使用 ubuntu-22.04 和 ubuntu-22.04-arm 原生节点。每个任务核对镜像 OS/Architecture，执行入口帮助和 real_smoke.py，通过后推送 run-<run_id>-<attempt>-<arch> 暂存标签，并上传该镜像 digest。缓存按 Runner 和架构隔离。四个构建任务全部成功后，两个发布任务分别下载本次运行的 amd64/arm64 digest，以 digest 合成暂存 index，校验仅包含 linux/amd64 和 linux/arm64 且子 digest 与测试产物一致，再发布版本及 sha 标签。失败时不把单架构镜像发布为正式多架构标签；成功的暂存镜像可能已留在仓库。两个最终发布任务并非事务，一个发布失败时另一个可能已经成功。Summary 中的 digest 是整个多架构 index 的 digest。重跑失败任务时，成功任务的本次运行 artifacts 可复用；不能按 sha 可变标签寻找另一个架构的产物。

0.3.2 镜像用户修复：两个 Dockerfile 创建 aisbench 用户和组（UID/GID 10001），再切换到 `USER 10001:10001`，让 pwd.getpwuid 能解析用户。设置 USER/LOGNAME=aisbench，兼容本地 run-local.sh 覆盖数值 UID 时的 getpass.getuser；TORCH_HOME、TORCHINDUCTOR_CACHE_DIR、TRITON_CACHE_DIR 均放在 /tmp。构建末尾以非 root 用户检查用户记录并导入 AISBench CLI workers，覆盖本次 evaluate → transformers → torch._dynamo 导入失败链；GitHub workflow 继续使用只读根文件系统和可写 /tmp 运行真实 AISBench 模拟服务测试。不增加 run.sh 的版本或源码兼容性门禁。

0.3.3 精度结果解析修复：AISBench 构建基线中的 BaseEvaluator.evaluate 会按 replica 将 pred/answer/correct 包成列表，OpenICLEvalTask.format_details 将它们写成 predictions/references/correct，即使只有一次推理也得到单元素列表。GSM8K 逐题审计同时接受原来的标量和这三个字段的单元素列表，转换为标量后继续核对布尔评分、样本 ID、实际回答、参考答案和汇总准确率。空列表、多元素列表、非布尔 correct、缺字段仍失败，不按汇总分数猜逐题成绩。原始 AISBench JSON 不修改，sample_scores.jsonl 保持原标量字段格式。回归覆盖 True/False 列表、错误格式、回答错配和汇总不一致。依据为 [本次失败日志](https://github.com/KINN123/Argus/actions/runs/45749610457/job/139673996601) 和构建基线源码的 BaseEvaluator.evaluate、OpenICLEvalTask.format_details。

原生路径参数：num_prompts、num_warmups、temperature、ignore_eos、output_len、concurrency、request_rate。默认低并发，temperature=0.01、ignore_eos=true 对齐 Jenkins；不改变既有 GSM8K 和生成数据路径的缺省值。显式给出的其它模式参数必须拒绝，不能看似接受却不生效。

0.3.5（temp）：为支持 VS Code 附加容器后的交互终端，aisbench 用户的 passwd Shell 从 /usr/sbin/nologin 改为 /bin/bash，并设置 SHELL=/bin/bash。构建时检查当前用户的 Shell 路径和可执行权限；UID/GID 仍为 10001，平台入口仍为 /runner/run.sh。0.3.4 已由 genet 分支的基础镜像适配使用，本次跳过该版本以区分产物。旧容器可由宿主机用 docker exec -u root 调用 usermod -s /bin/bash aisbench，然后重连 VS Code；只读根文件系统应通过 VS Code 远程终端配置指定 Bash，或重建容器使用新镜像。

prefix 仍用现有生成、预热、正式测量流程。增加 `test_cases` 接收旧六列数组 [input_len, output_len, num_requests, hit_rate百分数, concurrency, seed]，转换为私有前缀 rounds；预热输出 max(1, output_len//10)，并发与正式阶段一致，默认 vllm_prompt 指标口径。PD 分离的 POD_INFO 对应 pod_info/metrics_endpoint，逐 endpoint 保留，不自动发现部署形态。

输出：平台 result.json；原始日志、CSV、预测文件保持在本次 raw/native/aisbench 下；额外 accuracy.csv（原始汇总列）、performance_flat.csv（旧 label_statistic 表头）、legacy_results.json（build/type/status/data/extra_info，build 使用平台 task_id）。多份性能表逐份保存并加 source 防止覆盖。数值保留符号和科学计数法，缺失不填 0。退出非零、无有效汇总或汇总缺成绩均失败，仍留原文件。原生路径依赖 AISBench 自身的请求和评分判定，明确记录验证范围，不声称具备旧 GSM8K 逐题审计。

Jenkins 的 MongoDB 写入改为本地产物，由平台收集，不在 Runner 写外部数据库。EXTRA_INFO 对应 extra_info。镜像由 runner.yaml 固定版本；NFS 和模型/数据路径由平台 profile_mount 配置；不搬入 Jenkins 凭据、节点 IP 或特权 Pod。

参数对应：

| Jenkins | Runner / 平台 |
|---|---|
| SERVER_URL / API_MODEL_NAME / MODEL_FOLDER_NAME | AG_SERVED_URL / AG_SERVED_MODEL_NAME / AG_MODEL_PATH；本地用 url / model_name / model_path |
| AISBENCH_DATASETS | datasets 数组；CLI 可用逗号分隔 |
| max_out_len / batch_size | output_len / concurrency |
| num_prompts / num_warmups / request_rate | 同名参数，前两项用于原生数据集路径 |
| temperature / ignore_eos | 同名参数，原生路径沿用 Jenkins 缺省值 |
| TEST_TYPE=acc | aisbench-acc + datasets |
| TEST_TYPE=perf | aisbench-perf + datasets |
| TEST_TYPE=prefix / TEST_CASES | aisbench-perf + test_cases |
| PD_DISAGGREGATION / POD_INFO | 调用者按部署填写 pod_info 或 metrics_endpoint，不额外增加布尔开关 |
| EXTRA_INFO / BUILD_NAME | extra_info / 平台任务名称（prefix 也保存 extra_info） |
| CUSTOM_TEST_IMAGE / Pod YAML | Runner 镜像版本与 Execution Profile |

旧 Check Endpoint 发的 ping 请求不另行复制到 Runner；服务可用性沿用平台执行前检查，本地直接运行由实际 AISBench 请求报告连接错误。Jenkins 分支扫描、构建显示名、MongoDB 写入和归档阶段由平台职责替代，不嵌入测试脚本。

验证包括原生命令构造、参数互斥、多数据集汇总、失败保留、旧六列测试用例转换、结果契约与注册参数。真实 AISBench 3.1 和用户数据集还需在容器运行；本机的离线验证不能代替真实测试。

2026-11-24：精度 44 项、性能 74 项离线测试通过；Go registry 包测试通过。使用临时目录中的 MMEngine 0.10.7 验证生成配置可 Config.fromfile 加载并 Config.dump 保存，另用缓存的真实 AISBench 模型模板验证后处理得以保留（模型类与后处理函数使用替身，不发模型请求）。真实服务、镜像构建与平台任务提交未在本机执行。

原生 CLI 配置选择与汇总格式还对照了 [AISBench ConfigManager](https://github.com/AISBench/benchmark/blob/525ed1779f50a8ac068b8638771f633a151e769f/ais_bench/benchmark/cli/config_manager.py) 和 [模型模板](https://github.com/AISBench/benchmark/blob/525ed1779f50a8ac068b8638771f633a151e769f/ais_bench/benchmark/configs/models/vllm_api/vllm_api_stream_chat.py)。运行时仍使用容器自己的模板，这些来源不构成版本门禁。

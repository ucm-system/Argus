# agentx：长上下文 agentic 压测（真实 Weka agentic coding trace 回放）

回答一个问题：**这套服务在真实的 agentic coding 负载下表现如何？**

- 角色 `test`：跑完就出指标，是否达标由下游判断（平台不做门禁，§1 / §7.4）；失败不终止 Task（`on_failure: continue`）
- 入口 `/runner/run.sh`（由 `runner.yaml` 的 `entry: ["/runner/run.sh"]` 声明，§5.2）
- 契约 [ARCHITECTURE.md](../../design/ARCHITECTURE.md) §5.2、§5.3、§5.4、§5.4.1、§5.8、§5.9、§5.10、附录 A
- 负载来源：SemiAnalysis InferenceX 的 [Weka agentic trace 语料](https://inferencex.semianalysis.com/datasets)（HuggingFace `semianalysisai/cc-traces-weka-062126`，393 条真实 Claude Code 会话，含 subagent 分支）

## 目录结构

```text
agentx/
├── runner.yaml        # 唯一权威：元数据 + entry + input_format + 参数 schema + 资源/并发
├── run.sh             # 唯一执行入口（平台调它，独立运行也跑它）：契约面
├── agentx_runner.py   # 运行期实现：参数校验 → 调 agentx → result.json 归一化
├── build/
│   └── Dockerfile     # 发布镜像（构建上下文 = 本目录）
├── run-local.sh       # 本地便捷脚本（只 run，不 build）
├── tests/             # 维护者自测，不参与注册与调度
│   ├── stub_agentx.py           # 冒充 agentx profile 的离线 stub
│   ├── fixtures/                # stub 写出的 profile_export_aiperf.json
│   ├── test_runner_yaml.py      # runner.yaml 契约自检 + 默认值漂移
│   ├── test_result_mapping.py   # 参数/命令/指标/退出码/密钥
│   ├── validate_result_schema.py# result.json ↔ openapi Result
│   ├── smoke.sh                 # 一键离线冒烟
│   └── Dockerfile.stub          # 冒烟镜像（不进发布矩阵）
└── README.md
```

**这里没有、也不该有：**

- `deployment.yaml` / `job.yaml` —— 部署由内置 service-deploy runner + Genet 负责（§9）。
- `results/` —— 容器内固定写 `/workspace/output`；本地调试写 `.local/out`。

## 跑的是什么

它把录制下来的真实会话按原时间线重放到被测服务上：

1. **数据集**：从 HuggingFace 拉 `traces.jsonl`（1.85GB），用 `--tokenizer` 把每条 trace 重建为 tokenized、带 KV-cache 结构的会话树（parent + subagent 分支）。首次跑要几分钟，之后按 `random_seed` 命中 mmap 缓存秒级恢复。
2. **warmup**：每条 lane 先回放一个深前缀，把服务端 prefix cache 填成真实的多轮 coding 上下文；root 会话 warmup 一次失败就整体中止。
3. **profiling**：按录制间隔（end-to-start）继续回放，保留会话前缀以复用服务端 KV cache；并发按 **session tree** 计（整棵树排空才换下一条）。
4. **drain + 导出**：在途请求收尾后打印指标表，写 `profile_export_*`。

因此它测的不是"合成 1 轮 prompt 的 QPS"，而是"真实 agentic 负载下的 TTFT / ITL / 吞吐 + 前缀缓存命中率"。

## 怎么调用（v2：输入文件 + env，不走 argv）

输入读取优先级：`--params-file <path>` > `$AG_PARAMS_FILE` > `/workspace/input/params.jsonl`。
输入是 **JSONL**（每行一个 JSON 对象；参数放一行即可）：

```json
{"concurrency":8,"benchmark_duration_seconds":1800,"public_dataset":"semianalysis_cc_traces_weka_062126","random_seed":42}
```

```bash
# 平台内：entry=["/runner/run.sh"]，argv 恒空，参数文件由平台落盘并注入 AG_PARAMS_FILE
# 独立运行（§5.10）：
/runner/run.sh --params-file /tmp/params.jsonl \
  --url=http://10.0.0.10:8000/v1 --model_name=Qwen3-32B
```

| 通道 | 内容 |
|---|---|
| 输入文件 | 全部业务参数（JSONL）。平台做词法/结构校验后原样落盘（§5.4）；未声明字段本 Runner **拒绝**（带行号） |
| 环境变量（公共参数） | `AG_SERVED_URL` / `AG_SERVED_MODEL_NAME`（独立运行可用 `--url` / `--model_name` 覆盖） |
| 环境变量（元信息） | `AG_TASK_ID` / `AG_ATTEMPT` / `AG_RUNNER_SEQ`（缺省 `local` / `1` / `1`） |
| 凭据 | `ENDPOINT_API_KEY`（服务鉴权）、`HF_TOKEN`（gated tokenizer / 私有语料镜像）。平台上由 Task 级 `secrets` 注入（§12.4） |
| 输出目录 | `/workspace/output`（可用 `AG_OUTPUT_DIR` 覆盖）：`result.json`、`agentx/`、`logs/`、`raw/` |

`AG_SERVED_URL` 直接交给 agentx：写成 `http://host:8000/v1` 即可，agentx 自己拼 `/chat/completions`，不会拼出 `/v1/v1/...`。

**API key 不进 argv**：本 Runner 传的是 `--api-key '${ENDPOINT_API_KEY}'` 这个**引用**，由 agentx 在配置解析期展开。所以 `ps`、`logs/agentx.log`、`result.json.metadata.command` 里都只有引用形态（§12.4）。

## 参数清单

平台提交期只校验**类型**与 **required 存在性**（§5.4）；范围/枚举/互斥由本 Runner 运行时校验，参数错误一律退出码 2。

| 参数 | 默认 | 说明 |
|---|---|---|
| `concurrency` | `8` | 并发 session tree 数（单个整数）。subagent 并行时在飞请求可超过它 |
| `benchmark_duration_seconds` | `1800` | 测量时长。**< 900 必须配 `allow_short_run=true`**，否则直接判参数错误 |
| `public_dataset` | `semianalysis_cc_traces_weka_062126` | date-pinned 语料别名（滚动别名会变，两次运行不可比） |
| `num_dataset_entries` | `0` | 只取前 N 条 trace（0 = 全量 393）。**仅冒烟**：改变负载却不影响 `submission_valid` |
| `max_context_length` | `0` | 客户端上下文过滤（0 = 不过滤）。填服务真实上限，否则超长请求由服务端报错 |
| `random_seed` | `42` | 起始 turn / 回绕顺序 / 数据集缓存 key。固定才能复现 |
| `tokenizer` | `""` | HF tokenizer 仓库或本地路径；缺省按模型名推导。**服务端用内部别名时必须给** |
| `scenario` | `agentx` | `agentx`（3600s、0~100% 起始窗口）或 `inferencex-agentx-mvp`（1800s、25%~75%） |
| `warmup_requests_per_lane` | `0` | 每 lane 额外预热请求数（0 = 场景默认）；与下一项**互斥** |
| `agentic_cache_warmup_duration` | `0` | 时长制缓存预热（秒，0 = 关）；与上一项互斥 |
| `allow_short_run` | `false` | 放行 < 900s 的短跑（对齐 `--unsafe-override`），结果固定 `submission_valid=false` |
| `hf_cache_dir` | `""` | 语料缓存目录，缺省 `<output>/.cache/hf` 且跑完删除。跨 attempt 复用请指向挂载 PVC |
| `keep_cache` | `false` | 保留 `<output>/.cache`（会算进产物配额，仅排查用） |
| `agentx_cmd` | `agentx` | 替换命令（离线测试指 `tests/stub_agentx.py`） |
| `extra_args` | `""` | 透传未建模的 agentx 开关，如 `--use-server-token-count` |

## 指标口径（附录 A）

`name` 只放指标，`statistic` / `unit` 走字段，维度一律进 `labels`（当前只有 `concurrency`）：

| 指标 | 单位 | statistic | 来源 |
|---|---|---|---|
| `ttft` | `ms` | mean / p50 / p90 / p99 / max | `time_to_first_token` |
| `time_to_second_token` | `ms` | mean | `time_to_second_token` |
| `inter_token_latency` | `ms` | mean / p90 / p99 | `inter_token_latency` |
| `request_latency` | `ms` | mean / p50 / p90 / p99 / max | `request_latency` |
| `request_throughput` | `req_per_s` | mean | `request_throughput` |
| `output_token_throughput` | `tokens_per_s` | mean | `output_token_throughput` |
| `total_token_throughput` | `tokens_per_s` | mean | `total_token_throughput` |
| `output_token_throughput_per_user` | `tokens_per_s` | mean | `output_token_throughput_per_user` |
| `input_sequence_length` / `output_sequence_length` | `tokens` | mean | ISL / OSL |
| `prefix_cache_hit` | `percent` | mean | `theoretical_prefix_cache_hit`（**AgentX 的核心特征**） |
| `request_count` | `count` | count | `request_count` |
| `benchmark_duration` | `s` | max | `benchmark_duration` |

取不到的指标**不写**（不编数）；`metrics` 为空时会在 `notes` 说明。`submission_valid` 与 `submission_invalid_reasons` 落在 `metadata` 与 `notes`：它是**有效性戳，不是执行结果**——`false` 不会让本 Runner 失败（§5.8"执行成功 ≠ 指标达标"）。

## 退出码

| 码 | 含义 | `result.json.error.code` |
|---|---|---|
| `0` | agentx 正常跑完，产物可解析 | — |
| `1` | agentx 非零退出 / 退出 0 但产物缺失或损坏 / 被取消 | `RUNNER_NONZERO_EXIT` / `RESULT_INVALID` / `CANCELED` |
| `2` | 参数或输入错误（未声明字段、范围越界、短跑未授权、缺 `AG_SERVED_URL`） | `VALIDATION_ERROR` |

错误码只用附录 D 的枚举（`server/internal/domain/codes.go`）：自造码不会报错，而是被平台**静默降级成 `RUNNER_NONZERO_EXIT`**，丢掉区分信息。

## 资源、网络与超时（部署前必读）

| 约束 | 说明 |
|---|---|
| **`/tmp` 只有 256MiB** | 平台给只读根 + `/tmp` emptyDir 256Mi（execution §2.1），**装不下 1.85GB 语料**。语料按「语料：挂载本地数据集目录」一节挂到 PVC / 只读目录；拆行与重建的临时文件落在 `<output>/.cache`，跑完删除 |
| **出网** | 语料与 tokenizer 由挂载提供，**容器内不需要外网**：加 `-e HF_HUB_OFFLINE=1` 可以验证这一点。只有不挂载时才会回落到 HuggingFace 下载（需 NetworkPolicy 放行 `huggingface.co`，或 `HF_ENDPOINT` 指向内网镜像） |
| **建议超时** | 冷启动 ≈ 10~20 分钟（下载 + 393 条重建）+ warmup + `benchmark_duration_seconds` + drain。`runners[].timeout_seconds` 建议 ≥ `duration + 1800`，正式 1800s 跑建议 ≥ `5400` |
| **资源** | `4c / 8Gi`：重建阶段按 CPU 数并行；2c/2Gi 能跑但明显更慢 |
| **独占** | `concurrency.endpoint_exclusive: true`：回放期间别的测量会污染吞吐与前缀命中率（§6.7） |
| **非 root** | 镜像以 uid 10001 运行（PSA restricted）；挂载目录属主由平台 `fsGroup` 或调用方 `-u` 保证 |
| **PL 别名** | 服务端模型名是内部别名时**必须**给 `tokenizer`，否则数据集构建阶段失败（退出码 1） |

## 语料：挂载本地数据集目录（容器内不联网）

镜像里没有语料（约 1 GB，只有 agentx-harness + python 运行时）。用你已经下载好的 HuggingFace
仓库目录（`~/code/cc-traces-weka-062126`，含 `traces.jsonl` 1.85 GB）**只读挂进容器**，Runner 走
`--input-file` 直接读它 —— 容器全程不需要外网。

```bash
docker run --rm \
  -v ~/code/cc-traces-weka-062126:/data/cc-traces:ro \
  -v /tmp/out:/workspace/output \
  -v /tmp/params.jsonl:/workspace/input/params.jsonl:ro \
  -e HF_HUB_OFFLINE=1 \
  ${IMAGE} \
  /runner/run.sh --url http://10.0.0.10:8000/v1 --model_name Qwen3-32B
```

`/tmp/params.jsonl` 用 `dataset_dir` 指向挂载点（给 `.jsonl` 或已拆好的目录都行）：

```json
{"concurrency":8,"benchmark_duration_seconds":1800,"dataset_dir":"/data/cc-traces/traces.jsonl","tokenizer":"builtin"}
```

`tokenizer` 用 `builtin` 是零依赖的（tiktoken 词表已烤进镜像）。要用真模型的 tokenizer，把已有缓存
一起挂进来并让它可见：

```bash
  -v ~/.cache/huggingface:/data/hf -e HF_HOME=/data/hf \
  # 参数里写 "tokenizer":"Qwen/Qwen3-32B"（或任意 HF tokenizer 仓库名）
```

### 这条路做了什么（以及必须知道的代价）

- 本地 `weka_trace` loader 只认「目录 + 一条 trace 一个 `*.json`」，所以给 `traces.jsonl` 时 Runner 会
  先按行拆到 `<output>/.cache/weka-split/`（1.85 GB 约 1 分钟），跑完随缓存删除（`keep_cache=true` 可留）。
  已经拆好的目录则直接用，不再复制。
- 走的是 `--custom-dataset-type weka_trace --input-file`，**不是** `--public-dataset`：scenario 的
  「必须钉住公开语料」规则由 Runner 自动用 `--unsafe-override` 放行，因此结果**固定
  `submission_valid=false`**，不能拿去与其他 AgentX 结果对比或提交。
- `tokenizer` 必须显式给（本地语料不再按语料元数据推导）：`builtin`（tiktoken，零依赖）或真模型
  tokenizer（配 `-e HF_HOME=<挂载的 tokenizer 缓存>`）。
- 平台内：Execution Profile 把语料 PVC 只读挂到 `/data/cc-traces`，`dataset_dir` 指到里面的
  `traces.jsonl` 即可；`/tmp` 只有 256 MiB（execution §2.1），不要往那儿放。

### 如果要「可对比 / 可提交」的结果

那就必须走 `--public-dataset`（钉住的公开语料），Runner 用 `datasets.load_dataset(<repo>)` 读，
需要 **HuggingFace 的 datasets 缓存**（`<HF_HOME>/hub` + `<HF_HOME>/datasets`）。
实测：只有 `hub/`（即 `hf download` 的产物）而 `datasets/` 缺失时，`HF_HUB_OFFLINE=1` 会报
`Couldn't reach 'semianalysisai/cc-traces-weka-062126' on the Hub (OfflineModeIsEnabled)` —— 这条路
必须让 `datasets` 在线建过一次转换缓存，或者干脆接受本节的本地语料模式。

## 独立运行

镜像由 CI 产出，这里只 `docker run`，不 build（§6.2）。**语料走上一节的挂载**（容器不需要外网）：

```bash
export IMAGE=registry.dev.huawei.com/argus/agentx@sha256:...
echo '{"concurrency":8,"benchmark_duration_seconds":60,"allow_short_run":true,"dataset_dir":"/data/cc-traces/traces.jsonl","tokenizer":"builtin"}' > /tmp/params.jsonl

docker run --rm \
  -v ~/code/cc-traces-weka-062126:/data/cc-traces:ro \
  -v /tmp/out:/workspace/output \
  -v /tmp/params.jsonl:/workspace/input/params.jsonl:ro \
  -e HF_HUB_OFFLINE=1 \
  ${IMAGE} \
  /runner/run.sh --url http://10.0.0.10:8000/v1 --model_name Qwen3-32B
jq . /tmp/out/result.json
```

便捷脚本（`run-local.sh`）：`.local/params.jsonl` 存在时自动挂成输入文件；设
`DATASET_DIR=~/code/cc-traces-weka-062126` 时自动只读挂到 `/data/cc-traces`（参数文件里仍要写
`dataset_dir`，脚本缺了会提醒）；`${HOME}/.cache/agentx/mmap` 会自动挂成 `/data/mmap` 并注入
`AIPERF_DATASET_MMAP_CACHE_DIR`（重建缓存，位置可用 `MMAP_CACHE_DIR` 覆盖）。

```bash
cd runners/agentx
mkdir -p .local
echo '{"dataset_dir":"/data/cc-traces/traces.jsonl","tokenizer":"gpt2","concurrency":8,"benchmark_duration_seconds":1800}' > .local/params.jsonl
DATASET_DIR=~/code/cc-traces-weka-062126 ./run-local.sh --url=http://10.0.0.10:8000/v1 --model_name=Qwen3-32B
```

> 不提供 `dataset_dir` 且没挂语料时会回落到 `--public-dataset` 的 HuggingFace 下载（需要外网），
> 这条退化路径只适合一次性验证。

## 构建

**构建上下文是 Runner 目录本身**（`runners/agentx`），不是仓库根：

```bash
cd runners/agentx
TAG=1.0.1
docker build -f build/Dockerfile -t registry.dev.huawei.com/argus/agentx:${TAG} .
docker push registry.dev.huawei.com/argus/agentx:${TAG}
```

国内/内网（PyPI 或 Docker Hub 不通）：

```bash
docker build -f build/Dockerfile \
  --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.13-slim \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t registry.dev.huawei.com/argus/agentx:${TAG} .
```

镜像约 1.0GB：`agentx-harness` + python 运行时，绘图/调参依赖在**同一层**卸掉（跨层删除不减体积）。需要 `agentx plot` / sweep 时 `--build-arg PRUNE=` 关掉裁剪。tiktoken 词表已预缓存，`--tokenizer builtin` 无外网也能用。

## 自测（不需要镜像、不需要服务）

```bash
# 一键：yaml 契约 + 参数/映射/退出码 + bash entry 端到端 + openapi schema
bash runners/agentx/tests/smoke.sh

# 或者分开跑
py -3.11 runners/agentx/tests/test_runner_yaml.py      # 契约 + 默认值漂移
py -3.11 runners/agentx/tests/test_result_mapping.py   # 参数/命令/指标/退出码/密钥
py -3.11 runners/agentx/tests/validate_result_schema.py .data/agentx-runner/tests/out/result.json

# 容器内冒烟（stub 镜像，不进发布矩阵）
docker build -f runners/agentx/tests/Dockerfile.stub runners/agentx -t agentx-runner:stub
docker run --rm agentx-runner:stub
```

stub 只复现**产物形状**，验证的是 Runner 侧契约；agentx 自身的压测逻辑由真实运行的冒烟覆盖。

## 验收动线（对应 [argus-develop-runner](../../skills/argus-develop-runner/SKILL.md)）

1. `runner.yaml` 逐字段对照 `runners/mock-deploy` 样本；`placeholder` 单行合法 JSONL（`tests/test_runner_yaml.py` 已守）。
2. 重启 argusd → `GET /api/v1/runners` 里出现 `agentx@1.0.1`（K-3：启动期扫描，无运行时注册）。
3. `POST /tasks?dry_run=true` 过 → 小参数真提交（短跑 + 小语料）→ 看 `logs/` 与 `result.json`。

> 内容一变就必须升 `version`（含 description / placeholder / display_name）：注册表按 name+version 钉内容 sha，同版本不同内容会 `RUNNER_VERSION_PINNED`（§5.6.1）。

# agentx：长上下文 agentic 压测

把 SemiAnalysis InferenceX 的真实 Weka agentic coding trace（393 条 Claude Code 会话，含 subagent
分支，[语料](https://inferencex.semianalysis.com/datasets) 1.85 GB）按原时间线回放到被测服务，
输出 TTFT / ITL / 端到端时延 / token 吞吐 / 前缀缓存命中率。只回答「这套服务在真实 agentic 负载下
表现如何」，是否达标由下游判断。

- 角色 `test`，`on_failure: continue`（跑完即出指标，不终止 Task，§7.3 / §7.4）
- 入口 `/runner/run.sh`，契约见 [ARCHITECTURE.md](../../design/ARCHITECTURE.md) §5.2–§5.12、附录 A
- 镜像 `registry.dev.huawei.com/argus/agentx:1.0.6`（约 1 GB）

## 目录结构

```text
agentx/
├── runner.yaml        # 唯一权威：元数据 + entry + 参数 schema + 资源/并发
├── run.sh             # 唯一执行入口（契约面：输入三通道 / 输出目录 / 退出码）
├── agentx_runner.py   # 运行期：参数校验 → 调 agentx → result.json 归一化
├── build/Dockerfile   # 发布镜像（构建上下文 = 本目录）
├── run-local.sh       # 本地便捷脚本（只 run，不 build）
├── tests/             # 维护者自测，不参与注册与调度
└── README.md
```

不放 `job.yaml` / `deployment.yaml`（部署归内置 service-deploy + Genet）、不放 `results/`
（容器内固定写 `/workspace/output`）。

## 跑的是什么

1. **数据集**：用 `--tokenizer` 把每条 trace 重建为 tokenized、带 KV-cache 结构的会话树
   （parent + subagent）；重建结果按 `random_seed` 缓存，命中则秒级恢复。
2. **warmup**：每条 lane 先回放一个深前缀，把服务端 prefix cache 填成真实多轮编码上下文；
   root 会话 warmup 一次失败即整体中止。
3. **profiling**：按录制间隔（end-to-start）继续回放、保留会话前缀以复用 KV cache；并发按
   **session tree** 计（整棵树排空才换下一条），结束后 drain + 导出 `profile_export_*`。

## 怎么调用

业务输入只走输入文件（JSONL，一行一个对象），公共参数走环境变量（§5.3 / §5.4.1）。
读取优先级：`--params-file` > `$AG_PARAMS_FILE` > `/workspace/input/params.jsonl`。

```json
{"concurrency":8,"benchmark_duration_seconds":1800,"dataset_dir":"/data/cc-traces","tokenizer":"/models/Qwen3-32B"}
```

```bash
/runner/run.sh --params-file /tmp/params.jsonl \
  --url=http://10.0.0.10:8000/v1 --model_name=Qwen3-32B
```

| 通道 | 内容 |
|---|---|
| 输入文件 | 全部业务参数；未声明字段**拒绝**（报行号），多行时后者覆盖前者 |
| 公共参数 | `AG_SERVED_URL` / `AG_SERVED_MODEL_NAME`（独立运行可用 `--url` / `--model_name` 覆盖） |
| 元信息 | `AG_TASK_ID` / `AG_ATTEMPT` / `AG_RUNNER_SEQ`（缺省 `local` / `1` / `1`） |
| 凭据 | `ENDPOINT_API_KEY`、`HF_TOKEN`（Task 级 secrets 注入，§12.4） |
| 输出目录 | `/workspace/output`（`AG_OUTPUT_DIR` 可覆盖）：`result.json`、`agentx/`、`logs/`、`raw/` |

API key 以 `--api-key '${ENDPOINT_API_KEY}'` 的**引用**形式进 argv，明文不出现在 `ps`、日志或
`result.json.metadata.command` 里。`AG_SERVED_URL` 原样交给 agentx，写 `http://host:8000/v1` 即可
（不会拼出 `/v1/v1/...`）。

## 参数

平台只校**类型**与 **required**；范围/枚举/互斥由 Runner 校验，参数错误一律退出码 2。

| 参数 | 默认 | 说明 |
|---|---|---|
| `concurrency` | `8` | 并发 session tree 数；subagent 并行时在飞请求可超过它 |
| `benchmark_duration_seconds` | `1800` | 测量时长；**< 900 必须配 `allow_short_run=true`** |
| `public_dataset` | `semianalysis_cc_traces_weka_062126` | date-pinned 公开语料别名（滚动别名不可比） |
| `dataset_dir` | `""` | **本地语料目录**（`traces.jsonl` 的父目录）；设了就走离线路径，见下节 |
| `num_dataset_entries` | `0` | 只取前 N 条 trace（0 = 全量）。仅冒烟，改变负载但不改 `submission_valid` |
| `max_context_length` | `0` | 客户端上下文过滤（0 = 不过滤），填服务真实上限 |
| `random_seed` | `42` | 起始 turn / 回绕顺序 / 重建缓存 key；固定才能复现与复用缓存 |
| `tokenizer` | `""` | 容器内绝对路径（生产推荐）/ HF 仓库名 / `builtin`；本地路径不存在会预检失败 |
| `scenario` | `agentx` | `agentx`（3600 s、0~100% 起始窗口）或 `inferencex-agentx-mvp`（1800 s、25%~75%） |
| `warmup_requests_per_lane` | `0` | 每 lane 额外预热请求数；与下一项互斥 |
| `agentic_cache_warmup_duration` | `0` | 时长制缓存预热（秒，0 = 关）；与上一项互斥 |
| `allow_short_run` | `false` | 放行 < 900 s 的短跑，结果固定 `submission_valid=false` |
| `hf_cache_dir` | `""` | 公开语料路径下的 HF 缓存目录，缺省 `<output>/.cache/hf` 且跑完删除 |
| `keep_cache` | `false` | 保留 `<output>/.cache`（算进产物配额，仅排查用） |
| `agentx_cmd` | `agentx` | 替换命令（离线测试指 `tests/stub_agentx.py`） |
| `extra_args` | `""` | 透传未建模的开关，如 `--use-server-token-count` |

## 语料：挂载本地目录（容器内不联网）

镜像里没有语料。把下载好的 HuggingFace 仓库目录挂进来，`dataset_dir` 给**它的父目录**（不是
`traces.jsonl` 文件路径）：

```bash
docker run --rm \
  -v ~/code/cc-traces-weka-062126:/data/cc-traces \
  -v /host/weights/Qwen3-32B:/models/Qwen3-32B:ro \
  -v /tmp/out:/workspace/output \
  -v /tmp/params.jsonl:/workspace/input/params.jsonl:ro \
  registry.dev.huawei.com/argus/agentx:1.0.6 \
  /runner/run.sh --url http://10.0.0.10:8000/v1 --model_name Qwen3-32B
```

```json
{"concurrency":8,"benchmark_duration_seconds":1800,"dataset_dir":"/data/cc-traces","tokenizer":"/models/Qwen3-32B"}
```

Runner 在 `dataset_dir` 下按顺序找语料：

| 目录内容 | 行为 |
|---|---|
| 顶层已有「一条 trace 一个 `*.json`」 | 认为已拆好，直接用（不复制、不拆分，可只读挂载） |
| 只有 `traces.jsonl` | 拆到 `<dataset_dir>/.agentx-split/`；**拆分只付一次** —— 下次校验该目录的 `manifest`（源文件路径 + size + mtime + 拆分版本 + 条数）命中即跳过 |
| 两者都没有 | 参数预检直接失败（退出码 2） |

所以**语料目录要可写**（示例里没有 `:ro`）；只想只读挂载就预先在目录下放好拆好的 `*.json`。
源文件被替换/重新下载（size 或 mtime 变）或拆分逻辑升级会自动重拆，不会拿旧文件凑数。

其它约束：

- 走的是 `--custom-dataset-type weka_trace --input-file`，**不是** `--public-dataset`：Runner 会自动补
  `--unsafe-override`，结果**固定 `submission_valid=false`**，不可与其他 AgentX 结果对比或提交。
- `tokenizer` 三种写法：**容器内绝对路径**（生产推荐，如 `-v /host/weights/Qwen3-32B:/models/Qwen3-32B:ro`
  + `"tokenizer":"/models/Qwen3-32B"`）、HF 仓库名（配 `-e HF_HOME` 指到已缓存目录）、
  `builtin`（tiktoken 零依赖，仅冒烟用）。本地路径不存在会在参数预检阶段直接失败。
- 镜像默认 agentx-harness **1.0.14**（`--build-arg AGENTX_VERSION=` 可覆盖）；本地 tokenizer 路径与
  `HF_HUB_OFFLINE` 兼容（1.0.7 的离线分支会把路径当 repo id，已在新版修掉）。
- 镜像里那份 tiktoken 词表只服务 `builtin`：构建时下载不到只会打警告，不影响传本地路径；
  确定不用 builtin 就 `--build-arg TIKTOKEN_CACHE=0` 跳过下载。
- 平台内：Execution Profile 把语料 PVC 挂到 `/data/cc-traces`（可写）；`/tmp` 只有 256 MiB，别放那儿。
- 要「可对比 / 可提交」的结果必须走 `public_dataset`，它读 HuggingFace datasets 缓存
  （`<HF_HOME>/hub` + `<HF_HOME>/datasets`）；只挂 `hub/` 时离线会报
  `Couldn't reach ... (OfflineModeIsEnabled)`。

## 指标

`name` 只放指标，`statistic` / `unit` 走字段，维度进 `labels`（当前只有 `concurrency`）。取不到的
指标不写（不编数）。

| 指标 | 单位 | statistic |
|---|---|---|
| `ttft` / `request_latency` | `ms` | mean、p50、p90、p99、max |
| `inter_token_latency` | `ms` | mean、p90、p99 |
| `time_to_second_token` | `ms` | mean |
| `request_throughput` | `req_per_s` | mean |
| `output_token_throughput` / `total_token_throughput` / `output_token_throughput_per_user` | `tokens_per_s` | mean |
| `input_sequence_length` / `output_sequence_length` | `tokens` | mean |
| `prefix_cache_hit` | `percent` | mean（AgentX 核心特征） |
| `request_count` | `count` | count |
| `benchmark_duration` | `s` | max |

`submission_valid` 与 `submission_invalid_reasons` 落在 `metadata` 与 `notes`：它是**有效性戳，不是
执行结果**——`false` 不会让本 Runner 失败（§5.8）。

## 退出码

| 码 | 含义 | `error.code` |
|---|---|---|
| `0` | agentx 跑完且产物可解析 | — |
| `1` | agentx 非零退出 / 产物缺失或损坏 / 被取消 | `RUNNER_NONZERO_EXIT` / `RESULT_INVALID` / `CANCELED` |
| `2` | 参数或输入错误 | `VALIDATION_ERROR` |

错误码只用附录 D 枚举：自造码会被平台静默降级成 `RUNNER_NONZERO_EXIT`。

## 部署约束

| 项 | 说明 |
|---|---|
| 资源 | `4c / 8Gi`（重建按 CPU 并行）；2c/2Gi 能跑但明显更慢 |
| 独占 | `concurrency.endpoint_exclusive: true`：并行测量会污染吞吐与前缀命中率（§6.7） |
| 超时 | 冷启动 + warmup + `benchmark_duration_seconds` + drain；建议 `timeout_seconds ≥ duration + 1800`，正式 1800 s 跑建议 ≥ `5400` |
| 非 root | 以 uid 10001 运行（PSA restricted）；挂载属主由平台 `fsGroup` 或 `-u` 保证 |
| 出网 | 语料与 tokenizer 都由挂载提供时不需要外网；但**本地 tokenizer 路径不要配 `HF_HUB_OFFLINE`**（见语料一节） |
| 内部别名 | 服务端模型名不是可解析的 HF 仓库时**必须**给 `tokenizer` |

## 独立运行

```bash
export IMAGE=registry.dev.huawei.com/argus/agentx@sha256:...
echo '{"concurrency":8,"benchmark_duration_seconds":60,"allow_short_run":true,"dataset_dir":"/data/cc-traces","tokenizer":"/models/Qwen3-32B"}' > /tmp/params.jsonl
docker run --rm \
  -v ~/code/cc-traces-weka-062126:/data/cc-traces \
  -v /host/weights/Qwen3-32B:/models/Qwen3-32B:ro \
  -v /tmp/out:/workspace/output \
  -v /tmp/params.jsonl:/workspace/input/params.jsonl:ro \
  "${IMAGE}" \
  /runner/run.sh --url http://10.0.0.10:8000/v1 --model_name Qwen3-32B
jq . /tmp/out/result.json
```

或 `run-local.sh`：`.local/params.jsonl` 自动挂成输入文件；设 `DATASET_DIR=~/code/cc-traces-weka-062126`
自动挂到 `/data/cc-traces`（可写）（参数里仍要写 `dataset_dir`，缺了会提醒）；`~/.cache/agentx/mmap`
自动挂成重建缓存（`MMAP_CACHE_DIR` 可换）。

```bash
cd runners/agentx && mkdir -p .local
echo '{"dataset_dir":"/data/cc-traces","tokenizer":"builtin","concurrency":8}' > .local/params.jsonl
DATASET_DIR=~/code/cc-traces-weka-062126 ./run-local.sh --url=http://10.0.0.10:8000/v1 --model_name=Qwen3-32B
```

## 构建

构建上下文是 Runner 目录本身，不是仓库根：

```bash
cd runners/agentx
TAG=1.0.6
docker build -f build/Dockerfile -t registry.dev.huawei.com/argus/agentx:${TAG} .   # 国内/离线加下面的 build-arg
#   --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.13-slim
#   --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
#   --build-arg TIKTOKEN_CACHE=0        # 不用 builtin 时跳过词表下载（连不上 openaipublic 也不会失败）
docker push registry.dev.huawei.com/argus/agentx:${TAG}
```

约 1 GB：绘图/调参依赖在**同一层**卸掉（跨层删除不减体积）；需要 `agentx plot` / sweep 时
`--build-arg PRUNE=` 关掉裁剪。tiktoken 词表已预缓存。

## 自测与验收

```bash
bash runners/agentx/tests/smoke.sh                        # yaml + 参数/映射/退出码 + bash entry + openapi schema
docker build -f runners/agentx/tests/Dockerfile.stub runners/agentx -t agentx-runner:stub
docker run --rm agentx-runner:stub                        # 容器内冒烟（stub 不进发布矩阵）
```

1. `runner.yaml` 对照 `runners/mock-deploy`；`placeholder` 单行合法 JSONL（`tests/test_runner_yaml.py` 守）。
2. 重启 argusd → `GET /api/v1/runners` 出现 `agentx@1.0.6`（K-3：启动期扫描，无运行时注册）。
3. `POST /tasks?dry_run=true` 过 → 小参数真提交（短跑 + 小语料）→ 看 `logs/` 与 `result.json`。

> 内容一变就必须升 `version`（含 description / placeholder / display_name），否则同版本不同内容会
> `RUNNER_VERSION_PINNED`（§5.6.1）。

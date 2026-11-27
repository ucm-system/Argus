# aisbench-prefix：前缀缓存性能测量（并发阶梯 + 命中率）

回答一个问题：**给定负载形态（输入/输出长度、前缀重复率、数据条数），这套服务在各并发下的
TTFT / TPOT / E2E / 吞吐 / 前缀缓存命中率是多少？**

- 角色 `test`，`on_failure: continue`：单个并发点抖动不影响其余点的结论
- 入口 `/runner/run.sh`（exec form，§5.2）
- 独占 endpoint（`concurrency.endpoint_exclusive: true`）：压测会打满服务，且命中率取决于
  「此刻 KV cache 里有什么」，与任何其他测量并行都会互相污染（§6.7）
- 契约：`design/ARCHITECTURE.md` §5.3、§5.4.1、§5.8、§6.7、§12.4、附录 A
- 被测工具：**`aisbench_auto_tools_prefix`**（vendored 在 [`tool/`](tool/)，
  commit 与逐条本地改动见 [`tool/VENDORED.md`](tool/VENDORED.md)）

## 目录结构

```text
aisbench-prefix/
├── runner.yaml            # 契约声明（32 个参数 + env + 资源/独占）
├── run.sh                 # 唯一执行入口：参数映射 → 逐并发点跑工具 → 交给 summarize.py
├── summarize.py           # sidecar 事件 → result.json（附录 A schema 2.0）
├── build/Dockerfile       # **唯一发布镜像**（多架构，pip 装 ais-bench-benchmark）
├── run-local.sh           # 本地便捷脚本（只 run，不 build）
├── tool/                  # vendored 上游 CLI + 本地改动
│   ├── aisbench_test.py   #   压测驱动（上游）
│   ├── generate_dataset.py#   数据集生成（上游，改了 seed/状态/退出码）
│   ├── data_picker.py     #   GSM8K 挑题（上游，未改）
│   ├── save_file.py       #   汇总表解析 → aisbench_result.csv（上游，未改）
│   ├── cal_prefix_hit_rate.py # /metrics 抓取（上游，未改）
│   ├── config.py          #   **改写**：每一项都可被 AGP_* 环境变量覆盖
│   ├── prefix_env.py      #   **新增**：env 桥接 / 寻址归一化 / sidecar / 退出码
│   ├── default_api.py     #   ais_bench 模型配置模板（改了 api_key 不落盘）
│   ├── GSM8K.jsonl        #   题库（约 1300 行）
│   └── VENDORED.md        #   来源 commit、blob 校验表、12 条本地改动
├── tests/
│   ├── smoke.sh           # 离线冒烟（**唯一入口**，9 组断言 36 项）
│   ├── Dockerfile.stub    # 冒烟镜像（不进发布矩阵）
│   ├── stub_aisbench.py   # 假 ais_bench（读模型配置、打印可解析的汇总表、推流量）
│   ├── mock_endpoint.py   # 假服务端（/v1/models、/metrics 累计计数器、/__sim__/traffic）
│   ├── fakepkgs/transformers/ # 逐字符假 tokenizer（让数据集生成链路毫秒级跑完）
│   ├── test_dataset_determinism.py # seed 可复现 / picked_ids / 失败必须非零
│   ├── test_summarize.py  # sidecar → result.json 映射（含哨兵值、缺指标说明）
│   ├── test_prefix_env.py # 寻址归一化（host_ip vs url、/v1 尾斜杠）
│   ├── validate_result_schema.py # 按 openapi `Result`（含 ErrorCode enum）校验
│   └── fixtures/smoke_prefix.jsonl
└── README.md
```

**这里没有、也不该有**：`job.yaml`（部署由平台渲染，§6.8）、凭据（只走 env，§12.4）、
构建期 clone 上游仓库（§6.2）。

## 三个反馈点的正面回答

### 1）「要改 config.py，是不是就得迁到命令行？」——是，但不是全走命令行

上游用法是「编辑 `config.py` → 跑 `python aisbench_test.py --input_len …`」。
Runner 的根文件系统只读（`ReadOnlyRootFilesystem`，只有 `/workspace/output` 与 `/tmp` 可写），
**改不了文件**，所以按「谁产生这个值」把 `config.py` 劈成两半：

| `config.py` 里的项 | 迁移到哪 | 由谁给 |
|---|---|---|
| `HOST_IP` / `HOST_PORT` / `URL` | env `AGP_URL`（← 平台公共参数 `AG_SERVED_URL`） | 平台注入 |
| `MODEL_NAME` | env `AGP_MODEL_NAME`（← `AG_SERVED_MODEL_NAME`） | 平台注入 |
| `MODEL_PATH` | env `AGP_MODEL_PATH`（← `AG_MODEL_PATH`，D-63 统一共享挂载） | 平台注入 |
| `API_KEY` | env `ENDPOINT_API_KEY`（Task 级 secrets，§12.4） | 调用方 |
| `DATASET_PATH` | env `AGP_DATASET_PATH`（← 输入字段 `dataset_path`） | 输入文件 |
| `WORK_PATH` | env `AGP_WORK_PATH`（← 输入字段 `work_path`，默认 `/tmp/prefix-ws`） | 输入文件 |
| `OUTPUT_DIR` | env `AGP_OUTPUT_DIR`（= 该并发点的产物目录） | run.sh 决定 |
| `DEFAULT_PERFORMANCE_TEST` | env `AGP_SUMMARIZER`（← 输入字段 `summarizer`） | 输入文件 |
| `POD_INFO` | env `AGP_POD_INFO`（← 输入字段 `pods`；缺省用被测服务 host:port） | 输入文件 |
| 负载参数（`--input_len` 等 20 个） | **保持命令行**（上游本来就有，run.sh 逐项映射） | 输入文件 |

做法是把 `tool/config.py` 改写成「常量 = `环境变量 or 上游默认值`」，
`aisbench_test.py` 的 `from config import *` 原样可用——**没有重写工具**，只换了取值来源。
（改前：`/benchmark/...` 这种站点路径写死在文件里；改后：文件只留默认值，实际值全从 env 来。）

### 2）「aisbench 位置、数据集路径定死就好？」——给了默认值，但不写死

- `work_path`（ais_bench 源码根）：**默认 `/tmp/prefix-ws`**，可覆盖。
  不写死成 `/benchmark` 的原因是硬的：平台 Job 根文件系统只读，`/benchmark` 属 root 0755，
  写进去必然失败（`aisbench-sla` 同样结论）。run.sh 会自动 mkdir 并做可写探测，
  探测不过就 `failed`（不是打一行 log 硬跑）。
- `dataset_path`：**默认 `<output>/prefix_out/datasets`**（在产物目录里，会被平台收集、
  每点一个子目录），可指到 Profile 挂载的共享存储以便跨 attempt 复用。
- `model_path` / `AG_SERVED_*`：公共参数，平台注入，**不进输入文件**（§5.4.1）。
- 一句话：**该钉的钉在 Runner 默认值上（有理由、可覆盖），不该钉的（服务地址、权重路径）
  本来就不属于镜像。**

### 3）「生成数据集失败，请清空 picked ids」——Runner 自动清理，并且不再假成功

上游的 `DataPicker` 用 `picked_ids.txt` 记住「哪些 GSM8K 行已经当过前缀」，
文件写在 **CWD** 且**只增不减**：跑够次数后题库被自己标满 → `pick_one()` 返回 `None`
→ 打一行 `ERROR 生成数据集失败，请清空picked ids` → 然后 **`exit(0)`**。
人工「删文件能解决」，但下一次还会再犯；而 `exit(0)` 在 Runner 里更糟——
Task 会记成 `succeeded`，零指标零结论。

本 Runner 三件事一起做：

1. 记录路径改到 `AGP_STATE_DIR`（默认 `/tmp/prefix-state`），**不再跟 CWD 走**；
2. 每次生成数据集前清掉它（输入字段 `keep_pick_state: true` 可保留，见下面权衡）；
3. 真挑不出行时以 **`SystemExit(3)`** 失败，消息写明题库路径、记录路径与容量
   （`GSM8K.jsonl` 约 1300 行，`prefix_num`/`data_num` 过大才是根因），
   映射成 `failed` + `RUNNER_NONZERO_EXIT`。

冒烟用例 C 就是这条的红/绿证据：把记录填满是 → 默认清理照样成功；
`keep_pick_state: true` → 退出码 1（**不是 0**）且日志写明原因。

### 4）「原先不支持设置 seed，无法复现」——补上了，并加了断言

上游 `--seed` **只**喂给「前缀后插入的 3 个随机 token」（`generate_unique_tokens()` 里才
`random.seed()`）；GSM8K 挑题走 `DataPicker.pick_one()` 里未播种的全局
`random.choice/randint`。所以同一份参数两次跑，前缀与后缀的行都不一样——
所谓「不能复现」就在这。

改后（`tool/VENDORED.md` 第 4 条）：数据集生成入口先 `random.seed(seed)`，
配合默认清理挑题记录（跨次累积本身就是不可复现的第二个来源），得到：

```text
同参数 + 同 seed  ⇒  生成的数据集逐字节相同（前缀文件与完整数据集都验 sha256）
换 seed          ⇒  数据集必然变化（否则说明 seed 没吃到挑题路径）
seed = 0         ⇒  不播种，保留「每次都要新数据」的语义（此时不可复现，是用户选择）
```

阶梯里默认 `seed_per_point: true`：第 i 个点用 `seed + i`，各点前缀互不相同——
否则后一个点跑在前一个点已经焐热的 cache 上，命中率会逐点偏高、不可比。

## 参数映射：输入文件 / 公共参数 → 工具

业务参数只走输入文件（JSONL，§5.3）；`url` / `model_name` / `model_path` 是公共参数，
走 `AG_SERVED_URL` / `AG_SERVED_MODEL_NAME` / `AG_MODEL_PATH`（独立运行可用同名 CLI，§5.10）。

| 输入字段 | 工具参数 / env | 说明 |
|---|---|---|
| `concurrency`（整数数组） | `--concurrency`（逐点） | **必填**。多值 = 同一 Job 内按顺序逐点压；每点一个产物目录 `points/point-<c>/` |
| `input_len` / `output_len` / `data_num` | `--input_len` / `--output_len` / `--data_num` | 负载形态 |
| `request_rate` | `--request_rate` | 0 = 不限速（测并发上限必须 0） |
| `dataset_type` / `prefix_num` / `repeat_rate` / `dp` | 同名 `--…` | 前缀形态；`repeat_rate` 支持 `90%` 或 `0.9` |
| `prefix_test` | `--prefix_test` | 关掉就**没有命中率**（上游只在开启时抓 `/metrics` 快照） |
| `seed` / `seed_per_point` | `--seed`（第 i 点用 `seed+i`） | 见上面「可复现」 |
| `dataset` | `--dataset` | 现成 gsm8k 数据集；给了就跳过生成（`seed_per_point` 自动视为 false） |
| `dataset_path` | env `AGP_DATASET_PATH` | 默认 `<output>/prefix_out/datasets`，每点一个子目录 |
| `work_path` | env `AGP_WORK_PATH` | 影子源码根，默认 `/tmp/prefix-ws`，必须可写 |
| `summarizer` | env `AGP_SUMMARIZER` | `default_perf` \| `stable_stage`（上游 `DEFAULT_PERFORMANCE_TEST`） |
| `pods` | env `AGP_POD_INFO` | `/metrics` 快照节点；缺省用被测服务的 `host:port` |
| `test_type` / `enable_think` / `test_accuracy` / `repeat` / `npu_num` | 同名 `--…` | 上游原有开关 |
| `length_mean` / `length_std` / `length_min` / `length_max` | 同名 `--…` | 变长负载（mean/std 成对、min/max 成对） |
| `num_warmups` | env `AGP_NUM_WARMUPS` | 留空 = 不传 `--num-warmups`（上游 FAQ #3 的免改码解法） |
| `aisbench_cmd` / `aisbench_extra_args` | env `AGP_AISBENCH_CMD` / `AGP_AISBENCH_EXTRA_ARGS` | 换命令 / 透传未建模开关；run.sh 会自动追加 `--config-dir` |
| `run_timeout` | env `AGP_RUN_TIMEOUT` | 单点超时，按进程组终止 |
| `keep_pick_state` | env `AGP_RESET_PICK_STATE`（取反） | 默认清理挑题记录 |
| `no_stream` | （run.sh 行为） | true 时不实时打屏，仍完整落 `logs/prefix.log` |
| env `ENDPOINT_API_KEY` | 生成的模型配置里写 `api_key=_API_KEY` | 凭据**只走 env**：不进 argv、不落日志、明文不落盘 |

**没有映射的上游开关**：`--test_accuracy` 的精度分数解析（工具本身不解析，见「已知限制」）。
`dataset_source`（随机词表模式）在上游 `main` 里还不存在，见 `tool/VENDORED.md`。

## work_path 接线（为什么 `/tmp` 下的目录能被 ais_bench 认到）

工具往 `work_path` 下写两样东西：

```text
<work_path>/ais_bench/benchmark/configs/models/vllm_api/vllm_api_chat_temp.py   （模型配置软链）
<work_path>/ais_bench/datasets/gsm8k/{train.jsonl,test.jsonl}                    （数据集软链）
```

上游靠「`WORK_PATH` 恰好就是 ais_bench 源码根」让它们生效；平台上不可能写 `/benchmark`，
所以 run.sh 接两根线（都在本仓库的 `run.sh` 里，没改 vendored 的调用逻辑）：

1. **数据集**：`export AIS_BENCH_DATASETS_CACHE=<work_path>`。ais_bench 把**相对**数据集路径
   拼在这个环境变量下（`ais_bench/benchmark/datasets/utils/datasets.py: get_cache_dir()`），
   于是配置里的 `path='ais_bench/datasets/gsm8k'` 命中工具写的目录。
2. **模型配置**：`ais_bench --config-dir <work_path>/ais_bench/benchmark/configs`。
   `config_manager.py` 的搜索顺序是 **`--config-dir` 优先、内置 configs 兜底**
   （`_search_models_config` / `_search_datasets_config` 里各给了两个目录），
   所以影子根只补 `vllm_api_chat_temp.py`，`gsm8k_gen_0_shot_cot_str_perf` 与
   `default_perf`/`stable_stage` 汇总器仍从包内置配置解析——两根线互不影响。

## 镜像：不用官方镜像，自己打多架构

官方 `ghcr.io/aisbench/aisbench_benchmark` 的 tag 是**单架构各一份**
（命名规则 `{tag}-{OS}-{py}-{arch}`，见上游 `docker/OVERVIEW.md`），不是 manifest list。
而 `.github/workflows/release.yml` 对每个 Runner 镜像都要求清单里同时有
`linux/amd64` 与 `linux/arm64`（"不许静默降级成单架构"）——FROM 单架构基镜像的构建会在那一步直接红
（`aisbench-sla` 现在就是这个状态，它是既有事实、不改）。

所以 `build/Dockerfile` 参考上游 `docker/ubuntu/Dockerfile.py310.ubuntu22.04` 自己打，但更省：

```text
python:3.10-slim-bookworm                    # 多架构官方基础镜像
  + pip install torch        （CPU 版，--index-url download.pytorch.org/whl/cpu）
  + pip install ais-bench-benchmark==3.1.20260630   # PyPI wheel 是 py3-none-any，架构无关
```

要点：

- **为什么不用官方镜像**：本 Runner 只走 ais_bench 的 API 压测路径（HTTP 打被测服务），
  官方镜像里那套 NPU/CANN/mindie 栈是本地模型评测用的，6.4 GB 白背。
- **为什么先装 torch**：PyPI 上 x86 的 torch wheel 会拖 CUDA 全家桶（数 GB）；
  先装 CPU 版后，ais_bench 的依赖检查直接满足。镜像约 1.5 GB 量级。
- **arm64 可行性**：依赖里唯一需要预编译产物的 `tree_sitter_languages` 在 PyPI 上有
  `manylinux_2_17_aarch64` wheel（cp310/311/312 都有），所以 arm64 侧不需要源码编译。
  （CI 里 arm64 走 qemu，构建时间明显长于 amd64——这是双架构的固有代价。）
- **版本钉死**（§6.2）：`ARG AIS_BENCH_VERSION=3.1.20260630`，禁止 latest；
  镜像源可用 `ARG PIP_INDEX_URL` / `ARG TORCH_INDEX_URL` 覆盖给内网构建。
- 构建期会自检：`ais_bench --help` 能跑、`__version__` 以 `3.1` 开头、
  非 root（uid 10001）可写 `/tmp/prefix-ws` 与 `/workspace/output`。

## 退出码与 result.json 映射

> 产物契约（D-74）：`result.json` / `result.html` / `result.csv` **至少其一**，本 Runner 选 `result.json`。

| 场景 | Runner exit | `status` | `error.code` |
|---|---|---|---|
| 至少一个并发点跑通（其余点失败仍可） | `0` | `succeeded` | — |
| 本脚本参数错误（缺 `concurrency`、未声明字段、类型/枚举不对、`--help`） | `2` | `failed` | `VALIDATION_ERROR` |
| 被测服务 TCP 连不上 | `1` | `failed` | `ENDPOINT_UNREACHABLE`（`retryable: true`） |
| 工具退出码 3：数据集生成失败（题库容量、tokenizer 空文本） | `1` | `failed` | `RUNNER_NONZERO_EXIT` |
| 所有并发点都失败（工具退出码 4：ais_bench 非零） | `1` | `failed` | `RUNNER_NONZERO_EXIT` |
| 某点超时（`run_timeout`）且没有成功点 | `1` | `failed` | `RUNNER_TIMEOUT`（`retryable: true`） |
| 工具退出码 0 但 sidecar 一条指标都没有 | `1` | `failed` | `RESULT_INVALID` |
| `work_path` / `dataset_path` 建不出来或不可写 | `1` | `failed` | `RUNNER_NONZERO_EXIT` |

> 错误码只用平台码表里有的（`server/internal/domain/codes.go` + openapi `ErrorCode`）：
> 自造一个码不会报错，而是被平台**静默降级成 `RUNNER_NONZERO_EXIT`**、丢掉区分信息。
> 本 Runner 因此**不新增码**（要加 `PREFIX_*` 得先改 `ARCHITECTURE.md` §15 再改契约与码表）。
> `tests/validate_result_schema.py` 会按 openapi 的 `ErrorCode` enum 卡住这一点。

## result.json 里的 metrics（口径与来源）

数据全部来自工具写的 sidecar（`points/point-*/events.jsonl`，`kind=metrics` / `hit`），
**不解析打屏表格**。`get_data()` 抠不到某项时留的是 `DEFAULT_METRICS` 的哨兵值
（99999 / 9999）——本 Runner 一律按「缺失」处理：不写该指标，并在 `notes` 的
「未写入的指标」里点名（不编数）。

指标名只放指标本身：统计量走 `statistic`、单位走 `unit`、维度走 `labels`
（附录 A / openapi `ResultMetric`：`name` 不得编码维度）。

| `metrics[].name` | CSV/工具字段 | `unit` | `statistic` | `labels` |
|---|---|---|---|---|
| `ttft` | `TTFT avg` / `TTFT P90` | `ms` | `mean` / `p90` | `concurrency`, `phase` |
| `tpot` | `TPOT avg` / `TPOT SLO_P90` | `ms` | `mean` / `p90` | 同上 |
| `e2el` | `E2EL avg` / `E2EL P90` | `ms` | `mean` / `p90` | 同上 |
| `output_throughput` | `output_throughput` / `single_output_throughput` | `tokens_per_s` | `mean` | `concurrency`, `phase`, `scope=total\|per_device` |
| `total_token_throughput` | `E2E_throughput` / `single_E2E_throughput` | `tokens_per_s` | `mean` | 同上 |
| `input_token_throughput` / `prefill_token_throughput` | 同名字段 | `tokens_per_s` | `mean` | `concurrency`, `phase` |
| `request_throughput` | `qps` | `req_per_s` | `mean` | 同上 |
| `measured_concurrency` | `cc`（实测均值） | `count` | `mean` | 同上 |
| `target_concurrency` | `max_cc` | `count` | `count` | 同上 |
| `requests_total` | `total_req` | `count` | `count` | 同上 |
| `hit_rate` | `/metrics` 差分（`ALL_PODS`） | `percent` | `mean` | `concurrency`, `phase`, `scope=hbm\|external` |

- `phase=prefix` 的行是**预热阶段**自己的统计（并发 = `dp`，`dp=1` 时 = `prefix_num`），
  只作证据；判定用的数看 `phase=full`。
- `hit_rate` 只在 `prefix_test: true` 且目标服务暴露
  `vllm:prefix_cache_{queries,hits}_total` 时才有；否则不写并在 `notes` 说明原因
  （`prefix`/`external` 两组计数器都会给，用 `labels.scope` 区分）。
- `sample_size` 取 `total_req`（延迟/吞吐类都带）。
- **不写**的字段：`E2E_time`（上游按 ms 读又除 1000，口径存疑）、`qpm`（= qps×60，冗余）
  ——它们仍在 `aisbench_result.csv` 里。
- `workload` 带公共参数 + 本次形态（最后一个点的 `concurrency` 与 `seed` 等）；
  `metadata` 带 `tool` / `tool_version` / vendored commit / `aisbench_version`。

## 产物

```text
/workspace/output/
├── result.json                        # 本 Runner 产物（D-74 三选一）
├── logs/prefix.log                    # Runner 时间线 + 各点工具/ais_bench 输出
└── prefix_out/
    ├── datasets/{c<N>|shared}/*.jsonl # 生成的数据集（前缀文件 + 完整数据集）
    └── points/point-<N>/
        ├── events.jsonl               # sidecar（summarize.py 的唯一数据源）
        ├── aisbench.log               # 该点的工具 + ais_bench 日志（上游落盘）
        ├── aisbench_all.log           # 同上（上游累积副本）
        ├── aisbench_result.csv        # 上游结果表（给人看的证据）
        ├── temp_api.py                # 该点的 ais_bench 模型配置（不含凭据）
        └── work/<时间戳>/{configs,logs,performances,results}/  # ais_bench --work-dir
```

`result.json.artifacts` 按附录 A 列出（`{name, path, size_bytes, sha256}`，
`path` 相对 `/workspace/output`）。**单文件超 50 MB 保留末尾**并标 `truncated: true`；
**条目超过 300 个**时后续文件不再列进 artifacts（内容仍在产物目录，只是不逐个哈希），
两种情况都写进 `notes`。`datasets/**` 不进 artifacts（可能很大，且可用 `dataset` 复用）。

## 构建与推送（只发生在 CI）

构建上下文是 **Runner 目录本身**（`runners/aisbench-prefix`）：

```bash
docker build -f runners/aisbench-prefix/build/Dockerfile \
  -t ghcr.io/ucm-system/argus-runner-aisbench-prefix:0.1.2 \
  runners/aisbench-prefix

# 多架构（= release.yml 的做法；arm64 走 qemu，慢）
docker buildx build --platform linux/amd64,linux/arm64 --push \
  -f runners/aisbench-prefix/build/Dockerfile \
  -t ghcr.io/ucm-system/argus-runner-aisbench-prefix:0.1.2 runners/aisbench-prefix

# 关键断言：镜像里真有 ais_bench，且跑在 uid 10001
docker run --rm <img> bash -lc 'command -v ais_bench && ais_bench --help >/dev/null && id'
```

**平台、SSH 主机、本地一律只 pull + run**（§6.2）。灌进 kind 节点用
`deploy/kind/load-image.ps1`（Git Bash 里 kind.exe 不可用）。

## 平台上提交（Task）

```yaml
task:
  name: prefix-cache-2k
  service:                      # deploy/service 二选一（§5.4.1）
    url: http://vllm-demo.argus-mock.svc:8000/v1
    model_name: Qwen3-32B
  precheck: auto
  runners:
    - name: prefix-sweep
      runner: aisbench-prefix
      version: "0.1.0"
      execution: { profile: k8s-npu }
      on_failure: continue
      input: >-
        {"concurrency":[8,16,32],"input_len":2048,"output_len":1024,"data_num":128,
         "request_rate":0,"dataset_type":"prefix_cache","repeat_rate":"90%",
         "prefix_num":1,"dp":1,"prefix_test":true,"seed":1,"seed_per_point":true,
         "run_timeout":1800}
```

- `model_path` 是公共参数（走 `AG_MODEL_PATH`），**不进 `input`**；
  外接模式（`service:`）的 `AG_MODEL_PATH` 来源同 `aisbench-sla` 的说明——
  平台侧还没在契约里定死，需要 tokenizer 时先靠统一共享挂载（D-63）约定。
- 要鉴权就在 Task 的 `secrets` 里给 `ENDPOINT_API_KEY` 的引用名（`env` 声明里是 secret）。
- 阶梯也可以拆成多个 runner 条目（每点一条执行行），代价是每点重新生成数据集、
  且**同一服务实例上重复预热会让命中率不可比**——默认建议一个 runner 条目 + 数组。

## 独立运行（脱离平台，§5.10）

```bash
echo '{"concurrency":[16],"input_len":2048,"output_len":1024,"data_num":64,
       "request_rate":0,"dataset_type":"prefix_cache","repeat_rate":"90%"}' > /tmp/params.jsonl
docker run --rm --add-host=host.docker.internal:host-gateway \
  -e AG_TASK_ID=local -v /tmp/prefix-out:/workspace/output \
  -v /tmp/params.jsonl:/workspace/input/params.jsonl:ro \
  ghcr.io/ucm-system/argus-runner-aisbench-prefix:0.1.2 \
  /runner/run.sh --url http://host.docker.internal:8000/v1 \
    --model_name Qwen3-32B --model_path /models/Qwen3-32B
```

## 离线冒烟（不需要真 ais_bench / 权重 / 网络）

```bash
# 1) 纯 python 单测（本机就能跑，不需要 docker）
py -3.11 runners/aisbench-prefix/tests/test_prefix_env.py
py -3.11 runners/aisbench-prefix/tests/test_dataset_determinism.py
py -3.11 runners/aisbench-prefix/tests/test_summarize.py

# 2) 容器里的全链路冒烟（唯一入口；同一条命令也能在任意装了 pandas/requests 的 Linux 里跑）
docker build -f runners/aisbench-prefix/tests/Dockerfile.stub \
  -t ghcr.io/ucm-system/argus-runner-aisbench-prefix:0.1.2-stub runners/aisbench-prefix
docker run --rm --entrypoint bash \
  ghcr.io/ucm-system/argus-runner-aisbench-prefix:0.1.2-stub /runner/tests/smoke.sh
```

| 用例 | 构造 | 断言 |
|---|---|---|
| A 成功路径 | 生成数据集 + 阶梯 `[2,4]` + 假 ais_bench + 假服务端 | exit 0、`status=succeeded`、两点指标、`hit_rate(hbm)` 有值、单卡吞吐、artifacts ≥4、上游 CSV 落产物目录、每点独立 datasets 目录 |
| A2 字段映射 | `summarizer` / `aisbench_extra_args`（字段名与脚本变量名不同名） | 两者都真的传到了 ais_bench；`--config-dir` 仍在；`prefix_test:false` 时 notes 说明没有命中率 |
| B 可复现 | 同参数同 seed 两次；再换 seed | 数据集 **sha256 相同**；换 seed 必须变 |
| C picked_ids | 把挑题记录填满（≈1300 行全标） | 默认清理 → 仍成功；`keep_pick_state:true` → exit **1**（不是上游的 0）+ 日志写明「生成数据集失败」+ `RUNNER_NONZERO_EXIT` |
| D 失败不被吞 | `STUB_FAIL_AT` 让假 ais_bench 非零退出 | 全点失败 → exit 1；c=2 成功 c=4 失败 → exit 0 且 `notes` 记下落单点（证明 `bash -o pipefail` 生效） |
| E 单点超时 | `STUB_SLEEP` + `run_timeout:1` | exit 1、`error.code=RUNNER_TIMEOUT` |
| F 参数错误 | 缺 `concurrency` / 未声明字段 / `seed:-1` | 全部 exit 2、`VALIDATION_ERROR` |
| G 凭据零泄漏 | 全程 `ENDPOINT_API_KEY=sk-test-SECRET123…` | 产物目录 `grep` **0 命中**；`temp_api.py` 里是 `api_key=_API_KEY`；argv 里没有明文 key |
| H 契约 | `validate_result_schema.py` | 6 份 result.json 全过 openapi `Result`（含 `ErrorCode` enum） |

## 已知限制

1. **`test_accuracy` 的分数不进 result.json**：上游只把精度细节落在
   `--dump-eval-details` 的产物里，没有解析成结构化指标（要精度请用 `aisbench-acc`）。
   本 Runner 保留这个开关只为「同一 Job 顺带取精度」。
2. **`prefix_test: false` 就没有 `hit_rate`**：上游只在该模式下抓 `/metrics` 前后快照。
   其它指标不受影响。
3. **命中率依赖目标服务暴露 `vllm:prefix_cache_*`**：非 vLLM 后端（或没开 prefix cache）
   就没有该指标，`notes` 会写明是「抓不到」还是「快照不完整」。
4. **并发可能没被真正压满**：`request_rate` 非 0 时「在飞请求数」被 频率×时延 卡住，
   `measured_concurrency` 会明显低于 `target_concurrency`。本 Runner 把两者都写出来
   （`cc` 与 `max_cc`）供下游判断，但**不因此判失败**——测的就是这个配置下的表现。
5. **阶梯里 `seed_per_point: false` 仍会每点重新生成数据集**：`prefix_cache` 路径没有
   「文件已存在就复用」的缓存（只有 `normal` 有）。共享目录只是让产物集中，不省时间。
6. **`dataset` 与 `seed_per_point` 互斥**：给了现成数据集就不会（也不能）重新生成，
   run.sh 自动按 `false` 处理并打一行日志。
7. **GSM8K 题库只有约 1300 行**：`prefix_num` 逼近这个量级、或 `data_num` 很大时，
   前缀行不重复但后缀行会重复（`prefix_flag=0` 是有放回挑）。上游 `main` 还没有
   词表随机模式（本机开发副本里有，未合入），见 `tool/VENDORED.md`。
8. **`runner.image_ref` 恒为 `unknown` 是预期**：平台目前不注入 `AG_RUNNER_IMAGE`
   （`executor/render.go` 没给 Job 设这个变量）；本地跑可以自行 `-e AG_RUNNER_IMAGE=…`。
9. **`work_path`/`dataset_path` 必须在 `/tmp` 或 `/workspace/output` 下**（平台只读根文件系统）。
   `work_path` 可写性探测失败时直接 `failed`，不会硬跑。
10. **arm64 镜像构建慢**：qemu 下要解 torch 与依赖 wheel。发布矩阵里这一步比 amd64 慢一个量级，
    属预期（不是降级——`release.yml` 会校验清单里确实有两个平台）。

## 维护者验证清单

```bash
# 1) runner.yaml 能被注册表解析 + placeholder 逐行合法（临时测试文件，不入库）
cd server && go test ./internal/registry -run TestScanRepoRunnersTemp -v
# 2) 三个离线单测（本机 py -3.11 就够，不需要 docker）
py -3.11 runners/aisbench-prefix/tests/test_prefix_env.py
py -3.11 runners/aisbench-prefix/tests/test_dataset_determinism.py
py -3.11 runners/aisbench-prefix/tests/test_summarize.py
# 3) 全链路冒烟（stub 镜像，或任意有 pandas/requests 的 Linux）
bash runners/aisbench-prefix/tests/smoke.sh
# 4) result.json 按契约校验（需 pyyaml + jsonschema；缺依赖会跳过）
py -3.11 runners/aisbench-prefix/tests/validate_result_schema.py
# 5) 发布镜像按发版 context 构建（双架构见上一节）
docker build -f runners/aisbench-prefix/build/Dockerfile \
  -t ghcr.io/ucm-system/argus-runner-aisbench-prefix:0.1.2 runners/aisbench-prefix
```

## 本机验证记录（2026-11-27）

| 项 | 命令 | 结果 |
|---|---|---|
| vendor 溯源 | `git hash-object` vs `gh api …/git/trees/main` | 7 个未改动文件 blob **逐字节一致**（commit `d9c24f38`）；4 个已改 + 1 个新增见 `tool/VENDORED.md` 的改动表 |
| runner.yaml | 临时注册表测试（Parse + placeholder） | `OK aisbench-prefix@0.1.2 role=test params=32`，placeholder 单行合法且键全部已声明 |
| 离线单测 | `tests/test_prefix_env.py` / `test_dataset_determinism.py` / `test_summarize.py` | 12 / 6 / 11 全绿（同 seed 数据集逐字节相同、记录标满 → `SystemExit(3)`、哨兵值不写成指标、**指标键唯一**、逐点命中率、external 缺失不编 0） |
| 全链路离线冒烟 | `bash tests/smoke.sh`（WSL Ubuntu 24.04 + venv，桩 ais_bench + 假服务端） | **PASS=43 FAIL=0**：A/A2/A3/B/C/D/E/F/G 全部断言通过；`grep` 明文 key **0 命中** |
| 契约 | `tests/validate_result_schema.py` | RESULT SCHEMA **PASS**（成功/全点失败/部分失败/超时/参数错误全部过 openapi `Result`，含 `ErrorCode` enum） |
| 发布镜像 | `docker build -f build/Dockerfile …`（PyPI 清华镜像源） | 0.1.1，**627 MB**（python:3.10-slim + CPU torch 2.14.1 + ais_bench 3.1.0）；构建期自检过（`ais_bench --help`、uid 10001、可写目录） |
| **kind 真跑（真 ais_bench 3.1.20260630 + mock 服务 + 真 tokenizer）** | 平台 API 提交 Task，`service:` 外接模式 + `precheck: auto`，镜像先 `load-image.ps1` 灌进节点 | 两条链路都验证过，见下 |

kind 真跑细节（Task `t-20261127-9C8W`，runner `aisbench-prefix@0.1.2`）：

```yaml
service: { url: http://mock-llm.argus.svc.cluster.local:18080/v1, model_name: qwen3-32b }
env: { AG_MODEL_PATH: /models/mini }     # 外接模式平台不注入 AG_MODEL_PATH，Task env 给
input: {"concurrency":[2,4],"input_len":256,"output_len":16,"data_num":16,
        "dataset_type":"prefix_cache","repeat_rate":"90%","prefix_test":true,
        "seed":7,"seed_per_point":true,"run_timeout":600}
```

结果：`succeeded`，62 metrics / 38 artifacts / duration 50.2s；每点独立预热 + 命中率
（`hit_rate hbm c=2 → 42.7%`、`c=4 → 66.11%`，`labels.point` 区分）；实测并发
1.97/3.71 对得上目标 2/4；`external_prefix_cache_*` 缺失在 notes 写明（不编 0）。

kind 真跑发现并当场修掉的问题（都已带回归测试）：

| # | 症状 | 根因 | 修复 |
|---|---|---|---|
| 1 | `OSError [Errno 30] Read-only file system` | 工具以 `open('default_api.py','r+')` 打开**只读根文件系统**里的模板 | 改 `'r'`；smoke 加静态断言（A3） |
| 2 | 所有请求 404（`error_info: "Not Found"`） | ais_bench 3.1.**20260630** 给非空 path 补尾斜杠，`url=…/v1` 被拼成 `…/v1/v1/chat/completions`（0330 版无此问题） | url 字段给**服务根**（去掉 `/v1` 段）；两种版本行为都进了单测 |
| 3 | 命中率整张表丢失（`Skipped PODs`） | 上游要求 vllm+external 八个计数器字典齐全，vLLM 默认只有 `vllm:prefix_cache_*` | hbm/external 分开判；external 缺失写 notes 不编 0 |
| 4 | 平台判 `RESULT_INVALID`（metrics[i] 与前面重复） | 附录 A 硬规则 `name+statistic+labels` 唯一；两点阶梯的预热阶段并发相同（都=prefix_num） | 所有指标加 `labels.point`；`concurrency` 去重；summarize 撞键丢弃并记录 |
| 5 | `duration_ms=0` | `write_result` 赋值链里一个空行把 `AG_STARTED/AG_START_EPOCH` 断成 shell 局部变量 | 合并成一条前缀赋值 |
| 6 | 压测跑完才报 `UTILS-CFG-002/003`（tokenizer） | ais_bench 的 default_perf 汇总器自己要 `load_tokenizer(path)` | `AG_MODEL_PATH` 缺失/目录不存在 → 提前 `VALIDATION_ERROR`（Task `t-20261127-181T` 验证：3 秒内 exit 2 + 明确消息） |
| 7 | 预检 3 次×10s 误判「服务不可达」 | kind 上新 Pod 的前 1-2 分钟 pod→service 转发未就绪（endpoint-probe 第 7 次才 200） | 预检放宽到 12 次×10s + 5s 间隔（≈3 分钟） |

> 验证环境备注：本机 kind 集群（16 天）在 Docker Desktop 重启后出现过 pod-to-pod 转发
> 与 DNS 的间歇性退化（节点 `arp_filter/rp_filter=1` 与 kindnet ptp 插件不兼容的已知形态），
> 重启节点容器后恢复——这是环境问题，与 Runner 无关，但它正是第 7 条放宽容忍度的原因。

## 未做 / 后续

- **arm64 镜像构建**：Dockerfile 是双架构安全的（torch/ais_bench wheel 都有 aarch64），但本机
  没跑 `--platform linux/arm64` 全量构建（qemu 下 torch 解依赖很慢），交给 release.yml 发版时验证。
- **推送 GHCR**：镜像只在本地 kind 里（`load-image.ps1` 灌入），未 `docker push`——按 §6.2
  由 CI 发版路径推送。
- **PR**：分支 `feat/aisbench-prefix-runner` 待评审合入。

> 冒烟证明的是**链路与产物形态**（参数映射、退出码、seed、清理、零泄漏、schema），
> 不是任何真实模型的性能数字——假 tokenizer 逐字符分词、假 ais_bench 用确定性公式打表。

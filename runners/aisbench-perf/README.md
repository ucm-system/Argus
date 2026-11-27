# AISBench 性能和前缀缓存测试（0.3.6）

对已经运行的模型服务准备数据、预热、压测，再汇总性能和缓存命中率。实现从用户提供的 reference 两套脚本迁移而来。模型服务、UCM 和清缓存由环境负责。

正式实现放在 runners/aisbench-perf；旧入口 testers/aisbench-perf/run.sh 会转发到这里。直接用当前 AISBench 环境，不检查固定版本、源码哈希或内部文件清单。

## 在现有容器中运行

有三种输入：`datasets` 跑旧 Jenkins 的普通性能测试；`test_cases` 跑旧 Jenkins 的 prefix 六列用例；不指定这两项时沿用下面的共享前缀测试。

普通性能测试（配置名、数据路径沿用你的 AISBench 环境）：

```bash
OUT="/tmp/perf-$(date +%Y%m%d-%H%M%S)-$$"
export AIS_BENCH_DATASETS_CACHE=/benchmark
TESTPLANE_OUTPUT_DIR="$OUT" sh testers/aisbench-perf/run.sh \
  --url=http://127.0.0.1:7800/v1 --model_name=qwen25 \
  --model_path=/models/Qwen2.5-7B-Instruct/ \
  --datasets=gsm8k_gen_4_shot_cot_str --working_dir=/benchmark/ais_bench \
  --num_prompts=16 --num_warmups=0 --output_len=32 --concurrency=2 \
  --temperature=0.01 --ignore_eos=true
cat "$OUT/performance_flat.csv"
```

旧 Jenkins 的 prefix 测试：

```bash
OUT="/tmp/prefix-$(date +%Y%m%d-%H%M%S)-$$"
TESTPLANE_OUTPUT_DIR="$OUT" sh testers/aisbench-perf/run.sh \
  --url=http://127.0.0.1:7800/v1 --model_name=qwen25 \
  --model_path=/models/Qwen2.5-7B-Instruct/ \
  --test_cases='[[256,32,16,50,2,1]]' \
  --metrics_profile=vllm_prefix \
  --metrics_endpoint=http://127.0.0.1:7800/metrics
cat "$OUT/hit_rate.csv"
```

六列依次是输入长度、输出长度、请求数、前缀百分比、并发、seed。自动用 private 前缀、正式并发预热、max(1, output_len//10) 预热输出长度，默认 vllm_prompt 口径。如果服务只暴露 prefix_cache_queries_total，没有 prompt_tokens_total，可明确加 `--metrics_profile=vllm_prefix`；报告会注明口径变化。缺失指标按 best_effort 留空，不阻断压测。

`datasets` 路径直接调用 `--mode perf --summarizer default_perf`，不额外做 prefix 预热和指标采集；输出 `performance_flat.csv`、`legacy_results.json`、`result.json`，原始输出在 `raw/native/`。展开表沿用旧 label_statistic 列名，额外 source 列区分多个数据集；保留负号和科学计数法的真实数值。请求计数从相邻性能 JSON 核对，失败请求不会冒充完整成绩。

平台对应 JSONL：

```json
{"datasets":["gsm8k_gen_4_shot_cot_str"],"num_prompts":16,"num_warmups":0,"output_len":32,"concurrency":2,"ignore_eos":true}
{"test_cases":[[256,32,16,50,2,1]],"metrics_profile":"vllm_prefix"}
```

`config_dir` 可指定共享配置根目录；`extra_info` 给结果附加文字或 JSON 字符串，prefix 也导出 legacy_results.json。`datasets`、`test_cases`、`rounds` 三种入口不要混用。原生路径 num_prompts 控制条数，生成数据路径用 data_num。Jenkins 的镜像、NFS、节点和 MongoDB 操作由平台部署与制品收集接替；Runner 不写 MongoDB。

平台将数据集挂到 `/benchmark/ais_bench/datasets`，下级目录如 `gsm8k/`。镜像设置 `AIS_BENCH_DATASETS_CACHE=/benchmark`，使原生配置中的 `ais_bench/datasets/...` 找到该挂载；已有容器直接运行时按上例 export。`working_dir` 不能替代这个数据根目录。run-local.sh 将 DATA_DIR 同时挂到固定路径和 /data，原生模式应传包含各数据集子目录的根目录。随机生成 prefix 数据时不依赖这个挂载。

## 共享前缀测试

从项目根目录执行：

```bash
OUT="/tmp/perf-$(date +%Y%m%d-%H%M%S)-$$"
TESTPLANE_OUTPUT_DIR="$OUT" sh testers/aisbench-perf/run.sh \
  --url=http://127.0.0.1:7800/v1 --model_name=qwen25 \
  --model_path=/models/Qwen2.5-7B-Instruct/ \
  --input_len=256 --output_len=32 --data_num=16 --concurrency=2 \
  --prefix_num=1 --repeat_rate=50% \
  --metrics_profile=auto --metrics_policy=best_effort
cat "$OUT/result.json"
cat "$OUT/prefix_bench_result.csv"
```

metrics_endpoint 不填时，从服务 URL 推导 `/metrics`。PD 或多实例请重复传 `--metrics_endpoint=http://实例/metrics`，也支持旧版 `--pod_info host1:7800 host2:7800`，两者互斥。请填稳定的 Prefill 实例，不用随机转发的网关。

默认 best_effort：继续性能测试，采不到的命中率为 null，不当作 0%。required 要求正式测量前后都有完整计数。预热不因尚未初始化的指标被阻断，但缺失基线不会补成零。请求失败仍然失败。

## 两套旧实现怎样对应

| 来源 | 参数 | 统计口径 |
|---|---|---|
| reference/aisbench_auto_tools_prefix | `--prefix_mode=shared --metrics_profile=vllm_prefix` | 共享前缀池；HBM 和 external 各除以自己的查询 token 数 |
| reference/ais_bench | `--prefix_mode=private --metrics_profile=vllm_prompt` | 每条请求独立预埋前缀；HBM 和 external 都除以 prompt_tokens |
| 新 UCM Connector | `--metrics_profile=ucm_connector` | 使用三个 ucm:* 计数，分母是 Connector 查询 token 数 |

auto 优先完整 UCM 计数，再选择完整 vLLM prefix/prompt 计数；否则保留可用的 HBM 口径。一轮正式测量前选定后不切换；报告写出名称和分母。不同口径的 external 百分比不能直接比较。

默认 data_source=random，从普通词表随机生成，不依赖 GSM8K。data_source=corpus 保留上一版的内置语料方式，仅支持 shared 固定长度。生成后重新校验长度与聊天 token 前缀。shared 使用公共前缀、3 个随机 token、后缀；private 每条数据有独立前缀。前缀各预热 dp 次，不能保证路由覆盖所有实例。

repeat_rate 是目标前缀长度占比，不保证真实命中率。预热输出默认 1，可用 warmup_output_len 设置；并发可用 warmup_concurrency 设置。默认 shared 按 dp（dp=1 按前缀数），private 按正式并发。复现第一套旧脚本时，可手动把预热输出设成 max(1, output_len // 10)。

已有数据用 `--dataset=/data/full.jsonl --warmup_dataset=/data/prefill.jsonl`，每行非空 question，此时不传 input_len、prefix_num、repeat_rate。普通无预热性能测试使用 dataset_type=normal。

## 多轮、变长与监控

```bash
# 加在服务参数后，每组分别生成数据、预热、正式测量。
--rounds='[{"input_len":256,"concurrency":2,"seed":1,"test_name":"small"},{"input_len":512,"concurrency":4,"seed":2,"test_name":"large"}]'
```

rounds 也接受 JSON 文件路径，是带名字的参数覆盖；旧位置数组可直接用 test_cases。repeat=2 表示同组数据重复测量，不清缓存。不同组用不同 seed 可减少意外复用，但不承诺绝对无碰撞。seed=0 使用系统熵并记录实际 seed，非零可复现。

随机变长：length_min/length_max 成对给出时均匀采样；length_mean/length_std 成对给出时高斯采样，可加上下限。每条数据对应实际长度的预热前缀。

thinking=default/on/off 控制 chat_template_kwargs。npu_num 是调用者提供的实际卡数，用于每卡平均吞吐，未填则单卡指标为空。monitor_interval_seconds 默认 15，0 关闭采样；采集 vllm:num_requests_running 的分阶段、分 endpoint 最大值。这是采样最大值，不是连续时间峰值。

## 输出

| 文件 | 内容 |
|---|---|
| result.json | 平台 schema 2.0；标准指标、执行状态和原始文件索引 |
| prefix_bench_result.csv / .jsonl | 每阶段一行，phase=warmup/full；负载、TTFT/TPOT、吞吐、per-DP 及总命中率 |
| hit_rate.csv / .jsonl | 正式测量每 endpoint 一行，命中率、hit/query、running 最大值、AISBench JSON total / CSV Average |
| raw/resolved.json | 实际参数、seed、数据校验值、token 长度和前缀对应关系 |
| raw/round-N/warmup 或 measurement | AISBench 配置、原始输出、requests.jsonl、aisbench.log、report.json |
| raw/round-N/metrics | 原始 .prom、采集记录和 running 采样 |

prefix_bench_result 的 hit_rate 字段是 **0..1 比例**，result.json 的 percent 指标为 **0..100**，hit_rate.csv 的显示列带 `%`。JSON 缺失项为 null，CSV 为空；无查询不报告 0% 命中率。

dp_sources 给出 dp0 等列对应的 endpoint 和 engine/worker 标签，两个节点的 engine=0 不会合并。aisbench_total:*、aisbench_avg:* 原样保留上游字段；标准延迟来自逐请求时间点，未知指标不编造。warmup 行使用实际预热并发、条数和输出长度。

rounds 各组放 groups/N，根目录汇总表格；平台多行参数放 cases/N，各自保存表格，根 result.json 用 case 标签区分。失败阶段报告标 failed，保留已有结果。退出码 0 完成、1 执行/结果失败、2 输入错误。

## 平台参数和镜像

Dockerfile 支持 Buildx 的 linux/amd64、linux/arm64，按 TARGETARCH 选择对应 AISBench 基础镜像。BASE_AMD64、BASE_ARM64 和 PIP_INDEX_URL 可通过 --build-arg 覆盖；两个平台使用相同固定依赖。双架构构建及逐架构冒烟测试见 [镜像发布说明](../../docs/aisbench-dockerhub.md)。下面的 docker build 只构建当前目标架构。

平台扫描 runner.yaml。公共参数用 AG_SERVED_URL、AG_SERVED_MODEL_NAME、AG_MODEL_PATH，业务 JSONL 每行一组：

```json
{"input_len":256,"output_len":32,"data_num":16,"concurrency":2,"repeat_rate":"50%","metrics_profile":"auto","metrics_policy":"best_effort"}
```

输入文件优先 --params-file，其次 AG_PARAMS_FILE，再次 /workspace/input/params.jsonl。CLI 明确值覆盖对应字段。公共参数不写业务 JSONL。输出优先 AG_OUTPUT_DIR，再兼容 TESTPLANE_OUTPUT_DIR，必须是新空目录。模型和数据由 Execution Profile 挂载，路径参数不会上传文件。鉴权用 ENDPOINT_API_KEY 和 METRICS_API_KEY，分别用于模型与指标。

```bash
docker build -f runners/aisbench-perf/build/Dockerfile \
  -t ghcr.io/ucm-system/argus-runner-aisbench-perf:0.3.6 runners/aisbench-perf
python -m unittest discover -s runners/aisbench-perf/tests
# 在已有 AISBench 环境里，用模拟服务验证真实调用链：
python3 runners/aisbench-perf/tests/real_smoke.py
```

构建基线固定依赖，但用户现有镜像不必安装它们才能运行。镜像尚需构建和发布。离线验证使用模拟 tokenizer/请求，不能代替真实 AISBench 3.1 和 UCM 验收。

设计见 [Jenkins 迁移说明](../../design/impl/aisbench-jenkins-migration.md) 和 [已有迁移说明](../../design/impl/aisbench-reference-migration.md)，源文件校验值见 [reference-sources.json](reference-sources.json)。

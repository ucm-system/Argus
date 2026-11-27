# AISBench 精度测试（0.3.6）

对已经运行的模型服务做精度测试。`datasets` 使用容器里的 AISBench 数据集配置和原评分器；`dataset` 保留原有 GSM8K 逐题审计。模型与 UCM 由环境负责。

实现放在 runners/aisbench-acc，旧入口 testers/aisbench-acc/run.sh 继续有效。不检查 AISBench 固定版本、源码或内部文件。保留了没有 data_id 时的请求记录修复，以及 BatchEncoding 的 input_ids 处理。

## 按旧 Jenkins 的方式运行

已有 AISBench 3.1 容器中，从项目根目录执行：

```bash
OUT="/tmp/acc-$(date +%Y%m%d-%H%M%S)-$$"
export AIS_BENCH_DATASETS_CACHE=/benchmark
TESTPLANE_OUTPUT_DIR="$OUT" sh testers/aisbench-acc/run.sh \
  --url=http://127.0.0.1:7800/v1 --model_name=qwen25 \
  --model_path=/models/Qwen2.5-7B-Instruct/ \
  --datasets=gsm8k_gen_4_shot_cot_str \
  --working_dir=/benchmark/ais_bench \
  --num_prompts=10 --num_warmups=0 --concurrency=1 --output_len=512 \
  --temperature=0.01 --ignore_eos=true
cat "$OUT/accuracy.csv"
cat "$OUT/result.json"
```

`datasets` 填旧 AISBENCH_DATASETS 的配置名，多项用逗号分隔。可测的数据集以你镜像中的配置和数据为准；例如 GSM8K、MMLU、C-Eval 的生成式配置，不把所有数据集当作数学题评分。配置列表可在容器运行 `ais_bench --datasets gsm8k --search` 查看；实际运行填写完整匹配的名字。

原来的配置放在共享目录时，加 `--config_dir=/mnt/private/dataset/aisbench_configs`。平台将数据集根目录挂到 `/benchmark/ais_bench/datasets`，例如其下包含 `gsm8k/train.jsonl` 和 `gsm8k/test.jsonl`。Runner 镜像设置 `AIS_BENCH_DATASETS_CACHE=/benchmark`，对应配置里的 `ais_bench/datasets/gsm8k`；已有容器直接运行时按上例 export。`working_dir` 只设置工作目录，不能替代数据根目录配置。自定义配置的绝对路径需要与实际挂载一致。这个路径调用 `--mode all --merge-ds --summarizer example`，保留原模型配置的回答后处理。

`accuracy.csv` 保留 AISBench 汇总列和成绩文本，`legacy_results.json` 保留旧 build/type/status/data/extra_info 结构，build 改用平台 task_id。原始 CSV、预测文件、日志在 `raw/native/`，`result.json` 保存平台成绩与文件索引。多数据集各有自己的分数，不拼成一个准确率。原生路径依赖 AISBench 执行和汇总判定，不提供下面 GSM8K 路径的逐题审计保证。

## 原有 GSM8K 路径

```bash
OUT="/tmp/acc-$(date +%Y%m%d-%H%M%S)-$$"
TESTPLANE_OUTPUT_DIR="$OUT" sh testers/aisbench-acc/run.sh \
  --url=http://127.0.0.1:7800/v1 --model_name=qwen25 \
  --model_path=/mnt/model/Qwen2.5-7B-Instruct/ \
  --dataset=/benchmark/ais_bench/datasets/gsm8k/ \
  --dataset_config=gsm8k_4shot_cot --max_samples=10 --concurrency=1
cat "$OUT/result.json"
```

gsm8k_4shot_cot 使用已安装 AISBench 的官方四例模板，dataset 是含 train.jsonl/test.jsonl 的目录。gsm8k_jsonl_0shot 使用零样本模板，dataset 是 JSONL 文件，每行含 question 和数字 answer，例如 `{"question":"What is one plus one?","answer":"2"}`。replay_rounds=2 会真正重发一轮，并保存两轮评分。

## 平台和输出

平台扫描 runner.yaml；公共参数来自 AG_SERVED_URL、AG_SERVED_MODEL_NAME、AG_MODEL_PATH。业务参数示例：

```json
{"datasets":["gsm8k_gen_4_shot_cot_str"],"num_prompts":10,"num_warmups":0,"concurrency":1,"output_len":512,"ignore_eos":true}
```

输入文件顺序：--params-file > AG_PARAMS_FILE > /workspace/input/params.jsonl，每行一组，CLI 明确参数覆盖对应字段。数据和模型由 Execution Profile 挂载。鉴权用 ENDPOINT_API_KEY。

输出优先 AG_OUTPUT_DIR，其次 TESTPLANE_OUTPUT_DIR，每次用新空目录。result.json 使用 schema 2.0；身份来自 AG_TASK_ID、AG_ATTEMPT、AG_RUNNER_SEQ，独立执行为 local/1/1。

GSM8K 路径的结果包含 result.json、raw/resolved.json、raw/dataset，以及 raw/round-N 中的 requests.jsonl、sample_scores.jsonl、AISBench 原始目录和 aisbench.log。多行输入放 cases/N，根 result.json 用 case 标签区分。失败时显示原始日志末尾。退出码 0 完成、1 执行失败、2 输入错误。

## 构建和验证

Dockerfile 支持 Buildx 的 linux/amd64、linux/arm64，按 TARGETARCH 选择对应 AISBench 基础镜像。BASE_AMD64、BASE_ARM64 和 PIP_INDEX_URL 可通过 --build-arg 覆盖；两个平台使用相同固定依赖。双架构构建及逐架构冒烟测试见 [镜像发布说明](../../docs/aisbench-dockerhub.md)。下面的 docker build 只构建当前目标架构。

```bash
docker build -f runners/aisbench-acc/build/Dockerfile \
  -t ghcr.io/ucm-system/argus-runner-aisbench-acc:0.3.6 runners/aisbench-acc
python -m unittest discover -s runners/aisbench-acc/tests
# 在已有 AISBench 环境，用模拟服务检查真实调用链：
python3 runners/aisbench-acc/tests/real_smoke.py
```

run-local.sh 支持 MODEL_DIR、DATA_DIR、OUTPUT_DIR、TESTER_IMAGE。DATA_DIR 同时挂到 /benchmark/ais_bench/datasets 和兼容路径 /data；原生 datasets 模式应指向包含 gsm8k 等子目录的数据集根目录。源码锁只描述构建基线，不在运行前校验。离线测试不能替代真实模型/UCM 验收。比较 UCM 开关前后精度时，应保持模型、题目和生成参数相同。

`dataset` 与 `datasets` 互斥；原生路径使用 num_prompts，GSM8K 路径使用 max_samples，不能混传。extra_info 仅用于原生路径，可填 JSON 字符串或普通文字。平台重启 argusd 后扫描注册新版本；使用平台前需构建和发布 runner.yaml 指向的镜像。

设计见 [Jenkins 迁移说明](../../design/impl/aisbench-jenkins-migration.md) 和 [已有迁移说明](../../design/impl/aisbench-reference-migration.md)。

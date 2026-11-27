# 这是什么

本目录是外部仓库 **`aisbench_auto_tools_prefix`**（AISBench 前缀缓存自动化压测 CLI）的副本（vendored）。
Runner 镜像直接从这里 `COPY`，**不在构建期联网 clone**；上游改动靠重新 vendor 一次。
与上游的差异只有下面「本地改动」列出的几处（`design/ARCHITECTURE.md` §6.2 要求构建输入可追溯）。

| 项 | 值 |
|---|---|
| 上游仓库 | <https://github.com/rayn-zzz/aisbench_auto_tools_prefix> |
| vendored commit | `d9c24f3822dfdcf01545388561d6138e059851e4`（`main`，2026-11-20，`Merge pull request #28 … fix: Ignore metric dictionaries when parsing result tables`） |
| vendored 时间 | 2026-11-27 |
| License | MIT（`LICENSE`，Copyright (c) 2026 rayn-zzz） |
| 上游文档 | `README.md`（上游原文，含它自己的用法说明；**Runner 用法看 `../README.md`**，两处不一致时以本仓库为准） |
| 运行时依赖 | `pandas`（`save_file.py`）、`requests`（`cal_prefix_hit_rate.py`）、`transformers`（仅生成数据集时）、`tqdm`（可选，缺了不报错） |

## 逐文件核对（这份副本对应哪个上游 commit）

**未改动的文件逐字节等于上游 `main`**（blob sha 与 GitHub tree 一致）：

```powershell
# 上游 tree（GitHub API）
gh api repos/rayn-zzz/aisbench_auto_tools_prefix/git/trees/main?recursive=1 --jq '.tree[] | "\(.sha) \(.path)"'
# 本副本：只有下面这批「未改动」文件的 hash 应当与上游完全相同
cd runners/aisbench-prefix/tool
git hash-object data_picker.py cal_prefix_hit_rate.py save_file.py GSM8K.jsonl LICENSE README.md tests/test_save_file.py
```

| 文件 | 上游 blob sha | 状态 |
|---|---|---|
| `data_picker.py` | `f379553660c3537703918f7c0b6b77e0db44c55b` | 未改动 |
| `cal_prefix_hit_rate.py` | `147e54f7d464ac6ddccb90e7f525a2d658c57bf2` | 未改动 |
| `save_file.py` | `796c6fd59a5bc2a725effae5382beff5c31eeba0` | 未改动 |
| `GSM8K.jsonl` | `d3bbacc6b8e152049df89cf2b48cf8e7591d150a` | 未改动（约 1300 行题库） |
| `LICENSE` | `d06b6f9d506ce0a1732a0dc3d52a5fc343cd2585` | 未改动（MIT） |
| `README.md` | `e174afb040095e4d1c914502d0ff508e4d1ab77b` | 未改动（上游用法文档） |
| `tests/test_save_file.py` | `c17c4119004e5452ab4b77c9670a4a159f6d7f7c` | 未改动（上游自带测试，留档） |
| `aisbench_test.py` | 原始 `4f6d56f6b3d7c07f0de2349a74c463f8c3f75a13` | **已改**（下表第 8–12 条） |
| `generate_dataset.py` | 原始 `fa07e5a43feac460fca1b17e5f48902b46af153c` | **已改**（第 3–6 条） |
| `config.py` | 原始 `4d61a21c19318dd5eab702bab2915e5d13f0eeb7` | **改写**（第 2 条） |
| `default_api.py` | 原始 `22e70b9ae8e7089fe49fc203c815d6085ac57b67` | **已改**（第 7 条） |
| `prefix_env.py` | — | **新增**（第 1 条） |
| `VENDORED.md` | — | **新增**（本文件） |

已改文件的核对方式：把上游原始 blob 取出来逐行 diff，只应看到「本地改动」列出的那些差异：

```bash
git -C <上游副本> cat-file blob 4f6d56f6b3d7c07f0de2349a74c463f8c3f75a13 > /tmp/up_aisbench_test.py
diff -u /tmp/up_aisbench_test.py runners/aisbench-prefix/tool/aisbench_test.py
```

> 行尾：上游这些 blob 本身是 **LF**；Windows 检出（`core.autocrlf=true`）会落成本地 CRLF，
> 入库前已全部归一为 LF 并用 `git hash-object` 复核（`.gitattributes` 的 `text=auto`
> 保证索引里也是 LF）。镜像构建时再 `sed -i 's/\r$//'` 兜一道（见 `../build/Dockerfile`），
> 免得带 CRLF 的 shebang 在 Linux 里报 `/usr/bin/env: 'python3\r'`。

## 为什么 vendor 而不是 clone

`design/ARCHITECTURE.md` §6.2：镜像由 CI 产出、可追溯到具体镜像引用。构建期联网 clone 会让
「这次构建到底拉了哪个 commit」不可复现（且内网构建机不一定到 github.com）。vendor 进来后，
构建输入就是仓库里的字节——只有 pip 装 `ais-bench-benchmark` 这一步联网（版本已钉死，见 `build/Dockerfile`）。

## 本地改动（与上游 `d9c24f38` 的差异）

改动只有一类目的：**让这个 CLI 能在容器里以「一次调用一个实验」的方式被机器驱动**，
以及修掉三个会直接影响结论可信度的行为（失败被吞、seed 不生效、跨次状态累积）。
**没有改**压测算法、数据集结构、指标口径。

| # | 文件 | 改动 | 原因 |
|---|---|---|---|
| 1 | `prefix_env.py` | **新增**：环境变量读取、`AG_SERVED_URL` → ais_bench 寻址字段归一化、状态文件路径、RNG 播种、sidecar `emit()`、退出码常量 | 站点信息在容器里不可能靠编辑文件传入（§12.4 / §6.8）；`host_ip` 必须是合法 IP 而平台常给集群内 DNS 名（`docs/research/aisbench-real-run.md` 已踩过） |
| 2 | `config.py` | **改写**：每个常量都可被 `AGP_*` 环境变量覆盖；`URL`/`HOST_IP`/`HOST_PORT` 由 `AGP_URL` 归一化得到；新增 `POD_INFO_DEFAULT`；`API_KEY` 不再参与落盘 | 同一原因：Runner 不编辑配置文件。默认值改成对容器友好的 `/tmp/prefix-ws` |
| 3 | `generate_dataset.py` | `from transformers import AutoTokenizer` / `import torch` → **惰性 import**（`_load_tokenizer()`），去掉两处 `torch.manual_seed` | 顶层 import 让「指现成数据集」也要拖起 transformers；也让离线回归能在不装 torch 的镜像里跑。torch 在本文件只被用来 `manual_seed`，对可复现性没有贡献（没有任何 torch 算子参与生成） |
| 4 | `generate_dataset.py` | 生成入口先 `reset_pick_state()` + `seed_rng(seed)` | **本轮反馈的两条**：① FAQ「生成数据集失败，请清空 picked ids」= `picked_ids.txt` 跨次累积把题库标满，Runner 自动清（`keep_pick_state` 可关）；② `--seed` 原本只喂给「前缀后 3 个随机 token」（`generate_unique_tokens()` 里才 `random.seed()`），GSM8K 挑题走 `DataPicker.pick_one()` 里**未播种**的全局 `random.choice/randint` → 同参数两次跑数据集不同，结果不可复现 |
| 5 | `generate_dataset.py` | 题库与挑题记录改成绝对路径（`AGP_GSM8K_FILE` / `AGP_STATE_DIR`） | 上游写死 `"./GSM8K.jsonl"`、`"./picked_ids.txt"`，跟着 CWD 走；Runner 的 CWD 是产物目录，不换路径就找不到题库、记录还会攒进产物目录 |
| 6 | `generate_dataset.py` | 4 处 `logging.error("生成数据集失败…") + exit(0)` → `SystemExit(3)`（`_dataset_failed()`），并把容量与路径写进消息；另给 3 条 `_rows()` 结果补空值检查 | `exit(0)` 让「没生成出数据集」在 CI/Runner 里表现为**成功退出**（Task succeeded + 零结论）。没有空值检查的分支则以 `TypeError: object of type 'NoneType' has no len()` 收场，报错完全指不到真实原因 |
| 7 | `default_api.py` | `api_key="api_key_for_replace"` → `import os` + `_API_KEY = os.environ.get("ENDPOINT_API_KEY", "")` + `api_key=_API_KEY` | §12.4 凭据只走 env：生成的 `temp_api.py` 与被 `--config-dir` 引用的模型配置**落盘都不含明文 key**（评审 #62 对 aisbench-sla 提的同一问题，做法一致） |
| 8 | `aisbench_test.py` | `generate_aisbench_command()` 读 `AGP_AISBENCH_CMD` / `AGP_AISBENCH_EXTRA_ARGS` / `AGP_NUM_WARMUPS`；路径用引号包住 | ① 离线回归要用桩替代 `ais_bench`；② 上游 FAQ #3「旧版 ais_bench 报 unrecognized arguments: --num-warmups」的处置是「去源码里删掉这行」，现在留空即不传，不用改代码 |
| 9 | `aisbench_test.py` | 新增 `run_aisbench()`：`bash -o pipefail -c`、`start_new_session=True`、`AGP_RUN_TIMEOUT` 超时按进程组 SIGKILL、非零 → `SystemExit(4)` | 上游 `subprocess.run(cmd, shell=True, check=True)` 跑的是 `ais_bench … \| tee … \| tee …`，shell 取的是**最后一个 tee 的退出码**——ais_bench 崩了也返回 0，于是失败被吞、后面只剩哨兵值 99999；长上下文又没有单点超时（只能等平台 watchdog 掐整个 Job） |
| 10 | `aisbench_test.py` | `modify_aisbench_api()`：模板从 CWD 相对改为 `TOOL_DIR` 相对；`re.sub` → `str.replace`；写配置前先 `makedirs`；不再替换 `api_key_for_replace` | 模板在只读的 `/runner/tool` 里而 CWD 是产物目录；`re.sub` 会把替换值里的 `\1`/`\g<…>` 当反向引用（模型路径/URL 是用户给的字符串）；`WORK_PATH` 下的目录在新容器里不存在 |
| 11 | `aisbench_test.py` | `save_result(..., phase=)`、`cal_prefix_hit_info(..., phase=)` 各发一条 sidecar 事件（`AGP_EVENTS`）；`pod_info` 默认值改用真实 `host:port` | ① Runner 的 result.json 不该正则解析打屏表格（`0.00%` 字符串、对齐宽度都是给人看的）；② 上游默认 `HOST_IP+":"+HOST_PORT` 抓 `/metrics`，而 `HOST_IP` 在「用 url 字段寻址」时被刻意写成 `localhost`（规避 ais_bench 的 IP 校验）→ 永远抓不到指标；③ `--dataset` 不存在的退出码从 1 改 2（参数错误与业务失败要分开，§5.8） |
| 12 | `aisbench_test.py` | 日志与配置快照事件（`kind=config`）写出**归一化后**的口径 | 排查时要看「实际跑了什么」，不是原始输入 |

**语义保持不变**的部分：数据集结构（前缀 + 3 个随机 token + 后缀）、`ais_bench` 命令的
模型/数据集/汇总器选择、CSV 列与 `DEFAULT_METRICS` 口径、命中率差分算法（逐 engine 增量、
`ALL_PODS` 汇总、快照不完整就跳过该 POD）一律照上游。

## 上游没进本副本的东西

- 本机开发副本 `D:\Project\aisbench_auto_tools_prefix`（工作区在 `main` 之上有**未提交**改动）里
  有 `--dataset_source random`（词表随机采样生成精确长度文本，解决 GSM8K 只有约 1300 条导致
  大量重复）与 `random_picked.txt` 跨次去重。这些**没有推到 `main`**，本 Runner 不 vendor
  无法对应 commit 的代码；等它合入上游后按下面的流程重新 vendor 即可
  （届时把 `dataset_source` 加进 `runner.yaml` 参数与本 Runner 的 `run.sh` 映射）。
- 上游 `tests/` 只收了 `test_save_file.py`（跟着源码留档）。

## 怎么升级

```bash
# 1) 取上游目标 commit（本机到 github.com 走不通时用 gh api 落盘）
REF=<commit-sha>
for f in aisbench_test.py config.py default_api.py save_file.py generate_dataset.py \
         data_picker.py cal_prefix_hit_rate.py GSM8K.jsonl LICENSE README.md tests/test_save_file.py; do
  gh api "repos/rayn-zzz/aisbench_auto_tools_prefix/contents/$f?ref=$REF" --jq .content \
    | base64 -d > "runners/aisbench-prefix/tool/$f"
done
# 2) 逐文件比对：把 tool/ 与上游同路径 diff -r，允许出现的差异只有
#    「本地改动」列出的文件 + VENDORED.md 本身
# 3) 重跑本 Runner 的验证（见 ../README.md「维护者验证清单」）
```

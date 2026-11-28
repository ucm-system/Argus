# Argus v0.3.0

这一版的主题是**测量能力落地**：新增两个真实数据面 Runner（aisbench-prefix 前缀缓存测量、agentx 长上下文压测）、aisbench-sla 大版本升级（result.html 阶梯报告 + 前置校验 + 修丢参）、全部 Runner 镜像统一为自打多架构并弃用 QEMU；平台侧带 Mounts 别名挂载、提交页模型路径下拉、D-78 提交名称可选，外加一波 Web UX 收口与玻璃拟态落地页。

## 镜像（linux/amd64 + linux/arm64，全部原生构建、无 QEMU）

| 镜像 | index digest |
|---|---|
| `ghcr.io/ucm-system/argus-server:0.3.0` | `sha256:5488baf1b55e70deb7741a0830af29659df5e7838706a70de2407a047cc4965b` |
| `ghcr.io/ucm-system/argus-web:0.3.0` | `sha256:9db66964672653dbe9a2a2e0edeb5f19d106190d95fee37ea7569a7e107d48b8` |
| `ghcr.io/ucm-system/argus-runner-aisbench-prefix:0.3.0` | `sha256:29f94e40c9438bb0375fcfbfb78c8cf037522d8a54c5d09904896514c31596ea` |
| `ghcr.io/ucm-system/argus-runner-aisbench-sla:0.3.0` | `sha256:d661b05994808bc9bf9f04c9ed5fd1d8c638cd71fd2b3ef90b873b99c0ccedbc` |
| `ghcr.io/ucm-system/argus-runner-agentx:0.3.0` | `sha256:67717043ff355c193a65f69be4f26c7ef0e9d070d19148292aea07de3ac7c6d3` |
| `ghcr.io/ucm-system/argus-runner-endpoint-probe:0.3.0` | `sha256:d00e4802205c848fa219c55e822568b2046344fdc7c4be3c7a9593c5291ad8c7` |
| `ghcr.io/ucm-system/argus-runner-genet-deploy:0.3.0` | `sha256:b3dbde744529b88814bec623e427d0863dcde65b7d0ab7f72ad4852c62bdb0cc` |
| `ghcr.io/ucm-system/argus-runner-genet-clean:0.3.0` | `sha256:3545b5b4b9e40336a38bd3e411f85ebe61d74a23d60b5becd56f2804d77b7ddb` |
| `ghcr.io/ucm-system/argus-runner-mock-deploy:0.3.0` | `sha256:52a1172efbb83ae8af1a2efe34c881599bc5d0effed8594b1215a413cf47e117` |

> 发版矩阵自动发现 `runners/*/build/Dockerfile`（加 Runner 不用改 workflow）；构建双 job 原生 runner（amd64=`ubuntu-latest`、arm64=`ubuntu-24.04-arm`，public 仓库免费）+ `imagetools create` 合清单——**binfmt/QEMU 步骤已整个移除**，所有镜像基镜像均为多架构。

## 1. 新增 Runner：aisbench-prefix（#121/#124）

- 封装 [aisbench_auto_tools_prefix](https://github.com/rayn-zzz/aisbench_auto_tools_prefix)（vendored `main@d9c24f38`）：前缀缓存场景的并发阶梯测量，命中率按目标服务 `/metrics` 的 `vllm:prefix_cache_*` 差分；
- 三个观察面：`result.json`（平台长表）、**`result.html`（阶梯对比报告，自包含、浏览器直开）**、`aisbench_result.csv`（上游列序聚合宽表）+ 每点原始 CSV；
- 相对上游的行为修正（每条有回归测试）：picked_ids 跨次累积 → 自动清理且失败不再 exit(0)；`--seed` 真正吃到 GSM8K 挑题路径（同 seed 数据集逐字节可复现）；ais_bench 的 `| tee` 不再吞退出码（pipefail + 进程组超时）；AG_MODEL_PATH 前置校验；
- 镜像自打多架构（python slim + CPU torch + pip `ais-bench-benchmark==3.1.20260630`，~627MB）。

## 2. aisbench-sla 升级 0.5.1 → 0.6.0（#124/#125）

- **result.html 阶梯报告**：SLA 约束口径表、结论卡（配置口径与实测口径的最大可行并发分开）、阶梯总表（判定徽标/降级标注/实测并发）、趋势图带 **SLA 阈值虚线**、命中率趋势；自包含 HTML；
- 两个前置校验：`AG_MODEL_PATH` 缺失/不存在 → 秒级 `VALIDATION_ERROR`（ais_bench 汇总器要 load_tokenizer，别等压测跑完才报）；被测服务 TCP 预检 12 次×10s → `ENDPOINT_UNREACHABLE`；
- **修 start/max 静默丢参**（生产 bug，容器冒烟复现）：JSONL 解析循环把 `"start"`/`"max"` 设成 `$START`/`$MAX`，工具旗标用 `${START_C}`/`${MAX_C}`——没有映射，#94 引入后这两个参数一直被丢弃；
- 镜像统一为自打多架构（同 prefix），弃用官方单架构镜像。

## 3. 新增 Runner：agentx（#109）

长上下文 agentic 压测（Weka 真实 trace 回放）；离线可跑 stub 冒烟。

## 4. 提交体验（#110/#122）

- D-78：Task 名与 runner 名缺省由平台生成；
- Mounts 别名挂载 + 提交页模型路径下拉；修 `/admin/clusters` 405。

## 5. Web / 发布流程（#119/#120/#123）

- Web 控制台 UX、视觉密度与文件弹窗整体收口；Argus 落地页（玻璃拟态）+ GitHub Pages 发布；
- release.yml 多架构构建改**双 job 原生 runner**（amd64=`ubuntu-latest`、arm64=`ubuntu-24.04-arm`）+ `imagetools create` 合清单，弃用 QEMU 单机模拟（aisbench 镜像 pip 装依赖在模拟下要几十分钟，原生是分钟级）；job 级 `if` 不允许 matrix 上下文的启动失败已修（#123）。

## 发版步骤（本 PR 合并后）

1. `git tag v0.3.0 origin/main && git push origin v0.3.0` → release.yml 发 9 镜像（双架构原生）；
2. 从 merge job 的 job summary 提取 index digest（`gh run view <id> --log | grep index_digest`），`docker buildx imagetools inspect ghcr.io/ucm-system/argus-<name>:0.3.0` 复核；
3. digest 回填 PR（values.yaml 钉 argus-server digest + 本表补全 9 个 digest）；
4. `gh release create v0.3.0 --notes-file …`。

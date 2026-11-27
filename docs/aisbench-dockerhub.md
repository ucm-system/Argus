# 用 GitHub Actions 打包 AISBench 镜像

Workflow 文件是 `.github/workflows/aisbench-dockerhub.yml`，在 Actions 页面显示为 **AISBench Docker Hub**。构建源码固定取远端 **temp** 分支，一次运行会分别构建精度、性能两个镜像，使用各自 `runners/aisbench-*/build/Dockerfile`，构建上下文是对应 Runner 目录。

## 怎么运行

1. 仓库 Settings → Secrets and variables → Actions → Repository secrets 配好 `DOCKERHUB_USERNAME` 和 `DOCKERHUB_TOKEN`。用户名填写 Docker Hub 用户名，Token 需要有写入对应镜像仓库的权限。
2. 将最新构建代码推送到远端 `temp` 分支。另将 workflow 文件通过 PR 合入默认分支，以便 GitHub 显示手动运行入口；构建源码仍从 `temp` 读取。
3. 打开 Actions → **AISBench Docker Hub** → **Run workflow**。界面的分支选项决定读取哪一份 workflow，可选择包含本 workflow 的 `temp`；本 workflow 内部固定检出 `temp` 的源码。
4. `tag` 留空即可使用各自 `runner.yaml` 中的版本，当前都是 `0.3.5`；也可以填写 `0.3.5-test1`。填写的标签原样使用，不会自动去掉 `v`。不允许填写 `latest` 或以 `sha-` 开头的标签。
5. 等待两个任务都成功，分别打开任务 Summary，复制镜像地址或 digest。

例如 Docker Hub 用户名为 `yourname`，默认生成：

```text
docker.io/yourname/argus-runner-aisbench-acc:0.3.5
docker.io/yourname/argus-runner-aisbench-perf:0.3.5
```

每次运行先解析一次 `temp` 的提交 SHA，两个镜像都构建这一份源码。每个镜像另有 `sha-<完整源码提交 SHA>` 标签，镜像 revision 和 Summary 也记录这个 SHA。重复发布同一个标签会覆盖该标签，固定部署推荐使用 Summary 中的 `image@sha256:...`。两个构建任务独立，一个失败时另一个仍可能发布成功；以各自结果为准。

也可以使用 GitHub CLI：

```bash
gh workflow run aisbench-dockerhub.yml --ref temp -f tag=0.3.5-test1
```

这里的 `temp` 必须已经推送到远端，workflow 也必须已经进入默认分支。

## 构建时会做什么

先登录 Docker Hub，再构建并加载镜像，以镜像里的非 root 用户检查 `run.sh --help`，运行已有的 `tests/real_smoke.py`。该脚本在容器内启动模拟模型服务，检查真实 AISBench 调用与结果输出，通过后才推送同一份镜像。

这个检查不需要 GPU、模型权重、外部数据集或 UCM。它不能替代连接真实服务后的精度、性能和命中率测试。如果检查失败，查看对应任务的 smoke test 日志。

镜像创建 UID/GID 10001 的 aisbench 用户，并将 PyTorch 缓存设置在 `/tmp`。构建末尾也会以该用户导入 AISBench CLI，提前检查依赖初始化。旧镜像若报 `getpwuid(): uid not found: 10001`，需要用更新后的 Dockerfile 重新构建。

当前 Dockerfile 的基础镜像是 x86_64，因此只构建 `linux/amd64`。GitHub runner 需要能拉取基础镜像并下载 Dockerfile 中的 Python 依赖；环境内容仍由 Dockerfile 和 requirements.txt 决定。基础镜像较大，若构建报磁盘不足，需要换用磁盘更大的 GitHub runner。

## 发布后怎样接入平台

镜像中的 aisbench 用户使用 `/bin/bash`，支持 VS Code 附加容器后打开终端。旧容器若报 `/usr/sbin/nologin failed to launch`，在宿主机执行 `docker exec -u root 容器名 usermod -s /bin/bash aisbench`，再断开并重连 VS Code。该命令只修改当前容器，重建后需使用修复后的镜像。

若容器以只读根文件系统运行，无法修改 /etc/passwd，可在 VS Code 的容器远程设置中指定终端配置，再新建终端（参见 [Terminal Profiles](https://code.visualstudio.com/docs/terminal/profiles)）：

```json
{
  "terminal.integrated.profiles.linux": {
    "bash": { "path": "/bin/bash" }
  },
  "terminal.integrated.defaultProfile.linux": "bash"
}
```

发布镜像不会自动修改 `runner.yaml`。当前注册文件仍指向 GHCR；使用 Docker Hub 镜像时，把相应 `image` 改成 Summary 给出的地址或 digest，同时递增 Runner 的 `version`，按仓库约定同步版本引用和测试，再重启 argusd 注册。

本地联调可通过 `TESTER_IMAGE=docker.io/yourname/argus-runner-aisbench-acc:0.3.5`（性能使用 perf 镜像）指定 `run-local.sh` 使用的镜像。

数据集仍由系统挂到 `/benchmark/ais_bench/datasets`，镜像设置 `AIS_BENCH_DATASETS_CACHE=/benchmark`，无需在 workflow 上传数据集。模型目录与输出目录也在运行时挂载。私有 Docker Hub 镜像还需要集群具备相应拉取凭据，GitHub Secrets 不会自动传给集群。

实现采用 Docker 官方的[推送前测试流程](https://docs.docker.com/build/ci/github-actions/test-before-push/)。手动触发及默认分支要求见 [GitHub 文档](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)。

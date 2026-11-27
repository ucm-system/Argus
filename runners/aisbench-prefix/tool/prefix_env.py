#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runner 侧的环境桥接（**本文件不是上游代码**，见 ../tool/VENDORED.md）。

上游 `aisbench_auto_tools_prefix` 的用法是「编辑 config.py → 跑 python 命令」：
站点信息（服务地址、模型名、权重路径、数据集目录、aisbench 源码根、鉴权）写死在
`config.py` 里，负载参数走命令行。在 Argus Runner 里前者必须由**环境变量**给出
（§12.4：凭据只走 env；§5.3：业务参数只走输入文件），所以这里集中提供：

* `env()` / `env_int()` / `env_float()` —— 带默认值的环境变量读取；
* `parse_target()` —— 把 `AG_SERVED_URL` 归一化成 ais_bench 模型配置要的两套寻址字段
  （`url` 或 `host_ip`+`host_port`）与 `/metrics` 的 `host:port`；
* 状态文件路径（`picked_ids.txt`）——上游把它写死成 CWD 相对路径，跨次累积就会
  产出 README FAQ 里那条「生成数据集失败，请清空 picked ids」；这里改成
  `AGP_STATE_DIR` 下的绝对路径，并提供 `reset_pick_state()`；
* `seed_rng()` —— 让 gsm8k 挑题路径也吃 `--seed`（上游只给 3 个随机 token 播种）；
* `emit()` —— 往 `AGP_EVENTS` 指向的 JSONL 里追加一条结构化事件，Runner 的
  `summarize.py` 靠它生成 result.json（不再靠解析日志文本）；
* 退出码常量 —— 上游所有失败都是 `exit(0)`，这里统一成可区分的非零码。

环境变量清单（全部可选，缺省即上游行为）：

    AGP_STATE_DIR              picked_ids.txt 的目录（默认 = CWD）
    AGP_GSM8K_FILE             GSM8K 题库 jsonl（默认 = 本目录下的 GSM8K.jsonl）
    AGP_RESET_PICK_STATE       1=跑前删掉 picked_ids.txt（默认 1）
    AGP_URL / AGP_HOST_IP / AGP_HOST_PORT   目标服务寻址
    AGP_SEED_GLOBAL            1=在生成数据集前给全局 random 播种（默认 1）
    AGP_EVENTS                 sidecar JSONL 路径（空=不发事件）
    AGP_PHASE                  当前阶段名（prefix/full），写进事件里
    AGP_CONCURRENCY            当前点的目标并发，写进事件里
    AGP_AISBENCH_CMD           ais_bench 命令（默认 ais_bench）
    AGP_AISBENCH_EXTRA_ARGS    追加到 ais_bench 命令末尾的原始参数串
    AGP_NUM_WARMUPS            --num-warmups 的值（默认 0；留空=不传该参数）
    AGP_RUN_TIMEOUT            单次 ais_bench 调用的超时秒数（默认 0=不限）
    AGP_EXIT_DATASET/…         一般不用改，见下面的退出码常量
"""
from __future__ import annotations

import ipaddress
import json
import os
import random
import re
import sys
import time

# 与 runner.yaml 的 version 同步（summarize.py 把它写进 result.json.metadata）
TOOL_VERSION = "0.1.0"
VENDOR_COMMIT = "d9c24f3822dfdcf01545388561d6138e059851e4"

# ---------------------------------------------------------------- 退出码
EXIT_OK = 0
EXIT_USAGE = 2        # 参数/配置错误（平台 §5.8：输入错误用 2）
EXIT_DATASET = 3      # 数据集生成失败（题库耗尽 / tokenizer 返回空文本）
EXIT_AISBENCH = 4     # ais_bench 子进程非零退出
EXIT_TIMEOUT = 5      # ais_bench 子进程超时（AGP_RUN_TIMEOUT）

_DEFAULT_PORTS = {"http": 80, "https": 443}
_SCHEME_RE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://(?P<rest>.*)$")


# ---------------------------------------------------------------- 读取
def env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def env_int(name: str, default: int = 0) -> int:
    raw = env(name)
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name: str, default: float = 0.0) -> float:
    raw = env(name)
    try:
        return float(raw)
    except ValueError:
        return default


def env_bool(name: str, default: bool = True) -> bool:
    raw = env(name).strip().lower()
    if raw in ("1", "true", "yes", "y", "on"):
        return True
    if raw in ("0", "false", "no", "n", "off"):
        return False
    return default


# vendored 工具根（镜像里固定在 /runner/tool；本地测试可用 RUNNER_TOOL_DIR 覆盖）。
# 上游用 CWD 相对路径打开 default_api.py / GSM8K.jsonl，而 Runner 把 CWD 设成产物目录，
# 所以这类「读工具自带文件」的地方一律走 TOOL_DIR。
TOOL_DIR = os.path.abspath(env("RUNNER_TOOL_DIR", os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------- 寻址
class Target:
    """`AG_SERVED_URL` 归一化后的目标服务。

    ais_bench 的 API 模型有两套寻址字段（对 v3.1 实读源码确认）：

    * `host_ip` + `host_port`：运行时拼 `{http|https}://{host_ip}:{host_port}/`，
      但 `utils/config/build.py` 会校验 `host_ip` **必须是合法 IPv4/IPv6**，
      唯一放行的非 IP 值是 `"localhost"`；
    * `url`：完整 base url，给了就忽略 host_ip/host_port（校验仍然独立生效）。

    平台注入的 `AG_SERVED_URL` 常常是集群内 DNS 名（`http://svc.ns.svc:8000/v1`），
    直接写进 host_ip 会被 ais_bench 的校验拦下。于是这里按上游 tool 同样的规则决定写哪套：
    裸 IP 走 host_ip/host_port，其余走 url 并把 host_ip 置为 "localhost" 规避校验。
    """

    def __init__(self, raw: str = "", scheme: str = "", host: str = "",
                 port: int = 0, path: str = "", is_ip: bool = False, ip_version: int = 0):
        self.raw = raw
        self.scheme = scheme
        self.host = host
        self.port = port
        self.path = path
        self.is_ip = is_ip
        self.ip_version = ip_version

    @property
    def effective_scheme(self) -> str:
        return self.scheme or "http"

    @property
    def effective_port(self) -> int:
        return self.port or _DEFAULT_PORTS.get(self.effective_scheme, 80)

    @property
    def host_bracketed(self) -> str:
        return f"[{self.host}]" if self.ip_version == 6 else self.host

    def base_url(self) -> str:
        """给 ais_bench 的 `url` 字段（服务根，**不带** OpenAI 的 `/v1` 版本段）。

        ais_bench 拼的是 `urljoin(base_url, "v1/chat/completions")`，而不同版本对
        `base_url` 的处理不一样（两边都实测过的差异）：

        | 写法 | ≤3.1.20260330 | 3.1.20260630（本镜像） |
        |---|---|---|
        | `http://h:8000`        | …/v1/chat/completions ✓ | …/v1/chat/completions ✓ |
        | `http://h:8000/v1`     | …/v1/chat/completions ✓ | **…/v1/v1/chat/completions ✗ 404** |
        | `http://gw/openai`     | …/v1/chat/completions ✗（前缀被吃掉） | …/openai/v1/chat/completions ✓ |

        0630 的 `base_api._get_base_url()` 会给非空 path **补一个尾斜杠**
        （`urlunparse(path + "/")`），于是 `.../v1` 变成 `.../v1/`，再 urljoin 就成了
        双 `/v1`——kind 上真跑出来的 `error_info: "Not Found"` 就是它。
        两个版本唯一都成立的写法是**给服务根**：去掉末尾的 `v1` 段；
        若还有路径前缀（网关场景）则补尾斜杠，让 `urljoin` 不把前缀吃掉。
        """
        url = f"{self.effective_scheme}://{self.host_bracketed}:{self.effective_port}"
        path = (self.path or "").strip("/")
        if path.lower().endswith("v1"):
            path = path[:-2].strip("/")
        if path:
            url += "/" + path + "/"
        return url

    def use_url_field(self) -> bool:
        return bool(self.scheme or self.path or not self.is_ip)

    def model_fields(self):
        """→ 写进 ais_bench 模型配置的 (host_ip, host_port, url) 三件套。"""
        if self.use_url_field():
            # host_ip 只写 "localhost"：校验唯一放行的非 IP 值，运行时不用它
            return {"host_ip": "localhost", "host_port": self.effective_port,
                    "url": self.base_url()}
        return {"host_ip": self.host, "host_port": self.effective_port, "url": ""}

    def metrics_endpoint(self) -> str:
        """`/metrics` 的 `host:port`（cal_prefix_hit_rate 用；DNS 名在这里是合法的）。"""
        return f"{self.host_bracketed}:{self.effective_port}"

    def describe(self) -> str:
        mode = "url" if self.use_url_field() else "host_ip:host_port"
        return f"{self.base_url()}（{mode}）"


def parse_target(text: str) -> Target:
    """解析 `http://host:port[/path]` / `host:port` / `[::1]:8000` / 裸 IP。"""
    raw = (text or "").strip()
    target = Target(raw=raw)
    if not raw:
        return target
    rest = raw
    match = _SCHEME_RE.match(raw)
    if match:
        target.scheme = match.group("scheme").lower()
        rest = match.group("rest")
    head, _, path = rest.partition("/")
    target.path = path.strip("/")
    if "@" in head:                       # token@host 形式：丢掉 userinfo
        head = head.rpartition("@")[2]

    if not head.startswith("["):          # 裸 IPv6（无方括号）整体识别，避免被端口分割误伤
        try:
            ip = ipaddress.ip_address(head)
            target.host, target.is_ip, target.ip_version = str(ip), True, ip.version
            return target
        except ValueError:
            pass

    if head.startswith("["):              # [::1]:8000
        host, _, tail = head[1:].partition("]")
        target.host = host
        port_text = tail.lstrip(":")
    else:
        host, sep, port_text = head.rpartition(":")
        target.host = host if (sep and port_text.isdigit()) else head
    if not target.host:
        raise ValueError(f"无法从 {raw!r} 解析主机名")
    try:
        ip = ipaddress.ip_address(target.host)
        target.is_ip = True
        target.ip_version = ip.version
        target.host = str(ip)
    except ValueError:
        target.is_ip = False
    if port_text and port_text.isdigit():
        port = int(port_text)
        if not (0 < port < 65536):
            raise ValueError(f"端口越界：{port}（需在 1-65535）")
        target.port = port
    return target


def resolve_target() -> Target:
    """按 AGP_URL > (AGP_HOST_IP + AGP_HOST_PORT) 归一化目标服务。

    注意 SystemExit 的参数必须是**整数**：传字符串时 Python 只会打印它并按 1 退出，
    Runner 就无法把「没给地址」和「压测失败」区分开。
    """
    text = env("AGP_URL")
    if not text:
        host = env("AGP_HOST_IP")
        port = env("AGP_HOST_PORT")
        if not host:
            print("[prefix_env] 配置错误：未提供目标服务（AGP_URL 与 AGP_HOST_IP 都为空）",
                  file=sys.stderr)
            raise SystemExit(EXIT_USAGE)
        text = f"{host}:{port}" if port else host
    if "://" not in text:
        text = "http://" + text
    try:
        return parse_target(text)
    except ValueError as exc:
        print(f"[prefix_env] 配置错误：目标服务地址无法解析（{text!r}）：{exc}",
              file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


# ---------------------------------------------------------------- 状态文件
def state_dir() -> str:
    return os.path.abspath(env("AGP_STATE_DIR", os.getcwd()))


def picked_ids_path() -> str:
    return os.path.join(state_dir(), "picked_ids.txt")


def reset_pick_state() -> list:
    """按 AGP_RESET_PICK_STATE 删掉跨次累积的挑题记录，返回真正删掉的文件。

    这就是 README FAQ「生成数据集失败，请清空 picked ids」的自动化版本：
    上游用 picked_ids.txt 记住「哪些 GSM8K 行已经当过前缀」，文件只增不减，
    跑够次数后题库被自己标满 → pick_one() 返回 None → 数据集生成失败。
    Runner 每次都从干净状态开始（同一 seed 因此可复现）。
    """
    removed = []
    if not env_bool("AGP_RESET_PICK_STATE", True):
        return removed
    for path in (picked_ids_path(),):
        try:
            if os.path.exists(path):
                os.remove(path)
                removed.append(path)
        except OSError as exc:               # 删不掉不硬失败：让下游报错更完整
            print(f"[prefix_env] 警告：无法删除 {path}: {exc}")
    return removed


def gsm8k_pool() -> str:
    """GSM8K 题库路径（上游写死 `./GSM8K.jsonl`，依赖 CWD）。"""
    return os.path.abspath(env("AGP_GSM8K_FILE", os.path.join(TOOL_DIR, "GSM8K.jsonl")))


# ---------------------------------------------------------------- 可复现性
def seed_rng(seed: int) -> None:
    """给全局 `random` 播种。

    上游的 `--seed` 只影响「前缀后插入的 3 个随机 token」
    （`generate_unique_tokens()` 里才 `random.seed()`），而 gsm8k 挑题走的是
    `DataPicker.pick_one()` 里的 `random.choice/randint`，**没播种** →
    同一份参数两次跑出来的数据集不同，结果无法复现。
    这里在数据集生成入口先播一次（seed=0 表示「不固定」，保持不可复现的语义）。
    """
    if env_bool("AGP_SEED_GLOBAL", True) and seed:
        random.seed(int(seed))


# ---------------------------------------------------------------- sidecar
def emit(kind: str, **payload) -> None:
    """把一条结构化事件追加到 `AGP_EVENTS`（未设置时静默跳过）。

    Runner 的 summarize.py 只读这些事件，不再正则解析 aisbench.log。
    """
    path = env("AGP_EVENTS")
    if not path:
        return
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind}
    record.update(payload)
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"[prefix_env] 警告：sidecar 写入失败 {path}: {exc}")


def current_phase() -> str:
    return env("AGP_PHASE", "full")


def current_concurrency() -> int:
    return env_int("AGP_CONCURRENCY", 0)

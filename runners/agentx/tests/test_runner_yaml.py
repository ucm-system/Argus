#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Argus
# SPDX-License-Identifier: Apache-2.0
"""runner.yaml 的契约自检（不依赖 Go 服务）。

守两件事：
  1. 附录 C 的硬约束：role / on_failure / entry / input_format / placeholder（D-67 单行 JSON）/
     parameters 类型 / secret env 不带 default；
  2. **默认值漂移**：runner.yaml 的 parameters[].default 必须与 agentx_runner.py 的 DEFAULTS
     完全一致 —— 两份默认值不一致时，平台上（平台只校类型）与独立运行时（Runner 兜底）
     会跑出不同的负载，这种 bug 很难在结果里看出来。

跑法：py -3.11 runners/agentx/tests/test_runner_yaml.py
退出码：0 通过或跳过（缺 PyYAML）；1 不通过。
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER_DIR = HERE.parent
YAML_PATH = RUNNER_DIR / "runner.yaml"
ALLOWED_PARAM_TYPES = {"string", "number", "integer", "boolean", "object", "array"}
STATISTIC_UNITS = {"ms", "s", "req_per_s", "tokens_per_s", "tokens", "percent", "count", "score"}

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name}" + (f"：{detail}" if detail else ""))


def load_runner_module():
    spec = importlib.util.spec_from_file_location(
        "agentx_runner_for_yaml", RUNNER_DIR / "agentx_runner.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    try:
        import yaml
    except ImportError:
        print("SKIP 缺 PyYAML：py -3.11 -m pip install pyyaml")
        return 0

    doc = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    runner = load_runner_module()

    # --- 附录 C 的硬约束 -----------------------------------------------------
    check("name 合法（^[a-z0-9-]{1,63}$）", bool(re.fullmatch(r"[a-z0-9-]{1,63}", str(doc.get("name") or ""))))
    # 仓库里目录名就是 Runner 名；容器内 runner.yaml 落在 /runner（不叫 agentx），跳过这条。
    if RUNNER_DIR.name not in ("runner", "agentx-runner-stub"):
        check("name 与目录一致", doc.get("name") == RUNNER_DIR.name, str(doc.get("name")))
    check("version 是语义化版本", isinstance(doc.get("version"), str)
          and len(str(doc["version"]).split(".")) == 3, str(doc.get("version")))
    check("role 是 test", doc.get("role") == "test", str(doc.get("role")))
    check("on_failure 是 continue（测试类不该终止 Task）", doc.get("on_failure") == "continue")
    check("entry 是 exec form 数组", doc.get("entry") == ["/runner/run.sh"], str(doc.get("entry")))
    check("input_format 是 jsonl", doc.get("input_format") == "jsonl")
    check("image 非空", bool(str(doc.get("image") or "").strip()))
    check("resources 带 cpu/memory/gpu", all(k in (doc.get("resources") or {})
                                            for k in ("cpu", "memory", "gpu")))
    check("consumes 声明了两个公共参数",
          set(doc.get("consumes") or []) == {"AG_SERVED_URL", "AG_SERVED_MODEL_NAME"})
    check("exports 为空（测试类不产出下游 key）", (doc.get("exports") or []) == [])
    check("没有 appendix C 禁止字段",
          not ({"timeout_seconds", "outputs", "requires", "executors", "retry"} & set(doc)))

    # --- placeholder（D-67：单行、合法、与 input_format 同格式） --------------
    placeholder = str(doc.get("placeholder") or "")
    check("placeholder 是单行", "\n" not in placeholder)
    try:
        parsed_placeholder = json.loads(placeholder)
    except json.JSONDecodeError as exc:
        parsed_placeholder = None
        check("placeholder 是合法 JSON", False, str(exc))
    if parsed_placeholder is not None:
        check("placeholder 是合法 JSON", isinstance(parsed_placeholder, dict))
        declared = {p["name"] for p in doc.get("parameters") or []}
        check("placeholder 只用声明过的字段", set(parsed_placeholder) <= declared,
              str(set(parsed_placeholder) - declared))

    # --- parameters ----------------------------------------------------------
    parameters = doc.get("parameters") or []
    names = [p.get("name") for p in parameters]
    check("参数名唯一", len(names) == len(set(names)))
    check("参数类型都在平台枚举内",
          all(p.get("type") in ALLOWED_PARAM_TYPES for p in parameters),
          str([p.get("name") for p in parameters if p.get("type") not in ALLOWED_PARAM_TYPES]))
    check("每个参数都有 description",
          all(str(p.get("description") or "").strip() for p in parameters))
    check("参数名都是 snake_case（平台/UI 依赖）",
          all(n and n.replace("_", "").isalnum() and n.islower() for n in names))

    # --- 默认值漂移（最容易被忽略的坑） --------------------------------------
    yaml_defaults = {p["name"]: p.get("default") for p in parameters}
    check("参数集合与 DEFAULTS 一致",
          set(yaml_defaults) == set(runner.DEFAULTS),
          f"仅 yaml：{set(yaml_defaults) - set(runner.DEFAULTS)}；仅代码：{set(runner.DEFAULTS) - set(yaml_defaults)}")
    drift = {name: (yaml_defaults.get(name), runner.DEFAULTS.get(name))
             for name in set(yaml_defaults) & set(runner.DEFAULTS)
             if yaml_defaults.get(name) != runner.DEFAULTS.get(name)}
    check("yaml 默认值与代码默认值一致", not drift, str(drift))

    # --- env 声明（§12.4） ---------------------------------------------------
    env = doc.get("env") or {}
    check("secret env 不带 default", all(not (v or {}).get("default")
                                        for v in env.values() if (v or {}).get("secret")))
    check("每个 env 都有 description", all(str((v or {}).get("description") or "").strip()
                                          for v in env.values()))
    check("声明了 ENDPOINT_API_KEY 且标记 secret",
          (env.get("ENDPOINT_API_KEY") or {}).get("secret") is True)

    # --- 指标口径（附录 A） ---------------------------------------------------
    units = {unit for _n, unit, _s, _k, _sk in runner.METRIC_MAP}
    check("指标单位都在推荐集合内", units <= STATISTIC_UNITS, str(units - STATISTIC_UNITS))
    check("指标名都是 snake_case",
          all(name.replace("_", "").isalnum() and name.islower()
              for name, *_rest in runner.METRIC_MAP))

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Runtime shared verbatim with aisbench-perf (independent Docker contexts)."""
from __future__ import annotations

import argparse
import ast
from collections import deque
from collections.abc import Mapping
import hashlib
import importlib.metadata
import json
import math
import os
from numbers import Integral
from pathlib import Path
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

VERSION = "0.3.6"
HERE = Path(__file__).resolve().parent
MODEL = "testplane-model"
DATA = "testplane-data"


class Failure(Exception):
    def __init__(self, message, reason="invalid_input", exit_code=2):
        super().__init__(message)
        self.reason, self.exit_code = reason, exit_code


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure(message)

    def parse_args(self, args=None, namespace=None):
        args = list(sys.argv[1:] if args is None else args)
        seen = set()
        for arg in args:
            if arg.startswith("--"):
                key = arg.split("=", 1)[0]
                if key in seen and key != "--metrics_endpoint":
                    self.error(f"参数重复：{key}")
                seen.add(key)
        parsed = super().parse_args(args, namespace)
        parsed.explicit = seen
        return parsed


def integer(minimum=1, maximum=2147483647):
    def parse(value):
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("必须是整数")
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"必须在 {minimum}..{maximum} 之间")
        return number
    return parse


def number(minimum=0, maximum=float("inf"), open_min=False):
    def parse(value):
        try:
            result = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError("必须是数值")
        if not math.isfinite(result) or result < minimum or result > maximum or (open_min and result == minimum):
            raise argparse.ArgumentTypeError(f"数值超出允许范围：{value}")
        return result
    return parse


def boolean(value):
    if value not in ("true", "false"):
        raise argparse.ArgumentTypeError("必须写 true 或 false")
    return value == "true"


def public_args(parser):
    parser.epilog = "平台输入：--params-file FILE > AG_PARAMS_FILE > /workspace/input/params.jsonl；每行一个业务参数对象。"
    for name, env, help_text in (("url", "AG_SERVED_URL", "服务 base URL"),
                                 ("model_name", "AG_SERVED_MODEL_NAME", "请求中的模型名"),
                                 ("model_path", "AG_MODEL_PATH", "本地 tokenizer 绝对路径")):
        parser.add_argument("--" + name, default=os.environ.get(env), required=not bool(os.environ.get(env)), help=help_text)
    parser.add_argument("--concurrency", type=integer(1, 100000), default=1)
    parser.add_argument("--request_rate", type=number(0, 64000), default=0)
    parser.add_argument("--seed", type=integer(0), default=1)


def validate_url(value, metrics=False):
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        raise Failure("URL 格式或端口不正确")
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise Failure("URL 必须是 http(s)，不能包含凭据、查询参数或 fragment")
    if any(c.isspace() for c in value):
        raise Failure("URL 不能包含空白")
    path = parts.path.rstrip("/")
    if metrics:
        if not path.endswith("/metrics"):
            raise Failure("metrics_endpoint 必须是完整 /metrics 地址")
    else:
        if path.endswith("/chat/completions"):
            raise Failure("url 应填写 base URL，例如 http://host:8000/v1")
        if not path.endswith("/v1"):
            path += "/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def input_path(value, directory=False):
    path = Path(value)
    if not path.is_absolute() or not (path.is_dir() if directory else path.is_file()):
        raise Failure(f"需要可读的绝对{'目录' if directory else '文件'}路径：{value}")
    return path


def read_jsonl(path):
    rows = []
    try:
        with Path(path).open(encoding="utf-8") as handle:
            for index, line in enumerate(handle, 1):
                row = json.loads(line)
                if not isinstance(row, dict) or not isinstance(row.get("question"), str) or not row["question"].strip():
                    raise ValueError(f"第 {index} 行需要非空 question")
                rows.append(row)
    except (ValueError, OSError) as exc:
        raise Failure(f"数据集读取失败：{exc}") from exc
    if not rows:
        raise Failure("数据集为空")
    return rows


def select_rows(rows, limit):
    if limit and limit > len(rows):
        raise Failure(f"要求 {limit} 条，文件只有 {len(rows)} 条")
    return rows[:limit] if limit else rows


def redact(text):
    for name in ("ENDPOINT_API_KEY", "METRICS_API_KEY"):
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(redact(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)), encoding="utf-8")
    temporary.replace(path)


def jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(redact(json.dumps(row, ensure_ascii=False, allow_nan=False)) + "\n")


def digest(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def metric(name, value, unit, statistic, labels, sample_size=None, notes=None):
    item = dict(name=name, value=value, unit=unit, statistic=statistic, labels=dict(labels))
    if sample_size is not None:
        item["sample_size"] = sample_size
    if notes:
        item["notes"] = notes
    return item


def tokenizer_info(path):
    try:
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(str(path), local_files_only=True, trust_remote_code=False)
    except Exception as exc:
        raise Failure(f"无法加载本地 tokenizer：{exc}") from exc
    if not tokenizer.chat_template:
        raise Failure("tokenizer 必须包含与服务一致的 chat_template")
    files = {str(p.relative_to(path)): digest(p) for p in sorted(path.rglob("*"))
             if p.is_file() and (p.name.startswith(("tokenizer", "vocab", "merges", "special_tokens", "added_tokens", "chat_template")) or p.name in ("config.json", "generation_config.json"))}
    return tokenizer, files


def chat_token_ids(tokenizer, messages, **template_kwargs):
    options = dict(template_kwargs, tokenize=True, add_generation_prompt=True, return_dict=False)
    value = tokenizer.apply_chat_template(messages, **options)
    if isinstance(value, Mapping):
        if "input_ids" not in value:
            raise Failure("聊天模板返回的字典缺少 input_ids")
        value = value["input_ids"]
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, (list, tuple)) and len(value) == 1 and isinstance(value[0], (list, tuple)):
        value = value[0]
    if not isinstance(value, (list, tuple)) or any(not isinstance(token, Integral) or isinstance(token, bool) for token in value):
        raise Failure("聊天模板必须返回单条输入的整数 token ID 列表")
    return [int(token) for token in value]


def aisbench_info():
    """Record installed versions without gating execution on environment checks."""
    versions = {"python_version": sys.version.split()[0]}
    for field, package in (("aisbench_version", "ais_bench_benchmark"), ("transformers_version", "transformers")):
        try:
            versions[field] = importlib.metadata.version(package)
        except Exception:
            # Source checkouts or broken distribution metadata must not block a run.
            versions[field] = None
    return versions


def official_template(preset):
    root = Path(importlib.import_module("ais_bench.benchmark").__file__).parent
    name = "gsm8k_gen_4_shot_cot_str.py" if preset == "gsm8k_4shot_cot" else "gsm8k_gen_0_shot_cot_str.py"
    source = (root / "configs/datasets/gsm8k" / name).read_text(encoding="utf-8")
    templates = [ast.literal_eval(node.value) for node in ast.walk(ast.parse(source))
                 if isinstance(node, ast.keyword) and node.arg == "template"]
    if len(templates) != 1 or not isinstance(templates[0], str):
        raise Failure("AISBench 提问模板结构已变化", "unsupported_backend")
    return templates[0]


def write_config(path, args, dataset_dir, template, perf, concurrency=None, output_len=None):
    generation = {"temperature": 0 if perf else args.temperature,
                  "ignore_eos": perf, "seed": args.seed}
    if perf and getattr(args, "thinking", "default") != "default":
        generation["chat_template_kwargs"] = {"enable_thinking": args.thinking == "on"}
    if not perf:
        generation["top_p"] = args.top_p
        if args.thinking != "default":
            generation["chat_template_kwargs"] = {"enable_thinking": args.thinking == "on"}
    model = dict(type="testplane_model.Chat", attr="service", abbr=MODEL,
                 path=args.model_path, model=args.model_name, url=args.url,
                 stream=perf and args.test_type == "stream", retry=1,
                 batch_size=concurrency or args.concurrency,
                 request_rate=0 if concurrency is not None else args.request_rate,
                 max_out_len=output_len or args.output_len, generation_kwargs=generation)
    dataset = dict(type="ais_bench.benchmark.datasets.GSM8KDataset", abbr=DATA,
                   path=str(dataset_dir), reader_cfg=dict(input_columns=["question"], output_column="answer"),
                   infer_cfg=dict(prompt_template=dict(type="ais_bench.benchmark.openicl.icl_prompt_template.PromptTemplate", template=template),
                                  retriever=dict(type="ais_bench.benchmark.openicl.icl_retriever.ZeroRetriever"),
                                  inferencer=dict(type="testplane_inferencer.GenInferencer")),
                   eval_cfg=dict(evaluator=dict(type="ais_bench.benchmark.datasets.Gsm8kEvaluator"),
                                 pred_postprocessor=dict(type="ais_bench.benchmark.datasets.gsm8k_postprocess"),
                                 dataset_postprocessor=dict(type="ais_bench.benchmark.datasets.gsm8k_dataset_postprocess")))
    summarizer = dict(type="ais_bench.benchmark.summarizers.DefaultPerfSummarizer", attr="performance",
                      calculator=dict(type="ais_bench.benchmark.calculators.DefaultPerfMetricCalculator")) if perf else dict(type="ais_bench.benchmark.summarizers.DefaultSummarizer", attr="accuracy")
    # repr serializes literals; none of the user's strings becomes executable code.
    path.write_text(f"models = {[model]!r}\ndatasets = {[dataset]!r}\nsummarizer = {summarizer!r}\n", encoding="utf-8")


class Execution:
    def __init__(self, name):
        self.output = Path(os.environ.get("AG_OUTPUT_DIR") or os.environ.get("TESTPLANE_OUTPUT_DIR", "/workspace/output")).resolve()
        self.result = dict(schema_version="2.0", runner=dict(name=name, version=VERSION),
                           started_at=now(), status="failed", metrics=[], metadata={}, notes="")
        self.result.update(task_id=os.environ.get("AG_TASK_ID", "local"), attempt_no=1, runner_seq=1)
        self.child = None
        self.last_aisbench_directory = None
        self.started = time.monotonic()
        self.prepared = False

    def prepare(self):
        self.output.mkdir(parents=True, exist_ok=True)
        if any(self.output.iterdir()):
            raise Failure("输出目录必须为空；请为这次执行选择新目录")
        (self.output / "logs").mkdir()
        (self.output / "raw").mkdir()
        self.prepared = True
        for key, env in (("attempt_no", "AG_ATTEMPT"), ("runner_seq", "AG_RUNNER_SEQ")):
            try:
                self.result[key] = integer()(os.environ.get(env, "1"))
            except argparse.ArgumentTypeError as exc:
                raise Failure(f"{env}: {exc}") from exc

    def log(self, message):
        message = redact(str(message))
        print(message, flush=True)
        if self.prepared:
            with (self.output / "logs/runner.log").open("a", encoding="utf-8") as handle:
                handle.write(now() + " " + message + "\n")

    def cancel(self, signum, frame):
        if self.child and self.child.poll() is None:
            if os.name == "posix":
                os.killpg(self.child.pid, signal.SIGTERM)
            else:
                self.child.terminate()
            try:
                self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    os.killpg(self.child.pid, signal.SIGKILL)
                else:
                    self.child.kill()
        raise Failure(f"收到停止信号 {signum}", "canceled", 128 + signum)

    def run_aisbench(self, directory, args, dataset_dir, template, perf, concurrency=None, output_len=None):
        directory.mkdir(parents=True, exist_ok=False)
        self.last_aisbench_directory = directory
        config = directory / "config.py"
        audit = directory / "requests"
        audit.mkdir()
        write_config(config, args, dataset_dir, template, perf, concurrency, output_len)
        command = [sys.executable, "-m", "ais_bench.benchmark.cli.main", str(config),
                   "--mode", "perf" if perf else "all", "--work-dir", str(directory / "aisbench"),
                   "--num-warmups", "0", "--max-num-workers", "1", "--debug"]
        if not perf:
            command.append("--dump-eval-details")
        return self.run_command(directory, command, cwd=directory, audit=audit)

    def run_command(self, directory, command, cwd, audit=None):
        self.last_aisbench_directory = directory
        environment = dict(os.environ, PYTHONPATH=str(HERE) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                           PYTHONUNBUFFERED="1", HF_HUB_OFFLINE="1", HF_DATASETS_OFFLINE="1")
        environment.pop("TESTPLANE_AUDIT_DIR", None)
        if audit is not None:
            environment["TESTPLANE_AUDIT_DIR"] = str(audit)
        # Metrics credentials have no purpose in the AISBench process.
        environment.pop("METRICS_API_KEY", None)
        started_at = now()
        self.log(f"启动 AISBench：{directory.relative_to(self.output)}")
        with (directory / "aisbench.log").open("w", encoding="utf-8") as log:
            self.child = subprocess.Popen(command, cwd=cwd, env=environment, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                          start_new_session=os.name == "posix")
            try:
                for line in self.child.stdout:
                    log.write(redact(line))
                    log.flush()
                code = self.child.wait()
            finally:
                self.child.stdout.close()
        dump(directory / "execution.json", dict(started_at=started_at, finished_at=now(), returncode=code,
                                                command=command, cwd=str(cwd)))
        self.child = None
        return code

    def finish(self, error=None):
        if not self.prepared:
            self.log(error or "输出目录未准备")
            return error.exit_code if isinstance(error, Failure) else 1
        if not error:
            seen = set()
            for item in self.result["metrics"]:
                key = (item["name"], item["statistic"], json.dumps(item["labels"], sort_keys=True))
                if key in seen or (item["value"] is not None and not math.isfinite(item["value"])):
                    error = Failure("标准结果含重复或无效指标", "upstream_result_invalid", 1)
                    break
                seen.add(key)
        if error:
            self.result["error"] = dict(code="CANCELED" if error.reason == "canceled" else "RUNNER_NONZERO_EXIT",
                                         message=redact(str(error)), retryable=False, detail=dict(reason=error.reason))
            self.log(error)
            if self.last_aisbench_directory is not None:
                log_path = self.last_aisbench_directory / "aisbench.log"
                self.log(f"AISBench 详细日志：{log_path}")
                try:
                    with log_path.open(encoding="utf-8", errors="replace") as handle:
                        tail = "".join(deque(handle, maxlen=100))[-16000:]
                    if tail:
                        self.log("AISBench 最后 100 行（最多 16000 字符）：\n" + tail)
                except OSError as exc:
                    self.log(f"无法读取 AISBench 日志：{exc}")
        else:
            self.result["status"] = "succeeded"
        self.result.update(finished_at=now(), duration_ms=int((time.monotonic() - self.started) * 1000))
        self.log(f"结果将保存到：{self.output / 'result.json'}")
        # Sanitize upstream text outputs too; auth was injected only into model instances.
        for path in self.output.rglob("*"):
            if path.is_file() and path.suffix in (".json", ".jsonl", ".py", ".log", ".out", ".txt", ".csv"):
                with path.open(encoding="utf-8", errors="replace") as source, path.with_suffix(path.suffix + ".redacted").open("w", encoding="utf-8") as target:
                    for line in source:
                        target.write(redact(line))
                path.with_suffix(path.suffix + ".redacted").replace(path)
        # The platform indexes files; directory declarations would be dangling artifacts.
        self.result["artifacts"] = [dict(name=path.relative_to(self.output).as_posix(),
                                         path=path.relative_to(self.output).as_posix(), size_bytes=path.stat().st_size)
                                    for path in sorted(self.output.rglob("*")) if path.is_file()]
        dump(self.output / "result.json", self.result)
        return error.exit_code if error else 0


def input_cases(argv):
    """Convert platform JSONL rows into argv without shell evaluation."""
    argv = list(sys.argv[1:] if argv is None else argv)
    selector = Parser(add_help=False)
    selector.add_argument("--params-file")
    selected, cli = selector.parse_known_args(argv)
    if "--help" in cli or "-h" in cli:
        return [cli]
    filename = selected.params_file or os.environ.get("AG_PARAMS_FILE")
    default = Path("/workspace/input/params.jsonl")
    path = Path(filename) if filename else default if default.is_file() else None
    if path is None:
        return [cli]
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        raise Failure(f"参数文件读取失败：{exc}") from exc
    overridden = {arg.split("=", 1)[0][2:] for arg in cli if arg.startswith("--")}
    cases = []
    for row in rows or [{}]:
        if not isinstance(row, dict):
            raise Failure("参数文件每行必须是 JSON 对象")
        if set(row) & {"url", "model_name", "model_path"}:
            raise Failure("公共参数 url/model_name/model_path 请通过 AG_* 或 CLI 传入")
        values = []
        for key, value in row.items():
            if key in overridden:
                continue
            if not key or key.startswith("-") or not key.replace("_", "").isalnum():
                raise Failure("非法参数名")
            if value is None:
                continue
            if key in ("metrics_endpoint", "pod_info") and isinstance(value, list):
                if key == "pod_info":
                    values.extend(["--pod_info"] + [str(v) for v in value])
                else:
                    values.extend("--metrics_endpoint=" + str(v) for v in value)
            else:
                text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list, bool)) else str(value)
                values.append("--" + key + "=" + text)
        cases.append(values + cli)
    return cases


def run_main(name, parser, work, argv=None):
    execution = Execution(name)
    try:
        cases = [parser().parse_args(values) for values in input_cases(argv)]
        execution.prepare()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, execution.cancel)
        root_output = execution.output
        case_metadata = []
        for case_id, args in enumerate(cases, 1):
            args.url = validate_url(args.url)
            if not args.model_name.strip():
                raise Failure("model_name 不能为空")
            input_path(args.model_path, directory=True)
            start = len(execution.result["metrics"])
            if len(cases) > 1:
                execution.output = root_output / f"cases/{case_id}"
                (execution.output / "logs").mkdir(parents=True)
            try:
                work(execution, args)
            finally:
                for item in execution.result["metrics"][start:]:
                    item["labels"]["case"] = case_id
                case_metadata.append(dict(case=case_id, output=str(execution.output.relative_to(root_output)),
                                          metadata=json.loads(json.dumps({k: v for k, v in execution.result["metadata"].items() if k != "cases"})),
                                          workload=execution.result.get("workload")))
                execution.output = root_output
                if len(cases) > 1:
                    execution.result["metadata"]["cases"] = case_metadata
                    if any("metrics_complete" in c["metadata"] for c in case_metadata):
                        execution.result["metadata"]["metrics_complete"] = all(c["metadata"].get("metrics_complete", False) for c in case_metadata)
        return execution.finish()
    except Failure as exc:
        if not execution.prepared and (not execution.output.exists() or not any(execution.output.iterdir())):
            execution.prepare()
        return execution.finish(exc)
    except Exception as exc:
        return execution.finish(Failure(str(exc), "upstream_result_invalid", 1))

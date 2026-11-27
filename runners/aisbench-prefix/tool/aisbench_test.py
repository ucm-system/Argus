import os, errno
import sys
import argparse
import re
import logging
import signal
import subprocess
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import *
from generate_dataset import *
from save_file import get_data, save_csv, save_log
from cal_prefix_hit_rate import *
import prefix_env
logging.getLogger().setLevel(logging.INFO)

# 当前写进 ais_bench 模型配置的 batch_size（= 本阶段实际并发）。
# 预热阶段用的是 dp/prefix_num，全量阶段用的是 --concurrency，
# sidecar 里必须带上它，否则 summarize.py 分不清一行指标属于哪个并发。
EFFECTIVE_BATCH_SIZE = ""


def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input_len', type=int, default=3500, help="input token length")
    parser.add_argument("--output_len", type=str, default="1500", help="output token length")
    parser.add_argument("--data_num", type=int, default=8192, help="dataset number")
    parser.add_argument("--concurrency", type=str, default="2048", help="max concurrency")
    parser.add_argument("--request_rate", type=str, default="0", help="request rate")
    parser.add_argument("--test_type", type=str, default="stream", help="text or stream")
    parser.add_argument("--dataset", type=str, default="none", help="dataset path")
    parser.add_argument("--repeat", type=int, default=1, help="number of test repeat times")
    parser.add_argument("--enable_think", action='store_true', default=False, help="enable thinking for ds v3.1")
    parser.add_argument("--test_accuracy", action='store_true', default=False, help="test accuracy")
    parser.add_argument("--npu_num", type=int, default=1, help="npu numbers")
    parser.add_argument("--dataset_type", type=str, default="normal", help="normal or prefix_cache")
    parser.add_argument("--prefix_num", type=int, default=1, help="prefix numbers")
    parser.add_argument("--repeat_rate", type=str, default="0", help="dataset repeat rate")
    parser.add_argument("--prefix_test", action='store_true', default=False, help="test prefix dataset firstly")
    parser.add_argument("--seed", type=int, default=1, help="dataset random seed")
    parser.add_argument("--dp", type=int, default=1, help="dp size")
    parser.add_argument("--length_mean", type=int, default=None, help="gaussian mean for variable length")
    parser.add_argument("--length_std", type=float, default=None, help="gaussian std for variable length")
    parser.add_argument("--length_min", type=int, default=None, help="min length for uniform range or gaussian clip")
    parser.add_argument("--length_max", type=int, default=None, help="max length for uniform range or gaussian clip")
    return parser.parse_args()

def symlink_force(target, link_name):
    logging.info(f"make symlink: {link_name} ==> {target}")
    try:
        os.symlink(target, link_name)
    except OSError as e:
        if e.errno == errno.EEXIST:
            os.remove(link_name)
            os.symlink(target, link_name)
        else:
            raise e

def create_gsm8k_dataset(dataset_type, input_len, data_num, model_path, dataset_path, prefix_num, repeat_rate, seed,
                         length_mean=None, length_std=None, length_min=None, length_max=None):
    if not os.path.exists(dataset_path):
        logging.warning(f"dataset work path {dataset_path} not exist. creating it automatically.")
        os.makedirs(dataset_path, exist_ok=True)
        logging.info(f"dataset work path {dataset_path} created.")

    base_name = os.path.basename(os.path.normpath(model_path))
    if dataset_type == "prefix_cache":
        prefix_jsonl_path, dataset_jsonl_path = create_multi_prefix_dataset(model_path,input_len,data_num,dataset_path,1,dp,repeat_rate,seed,prefix_num,
                                                                             length_mean, length_std, length_min, length_max)
        logging.info("[完成] 数据集已生成：")
        logging.info(f"  - 公共前缀：{prefix_jsonl_path}  (行数={dp*prefix_num})")
        logging.info(f"  - 数据集：  {dataset_jsonl_path} (行数={data_num})")
        logging.info("[信息] 配置：")
        logging.info(f"  tokens(单条长度)={input_len}, prefix_ratio(前缀重复率)={repeat_rate}")
        if length_mean is not None and length_std is not None:
            logging.info(f"  length_dist=gaussian(mean={length_mean}, std={length_std}, min={length_min}, max={length_max})")
        elif length_min is not None and length_max is not None:
            logging.info(f"  length_dist=uniform_int([{length_min}, {length_max}])")
        else:
            logging.info("  length_dist=fixed")
    else:
        dataset_name = "GSM8K-in" + str(input_len) + "-num" + str(data_num) + "-" + base_name + ".jsonl"
        logging.info(f"dataset_name: {dataset_name}")
        dataset_jsonl_path = os.path.join(dataset_path, dataset_name)
        prefix_jsonl_path = ""
        # 判断数据集是否存在
        if not os.path.exists(dataset_jsonl_path):
            logging.warning(f"Dataset {dataset_name} is not exist. Start create dataset")
            # create_data(input_len, data_num, model_path, dataset_path)
            prefix_jsonl_path, dataset_jsonl_path = create_multi_prefix_dataset(model_path,input_len,data_num,dataset_path,0,dp,0,seed,prefix_num,
                                                                                 length_mean, length_std, length_min, length_max)
            logging.info(f"Dataset {dataset_name} created.")
        else:
            logging.info(f"Dataset {dataset_name} exist.")
    return prefix_jsonl_path, dataset_jsonl_path

def generate_aisbench_command(DEFAULT_PERFORMANCE_TEST):
    """拼 ais_bench 命令（**本地改动**：命令本体与开关全部可配）。

    - `AGP_AISBENCH_CMD`（默认 `ais_bench`）：镜像里是真命令；离线回归指向桩程序。
      上游写死 `ais_bench`，Runner 就没法在不装 ais_bench 的镜像里做链路自检。
    - `AGP_NUM_WARMUPS`（默认 `0`）：上游硬编码 `--num-warmups 0`，而 FAQ #3 的处置是
      「旧版 ais_bench 不认识这个参数就去源码里删掉它」。现在留空即不传，不用改代码。
    - `AGP_AISBENCH_EXTRA_ARGS`：透传本工具未建模的 ais_bench 开关
      （run.sh 用它追加 `--config-dir <work_path>/ais_bench/benchmark/configs`）。
    """
    aisbench_cmd = prefix_env.env("AGP_AISBENCH_CMD", "ais_bench")
    num_warmups = prefix_env.env("AGP_NUM_WARMUPS", "0")
    extra_args = prefix_env.env("AGP_AISBENCH_EXTRA_ARGS")
    parts = [aisbench_cmd, "--models vllm_api_chat_temp",
             "--datasets gsm8k_gen_0_shot_cot_str_perf"]
    if test_accuracy:
        parts.append("--work-dir")
        parts.append(shlex_quote(OUTPUT_DIR))
        parts.append("--dump-eval-details")
    else:
        parts += ["--mode", "perf", "--summarizer", DEFAULT_PERFORMANCE_TEST,
                  "--work-dir", shlex_quote(OUTPUT_DIR), "--debug"]
        if num_warmups:
            parts += ["--num-warmups", num_warmups]
    if extra_args:
        parts.append(extra_args)
    if not test_accuracy:
        # tee 到 CWD 的 aisbench.log / aisbench_all.log（save_file.get_data 读前者）。
        parts.append("2>&1 | tee -a aisbench.log | tee -a aisbench_all.log")
    return " ".join(parts)


def shlex_quote(value):
    """把路径安全地放进 shell 命令串（OUTPUT_DIR 带空格时不再拆词）。"""
    return "'" + str(value).replace("'", "'\\''") + "'"


def run_aisbench(cmd):
    """执行一次 ais_bench（**本地改动**，三处上游没有的处理）：

    1. **pipefail**：上游 `subprocess.run(cmd, shell=True, check=True)` 跑的是
       `ais_bench … | tee … | tee …`，shell 取的是**最后一个 tee 的退出码**——
       ais_bench 自己崩了也照样返回 0，于是「失败」被吞掉，后面 get_data 只能拿到
       99999 哨兵值，Runner 会上报一个 succeeded 但全是垃圾数的 Task。
    2. **超时**：`AGP_RUN_TIMEOUT` 秒；超时按进程组杀（shell + tee + ais_bench 一起），
       否则长上下文场景只能等平台 watchdog 把整个 Job 掐掉。
    3. **退出码**：ais_bench 非零 → 4；超时 → 5；run.sh 据此区分失败类型。
    """
    timeout = prefix_env.env_float("AGP_RUN_TIMEOUT", 0.0)
    proc = subprocess.Popen(["bash", "-o", "pipefail", "-c", cmd],
                            start_new_session=True)
    try:
        rc = proc.wait(timeout=timeout if timeout > 0 else None)
    except subprocess.TimeoutExpired:
        logging.error(f"ais_bench 超时（AGP_RUN_TIMEOUT={timeout}s），按进程组终止")
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.wait()
        prefix_env.emit("aisbench", phase=prefix_env.current_phase(),
                        concurrency=EFFECTIVE_BATCH_SIZE,
                        exit_code=None, timeout=True, command=cmd)
        raise SystemExit(prefix_env.EXIT_TIMEOUT)
    prefix_env.emit("aisbench", phase=prefix_env.current_phase(),
                    concurrency=EFFECTIVE_BATCH_SIZE,
                    exit_code=rc, timeout=False, command=cmd)
    if rc != 0:
        logging.error(f"ais_bench 退出码 {rc}（详见 aisbench.log 与 ais_bench 的 work-dir）")
        raise SystemExit(prefix_env.EXIT_AISBENCH)
    return rc

def generate_test_dataset(src_file, dst_dir):
    dst_file = os.path.join(dst_dir, "test.jsonl")
    logging.info(f"src_file: {src_file}")
    logging.info(f"dst_file: {dst_file}")
    # 使用软连接
    symlink_force(src_file, dst_file)
    return

def save_result(request_rate, npu_num, repeat_rate, phase="full"):
    """采集本次 aisbench.log 的汇总并落 CSV（**本地改动**：多带 phase 与 sidecar）。

    Runner 的 result.json 不解析 CSV/日志文本，而是读 `AGP_EVENTS` 里的结构化事件，
    所以这里把 get_data() 拿到的那份 dict 原样发出去。上游会调两次 save_result
    （预热阶段一次、全量阶段一次），没有 phase 就分不清哪一行属于哪个阶段。
    """
    aisbench_log_dir = "aisbench.log"
    filename = "aisbench_result.csv"
    ans, log_dir=get_data(aisbench_log_dir,request_rate,npu_num,repeat_rate)
    save_log(aisbench_log_dir, log_dir)
    save_csv(ans, filename)
    prefix_env.emit("metrics", phase=phase,
                    concurrency=EFFECTIVE_BATCH_SIZE,
                    values=ans, exp_folder=log_dir)

def modify_aisbench_api(concurrency, output_len, request_rate_param=None):
    if request_rate_param is None:
        request_rate_param = request_rate
    global EFFECTIVE_BATCH_SIZE
    EFFECTIVE_BATCH_SIZE = concurrency
    # 【本地改动】模板路径走 TOOL_DIR：上游写死 "default_api.py"（CWD 相对），
    # 而 Runner 把 CWD 设成产物目录（aisbench.log / temp_api.py 才落得到收集目录）。
    file_default = open(os.path.join(prefix_env.TOOL_DIR, "default_api.py"), 'r+')
    file_temp = open("temp_api.py", 'w+')
    logging.info("Api config file:")
    for ss in file_default.readlines():
        # 【本地改动】re.sub → str.replace：替换值里出现 `\1`/`\g<0>` 这类序列时
        # re.sub 会当成反向引用（模型路径、URL 都是用户给的字符串，不该被再解析）。
        # 另外不再替换 api_key：模板里已经改成运行时读 ENDPOINT_API_KEY，明文不落盘。
        tt = ss.replace("model_path_for_replace", MODEL_PATH)
        tt = tt.replace("model_name_for_replace", MODEL_NAME)
        tt = tt.replace("rr_for_replace", request_rate_param)
        tt = tt.replace("test_type_for_replace", api_test_type)
        tt = tt.replace("test_abbr_for_replace", api_test_abbr)
        tt = tt.replace("ip_for_replace", HOST_IP)
        tt = tt.replace("port_for_replace", HOST_PORT)
        tt = tt.replace("url_for_replace", URL)
        tt = tt.replace("outputlen_for_replace", output_len)
        tt = tt.replace("concurrency_for_replace", concurrency)
        if test_accuracy:
            generation_kwargs = "temperature=0.6,\n\t\t\ttop_p = 0.95"
        else:
            generation_kwargs = "temperature=0,\n\t\t\tignore_eos=True"
        if enable_think:
            generation_kwargs = generation_kwargs + ",\n\t\t\tchat_template_kwargs={\"enable_thinking\": True}"
        tt = tt.replace("generation_kwargs_for_replace", generation_kwargs.expandtabs(4))
        print(tt, end='')
        file_temp.write(tt)
    file_default.close()
    file_temp.close()
    # 模型配置写到 WORK_PATH（影子源码根）下，ais_bench 用 --config-dir 找它。
    cfg_dir = os.path.join(WORK_PATH, "ais_bench/benchmark/configs/models/vllm_api")
    os.makedirs(cfg_dir, exist_ok=True)
    symlink_force(
        os.path.join(os.getcwd(), "temp_api.py"),
        os.path.join(cfg_dir, "vllm_api_chat_temp.py")
    )

def _fetch_pod_metrics(pod):
    if pod.startswith('['):
        # IPv6 格式: [ip]:port
        ip = pod.split(']:')[0][1:]
        port = pod.split(']:')[1]
    elif pod.count(':') > 1:
        # IPv6 格式无方括号: ip:port
        ip, port = pod.rsplit(':', 1)
    else:
        # IPv4 格式: ip:port
        ip, port = pod.split(":")
    query_token, query_external = get_prefix_queries_total(ip, port)
    hit_token, hit_external = get_prefix_hits_total(ip, port)
    return pod, query_token, query_external, hit_token, hit_external

def get_pod_metrics_info(pod_info):
    query_tokens, query_tokens_external, hit_tokens, hit_tokens_external = {}, {}, {}, {}
    with ThreadPoolExecutor(max_workers=len(pod_info)) as executor:
        futures = [executor.submit(_fetch_pod_metrics, pod) for pod in pod_info]
        for future in as_completed(futures):
            pod, q_n, q_e, h_n, h_e = future.result()
            query_tokens[pod] = q_n
            query_tokens_external[pod] = q_e
            hit_tokens[pod] = h_n
            hit_tokens_external[pod] = h_e
    return query_tokens, query_tokens_external, hit_tokens, hit_tokens_external

def cal_prefix_hit_info(query_tokens, query_tokens_external, hit_tokens, hit_tokens_external, query_tokens_new,
                        query_tokens_external_new, hit_tokens_new, hit_tokens_external_new, phase="full"):
    # 【本地改动】加 phase 参数并把结果同时发一份 JSON 到 sidecar：
    # 打屏的那张表是给人看的（对齐宽度、`0.00%` 字符串），Runner 的 result.json
    # 不该靠正则解析它。原始增量（hits/queries）与 skipped_pods 都在这里emit，
    # 命中率与「没有指标」的原因由 summarize.py 统一判口径。
    if not query_tokens:
        logging.warning("No prefix cache metrics found.")
        prefix_env.emit("hit", phase=phase, concurrency=EFFECTIVE_BATCH_SIZE,
                        rows=[], skipped_pods=[], reason="目标服务没有暴露 prefix_cache 计数器（/metrics 抓取失败或无该指标）")
        return

    # 输出格式
    line_width = 118
    scope_width, hbm_rate_width, hbm_detail_width, external_rate_width = 35, 19, 21, 21
    headers = ["scope", "hbm_hit_rate", "hbm(hit/query)", "external_hit_rate", "external(hit/query)"]
    header = (f"{headers[0]:<{scope_width}}{headers[1]:<{hbm_rate_width}}"
              f"{headers[2]:<{hbm_detail_width}}{headers[3]:<{external_rate_width}}{headers[4]}")

    def format_rate(hits, queries):
        return "0.00%" if queries == 0 else format(hits / queries, ".2%")

    def build_row(scope, hbm_hits, hbm_queries, external_hits, external_queries):
        return (f"{scope:<{scope_width}}{format_rate(hbm_hits, hbm_queries):<{hbm_rate_width}}"
                f"{f'{hbm_hits}/{hbm_queries}':<{hbm_detail_width}}"
                f"{format_rate(external_hits, external_queries):<{external_rate_width}}"
                f"{external_hits}/{external_queries}")

    total_hbm_queries = total_hbm_hits = total_external_queries = total_external_hits = 0
    pod_sections, skipped_pods, metric_rows = [], [], []

    for pod, engines in sorted(query_tokens.items()):
        # 八类指标必须全部存在，且 engine_id 完全一致；
        # 否则无法可靠计算"测试后累计值 - 测试前累计值"，跳过该 POD
        metrics = [query_tokens.get(pod), query_tokens_external.get(pod), hit_tokens.get(pod),
                   hit_tokens_external.get(pod), query_tokens_new.get(pod), query_tokens_external_new.get(pod),
                   hit_tokens_new.get(pod), hit_tokens_external_new.get(pod)]

        if not all(metrics):
            skipped_pods.append(pod)
            continue

        engine_ids = set(engines)
        if any(set(metric) != engine_ids for metric in metrics[1:]):
            skipped_pods.append(pod)
            continue

        # 按 engine_id 升序遍历，为每个 engine 单独生成一行
        engine_rows = []
        for engine_id in sorted(engine_ids):
            engine_scope = f"{pod}/engine{engine_id}"
            engine_hbm_queries = query_tokens_new[pod][engine_id] - query_tokens[pod][engine_id]
            engine_hbm_hits = hit_tokens_new[pod][engine_id] - hit_tokens[pod][engine_id]
            engine_external_queries = query_tokens_external_new[pod][engine_id] \
                - query_tokens_external[pod][engine_id]
            engine_external_hits = hit_tokens_external_new[pod][engine_id] \
                - hit_tokens_external[pod][engine_id]
            engine_rows.append(build_row(engine_scope, engine_hbm_hits, engine_hbm_queries,
                                         engine_external_hits, engine_external_queries))
            metric_rows.append({
                "scope": engine_scope,
                "hbm_hits": engine_hbm_hits, "hbm_queries": engine_hbm_queries,
                "external_hits": engine_external_hits, "external_queries": engine_external_queries,
            })

        # 合并当前 POD 下所有 engine_id 的指标增量，用于 ALL_PODS 汇总
        pod_hbm_queries = sum(query_tokens_new[pod][engine_id] - query_tokens[pod][engine_id]
                              for engine_id in engine_ids)
        pod_hbm_hits = sum(hit_tokens_new[pod][engine_id] - hit_tokens[pod][engine_id]
                           for engine_id in engine_ids)
        pod_external_queries = sum(query_tokens_external_new[pod][engine_id]
                                   - query_tokens_external[pod][engine_id] for engine_id in engine_ids)
        pod_external_hits = sum(hit_tokens_external_new[pod][engine_id]
                                - hit_tokens_external[pod][engine_id] for engine_id in engine_ids)

        # 每个 POD 按 engine 升序输出，POD 之间用分隔线隔开
        pod_sections.append(engine_rows)
        total_hbm_queries += pod_hbm_queries
        total_hbm_hits += pod_hbm_hits
        total_external_queries += pod_external_queries
        total_external_hits += pod_external_hits

    if not pod_sections:
        logging.warning(f"No valid prefix cache metrics found. Skipped PODs: {', '.join(skipped_pods)}")
        prefix_env.emit("hit", phase=phase, concurrency=EFFECTIVE_BATCH_SIZE,
                        rows=[], skipped_pods=list(skipped_pods),
                        reason="测试前后的计数器快照不完整或 engine_id 对不上，无法差分")
        return

    all_pods_row = build_row("ALL_PODS", total_hbm_hits, total_hbm_queries,
                             total_external_hits, total_external_queries)
    skipped_pods_line = f"Skipped PODs: {', '.join(skipped_pods)}" if skipped_pods else None

    # 【本地改动】给 Runner 的结构化副本（phase + 本阶段实际 batch_size 一起给）。
    metric_rows.append({
        "scope": "ALL_PODS",
        "hbm_hits": total_hbm_hits, "hbm_queries": total_hbm_queries,
        "external_hits": total_external_hits, "external_queries": total_external_queries,
    })
    prefix_env.emit("hit", phase=phase, concurrency=EFFECTIVE_BATCH_SIZE,
                    rows=metric_rows, skipped_pods=list(skipped_pods), reason="")

    # 控制台只输出所有有效 POD 的汇总
    console_lines = ["=" * line_width, "Prefix cache hit summary", "=" * line_width, header,
                     "-" * line_width, all_pods_row, "=" * line_width]

    # 日志输出每个 POD 下所有 engine 的详细信息，POD 之间用分隔线隔开
    log_lines = ["=" * line_width, "Prefix cache hit detail", "=" * line_width, header,
                 "-" * line_width]
    for idx, section in enumerate(pod_sections):
        if idx > 0:
            log_lines.append("-" * line_width)
        log_lines.extend(section)
    log_lines.extend(["-" * line_width, all_pods_row, "=" * line_width])

    if skipped_pods_line:
        console_lines.append(skipped_pods_line)
        log_lines.append(skipped_pods_line)

    print("\n".join(console_lines))

    try:
        with open("aisbench.log", "a", encoding="utf-8") as log_file:
            log_file.write("\n" + "\n".join(log_lines) + "\n")
        with open("aisbench_all.log", "a", encoding="utf-8") as all_log_file:
            all_log_file.write("\n" + "\n".join(log_lines) + "\n")
    except OSError as error:
        logging.error(f"Failed to write prefix cache information to log files: {error}")

def setup_logging():
    """配置 Python logging：同时输出到控制台、aisbench.log 和 aisbench_all.log"""
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    # 清除已有 handlers（避免重复），但保留会输出到控制台的 StreamHandler
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    # 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 写入 aisbench.log（本次运行日志）
    file_handler = logging.FileHandler("aisbench.log", mode="a", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # 写入 aisbench_all.log（所有运行累积日志）
    all_file_handler = logging.FileHandler("aisbench_all.log", mode="a", encoding="utf-8")
    all_file_handler.setFormatter(formatter)
    logger.addHandler(all_file_handler)

if __name__ == '__main__':
    args = parse_arguments()
    input_len = args.input_len
    output_len = args.output_len
    data_num = args.data_num
    concurrency = args.concurrency
    request_rate = args.request_rate
    test_type = args.test_type
    dataset_path_input = args.dataset
    test_times = args.repeat
    enable_think = args.enable_think
    test_accuracy = args.test_accuracy
    npu_num = args.npu_num
    prefix_num = args.prefix_num
    repeat_rate = parse_prefix_ratio(args.repeat_rate)
    prefix_test = args.prefix_test
    dataset_type = args.dataset_type
    seed = args.seed
    dp = args.dp
    length_mean = args.length_mean
    length_std = args.length_std
    length_min = args.length_min
    length_max = args.length_max

    # 初始化日志文件：清空 aisbench.log（新运行覆盖），aisbench_all.log 添加分隔符（续写）
    with open("aisbench.log", "w", encoding="utf-8") as f:
        f.write("")
    with open("aisbench_all.log", "a", encoding="utf-8") as f:
        f.write(f"\n\n{'='*50}\n")
        f.write(f"New run started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"{'='*50}\n\n")

    # 配置日志输出到控制台 + aisbench.log + aisbench_all.log
    setup_logging()
    logging.info("aisbench.log has been cleared for this run.")
    logging.info("aisbench_all.log separator added for this run.")

    # 变长参数校验
    if (length_mean is None) ^ (length_std is None):
        raise ValueError("length_mean 和 length_std 必须同时提供或同时不提供")
    if (length_min is None) ^ (length_max is None):
        raise ValueError("length_min 和 length_max 必须同时提供或同时不提供")
    if length_mean is not None and length_mean < 1:
        raise ValueError("length_mean 必须 >= 1")
    if length_std is not None and length_std < 0:
        raise ValueError("length_std 必须 >= 0")
    if length_min is not None and length_min < 1:
        raise ValueError("length_min 必须 >= 1")
    if length_max is not None and length_max < 1:
        raise ValueError("length_max 必须 >= 1")

    # 【本地改动】picked_ids.txt 的清理与 RNG 播种都在数据集生成入口做
    # （见 generate_dataset.create_multi_prefix_dataset），这里不再重复；
    # 指定 --dataset 时压根不生成数据集，也就不需要清理。
    logging.info(f"input token length: {input_len}")
    logging.info(f"output token length: {output_len}")
    logging.info(f"number of dataset: {data_num}")
    logging.info(f"concurrency: {concurrency}")
    logging.info(f"request rate: {request_rate}")
    logging.info(f"test type: {test_type}")
    logging.info(f"test_times: {test_times}")
    logging.info(f"v3.1 enable_think: {enable_think}")
    logging.info(f"accuracy test: {test_accuracy}")
    logging.info(f"npu numbers: {npu_num}")
    logging.info(f"prefix numbers: {prefix_num}")
    logging.info(f"dataset repeat rate: {repeat_rate}")
    logging.info(f"test prefix dataset: {prefix_test}")
    logging.info(f"dataset type: {dataset_type}")
    logging.info(f"seed: {seed}")
    logging.info(f"dp size: {dp}")
    logging.info(f"length_mean: {length_mean}")
    logging.info(f"length_std: {length_std}")
    logging.info(f"length_min: {length_min}")
    logging.info(f"length_max: {length_max}")

    # 区分流式和非流式
    if test_type == "text":
        api_test_type = "VLLMCustomAPIChat"
        api_test_abbr = "vllm-api-general-chat"
    elif test_type == "stream":
        api_test_type = "VLLMCustomAPIChatStream"
        api_test_abbr = "vllm-api-stream-chat"
    else:
        api_test_type = "VLLMCustomAPIChatStream"
        api_test_abbr = "vllm-api-stream-chat"

    # 【本地改动】把本次真正生效的口径写进 sidecar，供 result.json 的 workload 使用。
    # 注意写的是归一化后的值（repeat_rate 已被 parse 成 float、寻址已被 config.py 决定），
    # 不是用户原始输入——排查时要看的是「实际跑了什么」。
    prefix_env.emit("config", input_len=input_len, output_len=output_len,
                    data_num=data_num, request_rate=request_rate,
                    target=prefix_env.resolve_target().describe(),
                    model_name=MODEL_NAME, model_path=MODEL_PATH,
                    dataset_type=dataset_type, prefix_num=prefix_num,
                    repeat_rate=repeat_rate, prefix_test=prefix_test, seed=seed, dp=dp,
                    dataset=dataset_path_input, dataset_path=DATASET_PATH,
                    work_path=WORK_PATH, output_dir=OUTPUT_DIR,
                    summarizer=DEFAULT_PERFORMANCE_TEST, test_type=test_type,
                    test_accuracy=test_accuracy, enable_think=enable_think,
                    npu_num=npu_num, repeat=test_times,
                    length_mean=length_mean, length_std=length_std,
                    length_min=length_min, length_max=length_max,
                    gsm8k_pool=prefix_env.gsm8k_pool(),
                    api_key_from_env=bool(os.environ.get("ENDPOINT_API_KEY")))

    if dataset_path_input == "none":
        src_file_prefix,src_file_data = create_gsm8k_dataset(dataset_type, input_len, data_num, MODEL_PATH, DATASET_PATH, prefix_num, repeat_rate, seed,
                                                              length_mean, length_std, length_min, length_max)
    else:
        # 指定数据集路径逻辑
        if not os.path.exists(dataset_path_input):
            logging.error(f"Dataset {dataset_path_input} is not exist.")
            sys.exit(prefix_env.EXIT_USAGE)
        src_file_data = dataset_path_input
        src_file_prefix = ""

    dst_dir = os.path.join(WORK_PATH, "ais_bench/datasets/gsm8k")

    # 判断 aisbench 的 gsm8k 文件夹是否存在在
    if not os.path.exists(dst_dir):
        logging.info("dataset work path not exist. creating.")
        os.makedirs(dst_dir)
        logging.info("dataset work path created.")
    # 判断 aisbench 的 gsm8k 文件夹是否存在在 train.jsonl 文件
    train_dataset = os.path.join(dst_dir, "train.jsonl")
    if not os.path.exists(train_dataset):
        logging.info("train dataset not exist. creating.")
        file = open(train_dataset, 'w')
        file.close()
        logging.info("train dataset created.")

    # 生成 aisbench 命令
    ais_bench_cmd = generate_aisbench_command(DEFAULT_PERFORMANCE_TEST)
    logging.info(f"test start, use command: {ais_bench_cmd}")

    # 【本地改动】命中率快照的节点默认值：上游用 HOST_IP+":"+HOST_PORT，
    # 但 HOST_IP 在「用 url 字段寻址」时是被刻意写成 localhost 的（规避 ais_bench 的
    # host_ip 必须是 IP 的校验），拿它去抓 /metrics 会永远抓不到。改用真实 host:port。
    if not POD_INFO:
        pod_info = [POD_INFO_DEFAULT]
    else:
        pod_info = list(POD_INFO)
    logging.info(f"pod_info: {pod_info}")

    # 执行命令
    if dataset_type == "prefix_cache":
        # 前缀数据集测试
        if prefix_test:
            logging.info(f"[开始] 前缀数据集测试")
            # 前缀测试: request rate 固定为 0
            warmup_concurrency = prefix_num if dp == 1 else dp
            modify_aisbench_api(str(warmup_concurrency), "1", request_rate_param="0")
            dst_file = generate_test_dataset(src_file_prefix, dst_dir)

            # 命中率计算
            query_tokens, query_tokens_external, hit_tokens, hit_tokens_external = get_pod_metrics_info(pod_info)

            run_aisbench(ais_bench_cmd)
            logging.info(f"[完成] 前缀数据集测试完成，结果保存在aisbench_result.csv")

            query_tokens_new, query_tokens_external_new, hit_tokens_new, hit_tokens_external_new = get_pod_metrics_info(pod_info)
            cal_prefix_hit_info(query_tokens, query_tokens_external, hit_tokens, hit_tokens_external,query_tokens_new,
                                query_tokens_external_new, hit_tokens_new, hit_tokens_external_new,
                                phase="prefix")

            # 保存前缀测试结果
            save_result(0, npu_num, repeat_rate, phase="prefix")
            logging.info(f"[开始] 全量数据集测试")
            # 命中率计算
            query_tokens, query_tokens_external, hit_tokens, hit_tokens_external = get_pod_metrics_info(pod_info)

            modify_aisbench_api(concurrency,str(output_len))
            dst_file = generate_test_dataset(src_file_data, dst_dir)
            # 执行测试命令
            run_aisbench(ais_bench_cmd)
            logging.info(f"[完成] 全量数据集测试完成，结果保存在aisbench_result.csv")

            query_tokens_new, query_tokens_external_new, hit_tokens_new, hit_tokens_external_new = get_pod_metrics_info(pod_info)
            cal_prefix_hit_info(query_tokens, query_tokens_external, hit_tokens, hit_tokens_external, query_tokens_new,
                                query_tokens_external_new, hit_tokens_new, hit_tokens_external_new,
                                phase="full")

        else:
            logging.info(f"[开始] 全量数据集测试")
            modify_aisbench_api(concurrency,str(output_len))
            dst_file = generate_test_dataset(src_file_data, dst_dir)
            run_aisbench(ais_bench_cmd)
            logging.info(f"[完成] 全量数据集测试完成，结果保存在aisbench_result.csv")

    else:
        logging.info(f"[开始] 全量数据集测试")
        modify_aisbench_api(concurrency,str(output_len))
        dst_file = generate_test_dataset(src_file_data, dst_dir)
        if test_times > 1:
            for test_time in range(test_times):
                logging.info(f"Execution rounds: {test_time + 1}")
                run_aisbench(ais_bench_cmd)
        else:
            run_aisbench(ais_bench_cmd)
        logging.info(f"[完成] 全量数据集测试完成，结果保存在aisbench_result.csv")


    # 保存结果
    save_result(request_rate, npu_num, repeat_rate, phase="full")

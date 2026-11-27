import os
from ais_bench.benchmark.models import test_type_for_replace

# 【本地改动】凭据只走环境变量（§12.4）：模板里不再有 `api_key_for_replace`，
# 而是写一句「运行时从 ENDPOINT_API_KEY 读」的表达式。这样
# <work_path>/…/vllm_api_chat_temp.py 与 <point_dir>/temp_api.py 落盘时都不含明文 key，
# 容器里 `ps` 也看不到（run.sh 从不把它拼进 argv），日志里只有 `api_key=_API_KEY`。
_API_KEY = os.environ.get("ENDPOINT_API_KEY", "")

models = [
    dict(
        attr="service",
        type=test_type_for_replace,
        abbr='test_abbr_for_replace',
        path="model_path_for_replace",
        model="model_name_for_replace",
        api_key=_API_KEY,
        request_rate=rr_for_replace,
        retry=2,
        host_ip="ip_for_replace",
        host_port=port_for_replace,
        url="url_for_replace",
        max_out_len=outputlen_for_replace,
        batch_size=concurrency_for_replace,
        generation_kwargs=dict(
            temperature=0,
            ignore_eos=True,
        )
    )
]

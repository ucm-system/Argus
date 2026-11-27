#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据集生成的可复现性与 picked_ids 行为（离线，用 tests/fakepkgs 的逐字符假 tokenizer）。

这三条就是本轮反馈里问到的两件事：

* 「原先不支持设置 seed，无法复现结果」——上游 `--seed` 只播给「前缀后插入的 3 个随机
  token」（`generate_unique_tokens()` 里才 `random.seed()`），而 GSM8K 挑题走
  `DataPicker.pick_one()` 里未播种的全局 `random.choice/randint`。
  本测试锁住修复后的语义：**同参数 + 同 seed ⇒ 数据集逐字节相同**。
* 「生成数据集失败，请清空 picked ids，删文件能解决吗？」——能，而且本 Runner 自动做。
  本测试同时锁住两面：清理后同一份记录被标满的题库**又能用了**；
  以及记录累积到超出题库容量时**必须非零退出**（上游是 exit(0)，会把失败伪装成成功）。

跑法：`py -3.11 runners/aisbench-prefix/tests/test_dataset_determinism.py`
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
# 假 transformers 必须先于真包被找到（本机装了 transformers 也要走假的）
sys.path.insert(0, os.path.join(HERE, "fakepkgs"))
sys.path.insert(0, os.path.join(HERE, "..", "tool"))

import generate_dataset  # noqa: E402
import prefix_env  # noqa: E402

POOL_ROWS = 12


def make_pool(path, rows=POOL_ROWS, size=90):
    with open(path, "w", encoding="utf-8") as handle:
        for index in range(rows):
            text = "".join(chr(32 + ((index * 7 + k * 13) % 95)) for k in range(size))
            handle.write(json.dumps({"question": "q%d-%s" % (index, text),
                                     "answer": "#### 1"}, ensure_ascii=False) + "\n")


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prefix-det-")
        self.pool = os.path.join(self.tmp, "GSM8K.jsonl")
        make_pool(self.pool)
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        os.environ["AGP_GSM8K_FILE"] = self.pool
        os.environ["AGP_STATE_DIR"] = self.state
        os.environ["AGP_SEED_GLOBAL"] = "1"
        os.environ["AGP_RESET_PICK_STATE"] = "1"

    def tearDown(self):
        for key in ("AGP_GSM8K_FILE", "AGP_STATE_DIR", "AGP_SEED_GLOBAL",
                    "AGP_RESET_PICK_STATE"):
            os.environ.pop(key, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ 工具
    def generate(self, out_dir, seed, input_len=128, number=6, prefix_flag=1,
                 dp=1, repeat_rate=0.5, prefix_num=2, **length):
        """按上游签名调 create_multi_prefix_dataset，返回 (前缀文件, 数据集文件)。"""
        os.makedirs(out_dir, exist_ok=True)
        return generate_dataset.create_multi_prefix_dataset(
            os.path.join(self.tmp, "model_dir"),   # tokenizer_path（假 tokenizer 不看内容）
            input_len, number, out_dir, prefix_flag, dp, repeat_rate, seed, prefix_num,
            length.get("length_mean"), length.get("length_std"),
            length.get("length_min"), length.get("length_max"))

    def digest(self, path):
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()

    def full_dataset_files(self, out_dir):
        return sorted(name for name in os.listdir(out_dir)
                      if name.startswith("GSM8K-") and name.endswith(".jsonl"))

    # ----------------------------------------------------------------- 用例
    def test_same_seed_is_reproducible(self):
        """同参数同 seed ⇒ 数据集逐字节相同（含前缀文件与完整数据集）。"""
        a = os.path.join(self.tmp, "a")
        b = os.path.join(self.tmp, "b")
        prefix_a, data_a = self.generate(a, seed=7)
        prefix_b, data_b = self.generate(b, seed=7)
        self.assertEqual(os.path.basename(data_a), os.path.basename(data_b))
        self.assertEqual(self.digest(data_a), self.digest(data_b),
                         "同一 seed 两次生成的数据集不同 —— seed 没吃到挑题路径？")
        self.assertEqual(self.digest(prefix_a), self.digest(prefix_b))

    def test_different_seed_changes_dataset(self):
        a = os.path.join(self.tmp, "s1")
        b = os.path.join(self.tmp, "s2")
        _pa, data_a = self.generate(a, seed=7)
        _pb, data_b = self.generate(b, seed=8)
        self.assertNotEqual(self.digest(data_a), self.digest(data_b),
                            "换 seed 数据集没变 —— 挑题与随机 token 都没吃到 seed")

    def test_variable_length_also_reproducible(self):
        a = os.path.join(self.tmp, "va")
        b = os.path.join(self.tmp, "vb")
        _pa, data_a = self.generate(a, seed=3, prefix_flag=0, number=5,
                                    length_mean=128, length_std=16,
                                    length_min=64, length_max=200)
        _pb, data_b = self.generate(b, seed=3, prefix_flag=0, number=5,
                                    length_mean=128, length_std=16,
                                    length_min=64, length_max=200)
        self.assertEqual(self.digest(data_a), self.digest(data_b))

    def test_pick_state_accumulation_breaks_generation_and_reset_fixes_it(self):
        """picked_ids 累积到题库被标满 → 必须报 3 号退出码；清理后恢复可用。"""
        first = os.path.join(self.tmp, "acc1")
        self.generate(first, seed=11, prefix_num=POOL_ROWS)   # 一次就把题库标满
        self.assertTrue(os.path.exists(prefix_env.picked_ids_path()))
        second = os.path.join(self.tmp, "acc2")
        with mock.patch.dict(os.environ, {"AGP_RESET_PICK_STATE": "0"}):
            with self.assertRaises(SystemExit) as caught:
                self.generate(second, seed=11, prefix_num=POOL_ROWS)
            self.assertEqual(caught.exception.code, prefix_env.EXIT_DATASET,
                             "上游在这里是 exit(0)：失败必须是非零，否则 Task 会被判成功")
        # 打开清理（Runner 的默认）→ 同一份参数又能跑出数据集
        third = os.path.join(self.tmp, "acc3")
        _prefix, data = self.generate(third, seed=11, prefix_num=POOL_ROWS)
        self.assertTrue(os.path.isfile(data))

    def test_dataset_failure_when_pool_too_small(self):
        """prefix_num 超过题库行数：报错要说清楚容量，而不是只喊「清空 picked ids」。"""
        out = os.path.join(self.tmp, "over")
        with self.assertRaises(SystemExit) as caught:
            self.generate(out, seed=1, prefix_num=POOL_ROWS + 5)
        self.assertEqual(caught.exception.code, prefix_env.EXIT_DATASET)

    def test_tokenizer_import_is_lazy(self):
        """`--dataset` 路径不该把 transformers 拖进来（惰性 import 的意义）。"""
        import subprocess
        code = ("import sys; sys.path.insert(0, %r); sys.path.insert(0, %r);"
                "import generate_dataset;"
                "print('transformers' in sys.modules)" % (
                    os.path.join(HERE, "fakepkgs"), os.path.join(HERE, "..", "tool")))
        result = subprocess.run([sys.executable, "-c", code],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False",
                         "import generate_dataset 就拖起 transformers 了")


if __name__ == "__main__":
    unittest.main(verbosity=2)

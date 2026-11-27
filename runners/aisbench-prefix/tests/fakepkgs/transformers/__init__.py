"""测试用的假 `transformers`（**只在离线冒烟里用**，不在发布镜像里）。

为什么要它：vendored 工具生成数据集时要 `AutoTokenizer.from_pretrained(model_path)`，
真镜像里那是几百 MB 的 transformers + 一个真实权重目录；离线冒烟既不想装 torch，
也没有 `/models/<x>`。于是这里给一个**逐字符**的假 tokenizer：

    encode(text) -> [ord(c) for c in text if 32 <= ord(c) <= 126]
    decode(ids)  -> "".join(chr(i) for i in ids)

1 token = 1 可打印 ASCII 字符，`decode∘encode` 往返**精确**，正好满足
`generate_dataset.create_dataset()` 里那句 `final_len != input_len` 的修正逻辑，
于是「定长/变长前缀数据集生成、picked_ids 去重、seed 可复现、前缀预热、命中率差分」
整条链路都能在几秒内离线跑通。

它**不**代表真实性能或真实数据集形态（分词粒度完全不同）；只证明链路与产物形状。
用法：`PYTHONPATH=/runner/tests/fakepkgs`（tests/Dockerfile.stub 里设好）。
"""


class _CharTokenizer:
    """逐字符 tokenizer（词表 = ASCII 32..126，共 95 个 token）。"""

    _LO = 32
    _HI = 126

    def __init__(self, name_or_path="fake-char-tokenizer"):
        self.name_or_path = name_or_path
        self.pad_token_id = 0
        self.all_special_ids = []

    def __len__(self):
        return self._HI - self._LO + 1

    def get_vocab(self):
        return {chr(code): code for code in range(self._LO, self._HI + 1)}

    def encode(self, text, add_special_tokens=False, **_kwargs):
        return [ord(ch) for ch in (text or "") if self._LO <= ord(ch) <= self._HI]

    def decode(self, token_ids, skip_special_tokens=True, **_kwargs):
        out = []
        for token_id in token_ids or []:
            code = int(token_id)
            out.append(chr(code) if self._LO <= code <= self._HI else " ")
        return "".join(out)


class AutoTokenizer:
    """只实现 `from_pretrained`：假 tokenizer 与路径无关，但保留参数以贴合真调用。"""

    @classmethod
    def from_pretrained(cls, tokenizer_path, *args, **kwargs):
        return _CharTokenizer(str(tokenizer_path))

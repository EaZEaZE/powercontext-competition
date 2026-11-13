"""本地确定性 embedding：字符 n-gram 哈希向量。

说明：比赛环境不允许依赖外部模型下载，这里用哈希词袋（hashed n-grams）构造向量，
对中文按字符二元组切分、对英文按词切分，做 sublinear TF 后 L2 归一化。
它是真实的向量表示（余弦距离有检索意义），只是不含语义泛化能力。
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Iterable, List

DIM = 512

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[a-zA-Z0-9_]+")


def _tokens(text: str) -> Iterable[str]:
    lowered = text.lower()
    for w in _WORD.findall(lowered):
        if len(w) > 1 or w.isdigit():
            yield "w:" + w
    for run in _CJK.findall(lowered):
        for ch in run:
            yield "c:" + ch
        for i in range(len(run) - 1):
            yield "b:" + run[i:i + 2]


def embed_text(text: str, dim: int = DIM) -> List[float]:
    counts = Counter(_tokens(text))
    vec = [0.0] * dim
    for tok, cnt in counts.items():
        digest = hashlib.md5(tok.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "little") % dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vec[idx] += sign * (1.0 + math.log(cnt))
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]

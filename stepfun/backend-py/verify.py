#!/usr/bin/env python3
"""端到端验证脚本：模拟浏览器端计算 embedding 后调用后端接口。

用法： python3 verify.py [http://127.0.0.1:3000]
"""
import hashlib
import json
import math
import re
import sys
import time
import urllib.request
from collections import Counter

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3000"
DIM = 384
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[a-zA-Z0-9_]+")


def embed(text):
    toks = Counter()
    low = text.lower()
    for w in _WORD.findall(low):
        toks["w:" + w] += 1
    for run in _CJK.findall(low):
        for ch in run:
            toks["c:" + ch] += 1
        for i in range(len(run) - 1):
            toks["b:" + run[i:i + 2]] += 1
    vec = [0.0] * DIM
    for tok, cnt in toks.items():
        d = hashlib.md5(tok.encode()).digest()
        idx = int.from_bytes(d[:4], "little") % DIM
        vec[idx] += (1.0 if d[4] & 1 else -1.0) * (1.0 + math.log(cnt))
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode())


DOCS = [
    "OceanBase seekdb 支持 HNSW 向量索引，距离度量可选 cosine 或 l2。",
    "Transformers.js 可以在浏览器里运行 all-MiniLM-L6-v2，无需上传数据到服务器。",
    "INT8 量化把模型体积从 90MB 压到 23MB，检索质量偏差不超过 0.01。",
    "WebGPU 不可用时自动降级到 WASM 后端，保证端侧推理可用。",
]

CASES = [
    ("HNSW 向量索引支持哪些距离度量", "OceanBase seekdb 支持"),
    ("量化后模型多大", "INT8 量化"),
    ("浏览器里跑模型用什么库", "Transformers.js"),
]

def main():
    print("== 1. 服务状态 ==")
    print(json.dumps(get("/api/stats"), ensure_ascii=False))

    print("\n== 2. 导入 4 条文档（embedding 在本地计算）==")
    run = str(int(time.time()))
    ids = ["run-%s-doc-%03d" % (run, i) for i in range(len(DOCS))]
    print(json.dumps(post("/api/add", {
        "ids": ids,
        "documents": DOCS,
        "metadatas": [{"content": d} for d in DOCS],
        "embeddings": [embed(d) for d in DOCS],
    }), ensure_ascii=False))

    print("\n== 3. 统计 ==")
    print(json.dumps(get("/api/stats"), ensure_ascii=False))

    print("\n== 4. 检索验证 ==")
    ok = 0
    for q, expect in CASES:
        r = post("/api/search", {"queryEmbedding": embed(q), "queryText": q, "nResults": 3})
        hits = r.get("results", [])
        top = hits[0]["document"] if hits else ""
        passed = expect in top
        ok += 1 if passed else 0
        print("[%s] %-28s -> 命中=%.3fms top=%s" % (
            "PASS" if passed else "FAIL", q, r.get("latencyMs", 0), top[:38]))

    print("\n结果：%d/%d 通过" % (ok, len(CASES)))
    return 0 if ok == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())

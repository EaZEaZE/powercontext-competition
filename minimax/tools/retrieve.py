"""从 seekdb 检索工程资料，并为本次改码构造上下文。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List

import pyseekdb

from embed import embed_text

COLLECTION = "eng_docs"


def search(query: str, k: int = 6, db: str | None = None) -> List[Dict]:
    client = pyseekdb.Client(path=db or os.environ.get("SEEKDB_PATH", "/root/seekdb-data"))
    col = client.get_collection(COLLECTION)
    res = col.query(query_embeddings=[embed_text(query)], n_results=k)
    hits: List[Dict] = []
    for i, doc_id in enumerate(res["ids"][0]):
        hits.append({
            "id": doc_id,
            "score": 1.0 - float(res["distances"][0][i]),
            "document": res["documents"][0][i],
            "metadata": res["metadatas"][0][i],
        })
    return hits


def build_context(query: str, k: int = 6) -> str:
    hits = search(query, k)
    parts = [
        "# 检索到的工程资料（来自 seekdb 向量检索）",
        "",
        "检索语句：%s" % query,
        "",
    ]
    for i, hit in enumerate(hits, 1):
        meta = hit["metadata"]
        parts.append("## [%d] %s › %s（相似度 %.3f）" % (i, meta["source"], meta["title"], hit["score"]))
        parts.append("")
        parts.append(hit["document"])
        parts.append("")
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("-k", type=int, default=6)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.json:
        print(json.dumps(search(args.query, args.k), ensure_ascii=False, indent=2))
    else:
        print(build_context(args.query, args.k))
    return 0


if __name__ == "__main__":
    sys.exit(main())

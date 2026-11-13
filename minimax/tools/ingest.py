"""把工程资料切块并写入 seekdb 向量库。"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, List

import pyseekdb

from embed import DIM, embed_text

MAX_CHARS = 700
COLLECTION = "eng_docs"


def _split_markdown(path: str, text: str) -> List[Dict[str, str]]:
    """按标题切块，超长再按段落切。"""
    chunks: List[Dict[str, str]] = []
    current_title = os.path.basename(path)
    buf: List[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            chunks.append({"title": current_title, "body": body})
        buf.clear()

    for line in text.splitlines():
        if re.match(r"^#{1,3}\s+", line):
            flush()
            current_title = line.lstrip("#").strip() or current_title
            buf.append(line)
        else:
            buf.append(line)
    flush()

    result: List[Dict[str, str]] = []
    for ch in chunks:
        body = ch["body"]
        if len(body) <= MAX_CHARS:
            result.append(ch)
            continue
        paras = [p for p in body.split("\n\n") if p.strip()]
        piece = ""
        for p in paras:
            if len(piece) + len(p) + 2 > MAX_CHARS and piece:
                result.append({"title": ch["title"], "body": piece})
                piece = p
            else:
                piece = (piece + "\n\n" + p).strip()
        if piece:
            result.append({"title": ch["title"], "body": piece})
    return result


def collect_files(root: str) -> List[str]:
    targets: List[str] = []
    for base, _dirs, files in os.walk(root):
        rel_base = os.path.relpath(base, root).replace("\\", "/")
        if ".git" in base or "__pycache__" in base or "/evidence" in base.replace("\\", "/"):
            continue
        # 脚手架自身代码不属于业务资料，排除以免自我污染检索结果
        if rel_base == "tools" or rel_base.startswith("tools/"):
            continue
        for name in files:
            if name.endswith((".md", ".py", ".toml")):
                targets.append(os.path.join(base, name))
    return sorted(targets)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="工程资料根目录（会递归收集 md/py/toml）")
    ap.add_argument("--db", default=os.environ.get("SEEKDB_PATH", "/root/seekdb-data"))
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    client = pyseekdb.Client(path=args.db)
    if args.reset and client.has_collection(COLLECTION):
        client.delete_collection(COLLECTION)
        print("[seekdb] dropped collection", COLLECTION)

    col = client.get_or_create_collection(
        name=COLLECTION,
        configuration=pyseekdb.HNSWConfiguration(dimension=DIM, distance="cosine"),
        embedding_function=None,
    )

    ids: List[str] = []
    docs: List[str] = []
    metas: List[Dict] = []
    vecs: List[List[float]] = []

    for path in collect_files(args.root):
        rel = os.path.relpath(path, args.root).replace("\\", "/")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        if not text.strip():
            continue
        for i, ch in enumerate(_split_markdown(rel, text)):
            doc = "# %s\n\n%s" % (ch["title"], ch["body"])
            ids.append("%s#%03d" % (rel, i))
            docs.append(doc)
            metas.append({"source": rel, "title": ch["title"], "chunk": i})
            vecs.append(embed_text(doc))
        print("[seekdb] indexed %-46s chunks<=%d" % (rel, MAX_CHARS))

    if ids:
        col.upsert(ids=ids, embeddings=vecs, documents=docs, metadatas=metas)
    col.refresh_index()
    total = col.count()
    print(json.dumps({"collection": COLLECTION, "chunks": len(ids), "total": total}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

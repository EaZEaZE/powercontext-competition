import os
import sys
import uuid

sys.path.insert(0, "/root/zhiyan/backend")
os.environ["SEEKDB_EMBEDDED"] = "1"
os.environ["EMBEDDING_BACKEND"] = "hash"
os.environ["SEEKDB_PATH"] = "/root/zhiyan/_probe-db"

import main  # noqa: E402

client = main.new_seekdb_client()

h1 = client.get_or_create_collection("probe_docs", **main.collection_kwargs())
h1.add(
    ids=[str(uuid.uuid4())],
    embeddings=[main.embed(["同一个进程里写入后立刻查询应该能看到这一段"])[0]],
    documents=["同一个进程里写入后立刻查询应该能看到这一段"],
    metadatas=[{"content": "同一进程写入后立刻查询"}],
)
print("h1.count:", h1.count())

# 另一个句柄（模拟 get_collection() 每次重建）
h2 = client.get_or_create_collection("probe_docs", **main.collection_kwargs())
print("h2.count:", h2.count())
print("h2.query:", h2.query(query_embeddings=[main.embed(["同一进程写入后立刻查询"])[0]], n_results=3))
print("h1.query:", h1.query(query_embeddings=[main.embed(["同一进程写入后立刻查询"])[0]], n_results=3))

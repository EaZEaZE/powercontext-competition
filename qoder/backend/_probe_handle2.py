import os
import sys
import uuid

sys.path.insert(0, "/root/zhiyan/backend")
os.environ["SEEKDB_EMBEDDED"] = "1"
os.environ["EMBEDDING_BACKEND"] = "hash"
os.environ["SEEKDB_PATH"] = "/root/zhiyan/_probe-db2"

import main  # noqa: E402

client = main.new_seekdb_client()
h1 = client.get_or_create_collection("probe2", **main.collection_kwargs())
h1.add(
    ids=[str(uuid.uuid4())],
    embeddings=[main.embed(["写入后立刻查询的一段话"])[0]],
    documents=["写入后立刻查询的一段话"],
    metadatas=[{"content": "写入后立刻查询的一段话"}],
)
q = main.embed(["写入后立刻查询"])[0]
print("A same-handle:", h1.query(query_embeddings=[q], n_results=3)["ids"])

print("client api:", [m for m in dir(client) if not m.startswith("_")])
print("coll api:", [m for m in dir(h1) if not m.startswith("_")])

# 新建客户端再查
client2 = main.new_seekdb_client()
h2 = client2.get_or_create_collection("probe2", **main.collection_kwargs())
print("B new-client:", h2.query(query_embeddings=[q], n_results=3)["ids"], "count:", h2.count())

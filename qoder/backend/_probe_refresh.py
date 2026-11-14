import os
import sys
import uuid

sys.path.insert(0, "/root/zhiyan/backend")
os.environ["SEEKDB_EMBEDDED"] = "1"
os.environ["EMBEDDING_BACKEND"] = "hash"
os.environ["SEEKDB_PATH"] = "/root/zhiyan/_probe-db3"

import main  # noqa: E402

client = main.new_seekdb_client()
h = client.get_or_create_collection("probe3", **main.collection_kwargs())
h.add(
    ids=[str(uuid.uuid4())],
    embeddings=[main.embed(["写入后刷新索引再查询应该能命中"])[0]],
    documents=["写入后刷新索引再查询应该能命中"],
    metadatas=[{"content": "写入后刷新索引再查询应该能命中"}],
)
q = main.embed(["写入后刷新索引再查询"])[0]
print("before refresh:", h.query(query_embeddings=[q], n_results=3)["ids"])
h.refresh_index()
print("after refresh :", h.query(query_embeddings=[q], n_results=3)["ids"])

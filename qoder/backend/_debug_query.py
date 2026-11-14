import os
import sys

sys.path.insert(0, "/root/zhiyan/backend")
os.environ["SEEKDB_EMBEDDED"] = "1"
os.environ["EMBEDDING_BACKEND"] = "hash"
os.environ["SEEKDB_PATH"] = "/root/zhiyan/seekdb-data"

import main  # noqa: E402

main.seekdb_client = main.new_seekdb_client()
coll = main.get_collection()
print("count:", coll.count())

vec = main.embed(["机器学习有哪些应用"])[0]
print("dim:", len(vec), "norm:", sum(x * x for x in vec) ** 0.5)

r = coll.query(query_embeddings=[vec], n_results=5, include=["documents", "metadatas", "distances"])
print("query(documents):", r)

r2 = coll.query(query_embeddings=[vec], n_results=5)
print("query(default):", r2)

peek = coll.get(limit=3, include=["documents", "metadatas"])
print("peek:", peek)

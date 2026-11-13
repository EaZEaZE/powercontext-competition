#!/usr/bin/env python3
"""StepFun 阶跃星辰后端 — 轻量版，用 seekdb 自带 DefaultEmbeddingFunction"""
import os, uuid
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel
import pyseekdb
from pyseekdb import DefaultEmbeddingFunction

app = FastAPI(title="阶跃星辰·语义检索问答")

_client = None
_coll_name = "stepfun_docs"
_embed_fn = None


def get_client():
    global _client
    if _client is None:
        _client = pyseekdb.Client(
            host="127.0.0.1", port=2881,
            tenant="sys", database="test",
            user="root", password="",
        )
    return _client


def get_embed():
    global _embed_fn
    if _embed_fn is None:
        print("Loading DefaultEmbeddingFunction (ONNX)...", flush=True)
        _embed_fn = DefaultEmbeddingFunction()
        print("Embedding ready!", flush=True)
    return _embed_fn


def coll():
    return get_client().get_or_create_collection(
        _coll_name, embedding_function=get_embed()
    )


FRONTEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend", "index.html")


@app.get("/")
def root():
    if os.path.isfile(FRONTEND):
        return FileResponse(FRONTEND)
    return {"ok": True, "service": "stepfun"}


@app.get("/api/stats")
def stats():
    return {"docs": coll().count()}


@app.post("/api/add")
def add(req: dict):
    texts = req.get("texts", [])
    ids = [str(uuid.uuid4()) for _ in texts]
    metas = [{"content": t} for t in texts]
    # 使用 seekdb 内置 embedding_function，不用手动传 embeddings
    coll().add(ids=ids, documents=texts, metadatas=metas)
    return {"ok": True, "count": len(texts)}


@app.post("/api/search")
def search(req: dict):
    q = req.get("query", "")
    top_k = req.get("top_k", 5)
    res = coll().query(query_texts=[q], n_results=top_k,
                       include=["documents", "metadatas", "distances"])
    ids = res.get("ids", [[]])[0]
    docs = res.get("documents", [[]])[0]
    dists = res.get("distances", [[]])[0]
    hits = [{"id": ids[i], "text": docs[i], "distance": round(dists[i], 4)}
            for i in range(len(ids))]
    return {"query": q, "hits": hits}


@app.delete("/api/clear")
def clear():
    c = get_client()
    c.delete_collection(_coll_name)
    c.get_or_create_collection(_coll_name, embedding_function=get_embed())
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3000)

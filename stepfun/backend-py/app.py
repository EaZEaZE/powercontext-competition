#!/usr/bin/env python3
"""阶跃星辰赛题后端（Python 版，pyseekdb 嵌入式，无需 seekdb 服务进程）

与 stepfun/backend/server.js 保持同一 HTTP 契约：
  POST /api/add     {ids, documents, metadatas, embeddings}
  POST /api/search  {queryEmbedding, queryText, nResults}
  GET  /api/stats
  DELETE /api/clear

embedding 由浏览器端 Transformers.js 计算后传入，服务端只做存储与向量检索。
"""
import os
import time
import uuid

import pyseekdb
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

DB_PATH = os.getenv("SEEKDB_PATH", "/root/stepfun-data")
COLL = os.getenv("COLLECTION_NAME", "stepfun_kb")
DIM = int(os.getenv("DIM", "384"))
HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.getenv("FRONTEND", os.path.join(HERE, "index.html"))

app = FastAPI(title="阶跃星辰·端侧向量检索")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_client = None


def coll():
    global _client
    if _client is None:
        _client = pyseekdb.Client(path=DB_PATH)
    return _client.get_or_create_collection(
        COLL,
        configuration=pyseekdb.HNSWConfiguration(dimension=DIM, distance="cosine"),
        embedding_function=None,
    )


@app.get("/")
def root():
    if os.path.isfile(FRONTEND):
        return FileResponse(FRONTEND)
    return {"ok": True, "service": "stepfun-embedded"}


@app.get("/api/stats")
def stats():
    return {"count": coll().count(), "collection": COLL, "dim": DIM}


@app.post("/api/add")
def add(req: dict):
    docs = req.get("documents") or []
    vecs = req.get("embeddings") or []
    if not docs or not vecs:
        return {"ok": False, "error": "documents and embeddings required"}
    if len(docs) != len(vecs):
        return {"ok": False, "error": "documents/embeddings length mismatch"}
    ids = req.get("ids") or [str(uuid.uuid4()) for _ in docs]
    metas = req.get("metadatas") or [{"content": d} for d in docs]
    coll().add(ids=ids, embeddings=vecs, documents=docs, metadatas=metas)
    return {"ok": True, "count": len(docs)}


@app.post("/api/search")
def search(req: dict):
    qv = req.get("queryEmbedding")
    if not qv:
        return {"ok": False, "error": "queryEmbedding required"}
    n = int(req.get("nResults", 5))
    t0 = time.time()
    try:
        res = coll().query(query_embeddings=[qv], n_results=n,
                           include=["documents", "metadatas", "distances"])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    ids = (res.get("ids") or [[]])[0]
    docs = (res.get("documents") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    results = [
        {"id": ids[i],
         "document": docs[i] if i < len(docs) else "",
         "distance": round(float(dists[i]), 4) if i < len(dists) else None,
         "metadata": metas[i] if i < len(metas) else None}
        for i in range(len(ids))
    ]
    return {"ok": True, "results": results, "latencyMs": round((time.time() - t0) * 1000, 1)}


@app.delete("/api/clear")
def clear():
    global _client
    if _client is not None:
        try:
            _client.delete_collection(COLL)
        except Exception:  # noqa: BLE001
            pass
    _client = None
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "3000")))

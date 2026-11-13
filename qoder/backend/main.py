"""
智研助手 - 后端服务
技术栈：FastAPI + pyseekdb(向量检索) + PowerContext(记忆) + MiniMax(LLM)
"""
import os
import uuid
import json
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer
import pyseekdb

# ---------- 配置 ----------
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")
MINIMAX_BASE_URL = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.chat/v1")
MINIMAX_MODEL = os.getenv("MINIMAX_MODEL", "MiniMax-M3.1-Flash")
POWERCONTEXT_BASE = os.getenv("POWERCONTEXT_BASE", "http://127.0.0.1:8001")
# seekdb 连接配置（RemoteServerClient，连接到 seekdb 服务器）
SEEKDB_HOST = os.getenv("SEEKDB_HOST", "127.0.0.1")
SEEKDB_PORT = int(os.getenv("SEEKDB_PORT", "2881"))
SEEKDB_USER = os.getenv("SEEKDB_USER", "root")
SEEKDB_PASSWORD = os.getenv("SEEKDB_PASSWORD", "")
SEEKDB_DATABASE = os.getenv("SEEKDB_DATABASE", "test")
COLLECTION = "zhiyan_docs"
DIM = 384

# ---------- 全局 ----------
embed_model = None
seekdb_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global embed_model, seekdb_client
    embed_model = SentenceTransformer("all-MiniLM-L6-v2")
    # 初始化 seekdb（RemoteServerClient，连接到 seekdb 服务器）
    seekdb_client = pyseekdb.Client(
        host=SEEKDB_HOST,
        port=SEEKDB_PORT,
        tenant="sys",
        database=SEEKDB_DATABASE,
        user=SEEKDB_USER,
        password=SEEKDB_PASSWORD,
    )
    # get_or_create_collection 默认 HNSW + cosine + 384d，embedding_function=None 表示手动传入向量
    seekdb_client.get_or_create_collection(COLLECTION, embedding_function=None)
    yield
    seekdb_client = None


app = FastAPI(title="智研助手", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="../frontend"), name="static")


# ---------- seekdb 辅助 ----------
def get_collection():
    return seekdb_client.get_or_create_collection(COLLECTION, embedding_function=None)


def embed(texts):
    vecs = embed_model.encode(texts, normalize_embeddings=True).tolist()
    return vecs


# ---------- PowerContext 辅助 ----------
def pc_post(path, payload):
    try:
        r = httpx.post(f"{POWERCONTEXT_BASE}{path}", json=payload, timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def pc_get(path):
    try:
        r = httpx.get(f"{POWERCONTEXT_BASE}{path}", timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


# ---------- 数据模型 ----------
class ImportReq(BaseModel):
    text: str
    title: str = ""
    tags: list[str] = []


class QueryReq(BaseModel):
    question: str
    user_id: str = "default"


# ---------- 接口 ----------
@app.post("/api/import")
def import_doc(req: ImportReq):
    """导入文档：切块 → 向量化 → 写入 seekdb"""
    chunks = [c.strip() for c in req.text.replace("\n", " ").split("。") if len(c.strip()) > 20]
    if not chunks:
        raise HTTPException(400, "文本过短，无法切块")
    vecs = embed(chunks)
    ids = [str(uuid.uuid4()) for _ in chunks]
    docs = chunks
    metadatas = [{"content": c, "title": req.title, "tags": req.tags} for c in chunks]

    coll = get_collection()
    coll.add(ids=ids, embeddings=vecs, documents=docs, metadatas=metadatas)
    return {"ok": True, "count": len(chunks)}


@app.post("/api/query")
def query(req: QueryReq):
    """检索 + LLM 回答，并记录到 PowerContext"""
    # 1. PowerContext: 读取用户偏好与历史
    memory = pc_get(f"/api/memory/{req.user_id}")
    user_prefs = ""
    if memory and isinstance(memory, dict) and memory.get("preferences"):
        user_prefs = f"\n用户偏好：{json.dumps(memory['preferences'], ensure_ascii=False)}"

    # 2. seekdb 语义检索
    q_vec = embed([req.question])[0]
    coll = get_collection()
    res = coll.query(query_embeddings=[q_vec], n_results=5, include=["documents", "metadatas"])

    # pyseekdb 返回 chromadb 兼容格式: {ids: [[...]], documents: [[...]], metadatas: [[...]], distances: [[...]]}
    ids = res.get("ids", [[]])[0]
    documents = res.get("documents", [[]])[0]
    metadatas = res.get("metadatas", [[]])[0]

    contexts = [f"[{i+1}] {metadatas[i].get('content', documents[i]) if i < len(metadatas) else ''}" for i in range(len(ids))]
    sources = [metadatas[i] if i < len(metadatas) else {"content": documents[i] if i < len(documents) else ""} for i in range(len(ids))]

    # 3. MiniMax 生成回答
    prompt = (
        "你是智研助手，基于检索到的资料回答用户问题。"
        "请引用资料编号，并给出简洁准确的回答。"
        f"{user_prefs}\n\n"
        f"用户问题：{req.question}\n\n"
        f"检索资料：\n" + "\n".join(contexts)
    )
    answer = call_minimax(prompt)

    # 4. PowerContext: 记录本次交互
    pc_post(f"/api/memory/{req.user_id}/append", {
        "question": req.question,
        "answer": answer,
        "timestamp": time.time()
    })

    return {"answer": answer, "sources": sources}


@app.get("/api/history/{user_id}")
def history(user_id: str):
    """PowerContext: 读取用户历史交互"""
    return pc_get(f"/api/memory/{user_id}")


@app.post("/api/preferences/{user_id}")
def set_preferences(user_id: str, prefs: dict):
    """PowerContext: 设置用户偏好"""
    return pc_post(f"/api/memory/{user_id}/preferences", prefs)


@app.get("/api/stats")
def stats():
    coll = get_collection()
    count = coll.count()
    return {"docs": count}


def call_minimax(prompt: str) -> str:
    if not MINIMAX_API_KEY:
        return "[MiniMax API Key 未配置，返回占位回答]"
    try:
        r = httpx.post(
            f"{MINIMAX_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {MINIMAX_API_KEY}"},
            json={"model": MINIMAX_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 1024},
            timeout=60,
        )
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[LLM 调用失败: {e}]"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

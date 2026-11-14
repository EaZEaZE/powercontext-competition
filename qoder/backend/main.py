"""
智研助手 - 后端服务
技术栈：FastAPI + pyseekdb(向量检索) + PowerContext(记忆) + MiniMax(LLM)
"""
import os
import uuid
import json
import math
import hashlib
import threading
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import pyseekdb

# ---------- 配置 ----------
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "")
MINIMAX_BASE_URL = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.chat/v1")
MINIMAX_MODEL = os.getenv("MINIMAX_MODEL", "MiniMax-M3.1-Flash")
POWERCONTEXT_BASE = os.getenv("POWERCONTEXT_BASE", "http://127.0.0.1:8001")
POWERCONTEXT_TOKEN = os.getenv("POWERCONTEXT_TOKEN", "")
POWERCONTEXT_TIMEOUT = float(os.getenv("POWERCONTEXT_TIMEOUT", "10"))
# PowerContext 的 Memory 条目需要标注类型，decision/constraint/next-step 均可
POWERCONTEXT_MEMORY_KIND = os.getenv("POWERCONTEXT_MEMORY_KIND", "decision")
# seekdb 连接配置：SEEKDB_EMBEDDED=1 走本地嵌入式，否则连接 seekdb 服务器（RemoteServerClient）
SEEKDB_EMBEDDED = os.getenv("SEEKDB_EMBEDDED", "") == "1"
SEEKDB_PATH = os.getenv("SEEKDB_PATH", "../seekdb-data")
SEEKDB_HOST = os.getenv("SEEKDB_HOST", "127.0.0.1")
SEEKDB_PORT = int(os.getenv("SEEKDB_PORT", "2881"))
SEEKDB_USER = os.getenv("SEEKDB_USER", "root")
SEEKDB_PASSWORD = os.getenv("SEEKDB_PASSWORD", "")
SEEKDB_DATABASE = os.getenv("SEEKDB_DATABASE", "test")
COLLECTION = "zhiyan_docs"
DIM = 384
# minilm：SentenceTransformer 语义向量（正式使用）；hash：字符 n-gram 哈希，仅无网络冒烟
EMBEDDING_BACKEND = os.getenv("EMBEDDING_BACKEND", "minilm")
if EMBEDDING_BACKEND not in ("minilm", "hash"):
    raise ValueError(f"EMBEDDING_BACKEND 只支持 minilm / hash，当前为 {EMBEDDING_BACKEND!r}")

# ---------- 全局 ----------
embed_model = None
seekdb_client = None
_model_lock = threading.Lock()


def new_seekdb_client():
    if SEEKDB_EMBEDDED:
        # 嵌入式：数据写入本地目录，无需启动 seekdb 服务器
        return pyseekdb.Client(path=SEEKDB_PATH)
    return pyseekdb.Client(
        host=SEEKDB_HOST,
        port=SEEKDB_PORT,
        tenant="sys",
        database=SEEKDB_DATABASE,
        user=SEEKDB_USER,
        password=SEEKDB_PASSWORD,
    )


def collection_kwargs():
    """embedding_function=None 表示手动传入向量；嵌入式模式必须显式声明索引配置"""
    if SEEKDB_EMBEDDED:
        return {
            "configuration": pyseekdb.HNSWConfiguration(dimension=DIM, distance="cosine"),
            "embedding_function": None,
        }
    return {"embedding_function": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global seekdb_client
    seekdb_client = new_seekdb_client()
    get_collection()
    yield
    seekdb_client = None


app = FastAPI(title="智研助手", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="../frontend"), name="static")


# ---------- seekdb 辅助 ----------
def get_collection():
    return seekdb_client.get_or_create_collection(COLLECTION, **collection_kwargs())


def hash_embed(text):
    """字符 2/3-gram 哈希进 384 维再归一化：只会让字面相近的文本相似，没有语义，供无网络冒烟用"""
    vec = [0.0] * DIM
    for n in (2, 3):
        for i in range(len(text) - n + 1):
            digest = hashlib.blake2b(text[i:i + n].encode("utf-8"), digest_size=8).digest()
            vec[int.from_bytes(digest, "big") % DIM] += 1.0
    magnitude = math.sqrt(sum(v * v for v in vec))
    return [v / magnitude for v in vec] if magnitude else vec


def embed(texts):
    if EMBEDDING_BACKEND == "hash":
        return [hash_embed(t) for t in texts]
    # 懒加载：包和权重都推迟到第一次真正要向量时才加载，hash 模式因此不用装 sentence-transformers / torch
    global embed_model
    if embed_model is None:
        with _model_lock:
            if embed_model is None:
                from sentence_transformers import SentenceTransformer
                embed_model = SentenceTransformer("all-MiniLM-L6-v2")
    vecs = embed_model.encode(texts, normalize_embeddings=True).tolist()
    return vecs


# ---------- PowerContext 辅助 ----------
# PowerContext 对外只暴露 /v1/* 命名空间，鉴权用 Authorization: Bearer <token>
_scope_ids: dict[str, str] = {}


def _pc_request(method, path, *, payload=None, headers=None):
    """返回 (json_body, response_headers)；失败时 json_body 为 {"error": ..., "detail": ...}"""
    req_headers = {"Content-Type": "application/json"}
    if POWERCONTEXT_TOKEN:
        req_headers["Authorization"] = f"Bearer {POWERCONTEXT_TOKEN}"
    if headers:
        req_headers.update(headers)
    try:
        r = httpx.request(
            method,
            f"{POWERCONTEXT_BASE}{path}",
            json=payload,
            headers=req_headers,
            timeout=POWERCONTEXT_TIMEOUT,
        )
        r.raise_for_status()
        return (r.json() if r.content else {}), r.headers
    except httpx.HTTPStatusError as e:
        return {"error": f"PowerContext {method} {path} 返回 {e.response.status_code}",
                "detail": e.response.text[:500]}, e.response.headers
    except Exception as e:
        return {"error": f"PowerContext {method} {path} 调用失败: {e}"}, {}


def pc_post(path, payload):
    body, _ = _pc_request("POST", path, payload=payload)
    return body


def pc_get(path, *, with_meta=False):
    body, headers = _pc_request("GET", path)
    return (body, headers) if with_meta else body


def pc_scope_id(user_id: str):
    """PowerContext 按项目 Scope 组织上下文，这里把 user_id 映射成一个幂等创建的 Scope；失败返回 None"""
    handle = f"zhiyan-{user_id}"
    if handle in _scope_ids:
        return _scope_ids[handle]
    body, _ = _pc_request("POST", "/v1/scopes", payload={
        "title": f"智研助手 - {user_id}",
        "summary": "智研助手的用户问答上下文",
        "idempotency_key": handle,
    })
    scope_id = body.get("scope_id")
    if scope_id:
        _scope_ids[handle] = scope_id
    return scope_id or None


def pc_items(body, *keys):
    """列表类接口的集合字段名不统一，按候选键位取第一个列表"""
    if not isinstance(body, dict):
        return []
    for key in keys or ("results", "hits", "items", "entries", "records"):
        value = body.get(key)
        if isinstance(value, list):
            return value
    return []


def pc_memory_text(item) -> str:
    """把一条 Memory（原子记忆 body / 兼容层条目）取成正文文本"""
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return ""
    for key in ("memory", "artifact"):
        if key in item:
            nested = pc_memory_text(item[key])
            if nested:
                return nested
    content = item.get("content")
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    if isinstance(content, str):
        return content
    return str(item.get("text") or "")


MEM_QUESTION_MARK = "问："
MEM_ANSWER_MARK = "答："

PROFILE_PATH = "/v1/scopes/{}/artifacts/profile/profile"


def pc_profile(scope_id):
    """读取 Scope 的 Profile Artifact（固定地址 family=profile, artifact_id=profile），返回 (正文, ETag)"""
    if not scope_id:
        return "", None
    body, headers = pc_get(PROFILE_PATH.format(quote(scope_id, safe="")), with_meta=True)
    if "error" in body:
        return "", None
    content = body.get("content")
    if isinstance(content, dict):
        content = content.get("content")
    return str(content or ""), headers.get("etag") or body.get("etag")


def pc_time_field(entry):
    """取 PowerContext 条目自带的时间字段；取不到就返回 None，绝不用当前时间伪造"""
    for source in (entry, entry.get("content"), entry.get("memory")):
        if not isinstance(source, dict):
            continue
        for key in ("created_at", "updated_at"):
            if source.get(key):
                return key, source[key]
    return None


def with_memory_flag(payload, scope_id):
    """Scope 创建失败时，只在给用户的返回里标注记忆能力不可用"""
    if not scope_id:
        payload["memory"] = "unavailable"
    return payload


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
    # seekdb 的向量索引是异步构建的，不 refresh 的话 count() 能看到新行，检索却查不到
    coll.refresh_index()
    return {"ok": True, "count": len(chunks)}


@app.delete("/api/clear")
def clear_docs():
    """清空 zhiyan_docs：删完必须重新 get_or_create 拿新句柄，旧句柄随集合失效，复用会让检索静默返回空；重建后同样要刷新索引"""
    try:
        seekdb_client.delete_collection(COLLECTION)
        coll = get_collection()
        coll.refresh_index()
        return {"ok": True, "docs": coll.count()}
    except Exception as e:
        return {"ok": False, "error": f"清空知识库失败: {e}"}


@app.post("/api/query")
def query(req: QueryReq):
    """检索 + LLM 回答，并把本次交互写回 PowerContext"""
    scope_id = pc_scope_id(req.user_id)

    # 1. PowerContext: 提问前读偏好（Profile Artifact）并召回相关记忆，Scope 不可用时跳过
    prefs, _ = pc_profile(scope_id)
    memories = []
    if scope_id:
        recalled = pc_post("/v1/memory/search", {
            "scope_id": scope_id,
            "query": req.question,
            "limit": 5,
        })
        memories = [t for t in (pc_memory_text(i) for i in pc_items(recalled, "results", "hits", "items", "entries")) if t]

    context_lines = []
    if prefs:
        context_lines.append(f"用户偏好：\n{prefs}")
    if memories:
        context_lines.append("相关历史记忆：\n" + "\n".join(f"- {m}" for m in memories))
    pc_context = ("\n" + "\n".join(context_lines)) if context_lines else ""

    # 2. seekdb 语义检索
    try:
        q_vec = embed([req.question])[0]
        coll = get_collection()
        res = coll.query(query_embeddings=[q_vec], n_results=5, include=["documents", "metadatas"])
    except Exception as e:
        return with_memory_flag({"answer": f"向量检索失败，暂时无法回答：{e}", "sources": []}, scope_id)

    # pyseekdb 返回 chromadb 兼容格式: {ids: [[...]], documents: [[...]], metadatas: [[...]], distances: [[...]]}
    ids = res.get("ids", [[]])[0]
    documents = res.get("documents", [[]])[0]
    metadatas = res.get("metadatas", [[]])[0]

    if not ids:
        return with_memory_flag({"answer": "知识库中暂未找到相关内容，请先导入资料。", "sources": []}, scope_id)

    contexts = [f"[{i+1}] {metadatas[i].get('content', documents[i]) if i < len(metadatas) else ''}" for i in range(len(ids))]
    sources = [metadatas[i] if i < len(metadatas) else {"content": documents[i] if i < len(documents) else ""} for i in range(len(ids))]

    # 3. MiniMax 生成回答
    prompt = (
        "你是智研助手，基于检索到的资料回答用户问题。"
        "请引用资料编号，并给出简洁准确的回答。"
        f"{pc_context}\n\n"
        f"用户问题：{req.question}\n\n"
        f"检索资料：\n" + "\n".join(contexts)
    )
    answer = call_minimax(prompt)

    # 4. PowerContext: 回答后写入 Memory
    if scope_id:
        pc_post("/v1/memory/remember", {
            "scope_id": scope_id,
            "kind": POWERCONTEXT_MEMORY_KIND,
            "text": f"{MEM_QUESTION_MARK}{req.question}\n{MEM_ANSWER_MARK}{answer}",
            "reason": "智研助手问答记录",
        })

    return with_memory_flag({"answer": answer, "sources": sources}, scope_id)


@app.get("/api/history/{user_id}")
def history(user_id: str):
    """PowerContext: 列出该用户 Scope 的记忆条目，还原成前端需要的问答历史"""
    scope_id = pc_scope_id(user_id)
    if not scope_id:
        return {"history": [], "memory": "unavailable"}
    body = pc_post("/v1/memory/entries/list", {"scope_id": scope_id, "limit": 50})
    if "error" in body:
        return {"history": [], "error": body["error"]}

    items = []
    for entry in pc_items(body, "items", "entries", "records", "results"):
        text = pc_memory_text(entry)
        if not text:
            continue
        question, _, answer = text.partition(MEM_ANSWER_MARK)
        item = {
            "question": question.removeprefix(MEM_QUESTION_MARK).strip(),
            "answer": answer.strip(),
            "text": text,
        }
        found = pc_time_field(entry)
        if found:
            item[found[0]] = found[1]
        items.append(item)
    return {"history": items}


@app.post("/api/preferences/{user_id}")
def set_preferences(user_id: str, prefs: dict):
    """PowerContext: 把偏好写进 Scope 的 Profile Artifact（正文是 Markdown）"""
    scope_id = pc_scope_id(user_id)
    if not scope_id:
        return {"error": "PowerContext Scope 创建失败，偏好未保存", "memory": "unavailable"}
    content = "# 用户偏好\n\n```json\n" + json.dumps(prefs, ensure_ascii=False, indent=2) + "\n```\n"
    path = PROFILE_PATH.format(quote(scope_id, safe=""))

    _, etag = pc_profile(scope_id)
    if etag:
        body, _ = _pc_request("PUT", path, payload={
            "content": {"content": content},
            "reason": "智研助手更新用户偏好",
        }, headers={"If-Match": etag})
        return body
    # Profile 还不存在时先创建，已有则必须带 If-Match 修订
    return pc_post(f"/v1/scopes/{quote(scope_id, safe='')}/artifacts", {
        "family": "profile",
        "content": {"content": content},
    })


@app.get("/api/stats")
def stats():
    coll = get_collection()
    count = coll.count()
    return {"docs": count}


def call_minimax(prompt: str) -> str:
    if not MINIMAX_API_KEY:
        return "大模型未配置（MINIMAX_API_KEY 为空），暂时无法生成回答。"
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
        return f"大模型调用失败，暂时无法生成回答：{e}"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

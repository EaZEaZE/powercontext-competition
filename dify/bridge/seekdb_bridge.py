"""
Dify → seekdb 执行桥接服务

Dify 没有内置的 MySQL 查询节点，用本服务把生成的 SQL 落到 seekdb 执行，
再把结果回传给 Dify 的 HTTP 请求节点。

启动：
    pip install -r requirements.txt
    SEEKDB_HOST=127.0.0.1 SEEKDB_PORT=2881 SEEKDB_PASSWORD= uvicorn seekdb_bridge:app --host 0.0.0.0 --port 18080
"""
import os

import pymysql
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

SEEKDB_HOST = os.getenv("SEEKDB_HOST", "127.0.0.1")
SEEKDB_PORT = int(os.getenv("SEEKDB_PORT", "2881"))
SEEKDB_USER = os.getenv("SEEKDB_USER", "root")
SEEKDB_PASSWORD = os.getenv("SEEKDB_PASSWORD", "")
SEEKDB_DATABASE = os.getenv("SEEKDB_DATABASE", "test")

READONLY_KEYWORDS = {"SELECT", "SHOW", "DESC", "DESCRIBE", "WITH", "EXPLAIN"}

app = FastAPI(title="seekdb bridge")


class SqlReq(BaseModel):
    sql: str


def connect():
    return pymysql.connect(
        host=SEEKDB_HOST,
        port=SEEKDB_PORT,
        user=SEEKDB_USER,
        password=SEEKDB_PASSWORD,
        database=SEEKDB_DATABASE,
        charset="utf8mb4",
        connect_timeout=5,
        read_timeout=30,
    )


@app.get("/health")
def health():
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return {"ok": True, "seekdb": f"{SEEKDB_HOST}:{SEEKDB_PORT}"}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


@app.post("/sql")
def run_sql(req: SqlReq):
    sql = (req.sql or "").strip().rstrip(";")
    first = sql.split()[0].upper() if sql.split() else ""
    if first not in READONLY_KEYWORDS:
        # 只允许只读语句，DDL/DML 由用户确认后手动执行
        return {"ok": False, "message": f"仅允许只读语句，当前语句以 {first or '空'} 开头", "columns": [], "rows": []}
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                columns = [d[0] for d in cur.description] if cur.description else []
                rows = [list(map(str, r)) for r in cur.fetchmany(200)] if columns else []
        return {"ok": True, "columns": columns, "rows": rows, "row_count": len(rows)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e), "columns": [], "rows": []}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "18080")))

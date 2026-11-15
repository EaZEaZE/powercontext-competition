"""Dify 到嵌入式 seekdb 的 SQL 执行桥接。

启动：
    pip install -r requirements.txt
    SEEKDB_DIR=./seekdb-data python seekdb_bridge.py
"""
import os
import threading

import pylibseekdb
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

SEEKDB_DIR = os.getenv("SEEKDB_DIR", "./seekdb-data")
SEEKDB_DATABASE = os.getenv("SEEKDB_DATABASE", "test")
QUERY_KEYWORDS = {"SELECT", "SHOW", "DESC", "DESCRIBE", "WITH", "EXPLAIN"}
ALLOWED_KEYWORDS = QUERY_KEYWORDS | {"CREATE"}

app = FastAPI(title="seekdb bridge")
_instance = pylibseekdb.open(SEEKDB_DIR)
_db_lock = threading.Lock()


class SqlReq(BaseModel):
    sql: str


def execute(sql: str):
    first = sql.split()[0].upper() if sql.split() else ""
    with _db_lock:
        conn = _instance.connect(database=SEEKDB_DATABASE, autocommit=True)
        cur = conn.cursor()
        try:
            cur.execute(sql)
            rows = cur.fetchall()[:200] if first in QUERY_KEYWORDS else []
        finally:
            cur.close()
            conn.close()
    return rows


@app.get("/health")
def health():
    try:
        rows = execute("SELECT 1")
        return {
            "ok": rows == [(1,)],
            "mode": "embedded",
            "seekdb_dir": SEEKDB_DIR,
            "database": SEEKDB_DATABASE,
        }
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


@app.post("/sql")
def run_sql(req: SqlReq):
    sql = (req.sql or "").strip().rstrip(";")
    first = sql.split()[0].upper() if sql.split() else ""
    if first not in ALLOWED_KEYWORDS:
        return {
            "ok": False,
            "message": f"不允许执行以 {first or '空'} 开头的语句",
            "columns": [],
            "rows": [],
        }
    try:
        rows = execute(sql)
        return {
            "ok": True,
            "mode": "embedded",
            "columns": [],
            "rows": [list(map(str, row)) for row in rows],
            "row_count": len(rows),
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e), "columns": [], "rows": []}


@app.on_event("shutdown")
def shutdown():
    if not _instance.closed:
        _instance.close()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "18080")))

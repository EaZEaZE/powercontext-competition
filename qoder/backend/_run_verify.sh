#!/bin/bash
# 在 Linux 实例上启动智研助手后端（hash 向量 + 嵌入式 seekdb）并跑 verify.py
cd /root/zhiyan/backend || exit 1

export SEEKDB_EMBEDDED=1
export EMBEDDING_BACKEND=hash
export SEEKDB_PATH=/root/zhiyan/seekdb-data
export POWERCONTEXT_BASE=http://127.0.0.1:8001
export PORT=8000
export PYTHONUNBUFFERED=1

python3 -c "import pyseekdb, fastapi, httpx; print('[deps] ok')"

pkill -f "python3 main.py" 2>/dev/null
sleep 1
nohup python3 main.py > /root/zhiyan/server.log 2>&1 &
echo "[start] pid=$!"
sleep 15

echo "=== /api/stats ==="
curl -s -m 10 http://127.0.0.1:8000/api/stats
echo
echo "=== verify.py ==="
python3 verify.py
echo "EXIT=$?"

echo "=== server.log (tail) ==="
tail -15 /root/zhiyan/server.log

#!/usr/bin/env bash
# Dify × OceanBase seekdb 一键部署脚本
# 前置条件：已安装 Docker & Docker Compose
set -e

echo "=== Dify × OceanBase seekdb 部署 ==="

# 1. 克隆 Dify（如果尚未克隆）
if [ ! -d "dify-src" ]; then
    echo "[1/5] 克隆 Dify 源码..."
    git clone --depth 1 -b v1.10.1 https://github.com/langgenius/dify.git dify-src
else
    echo "[1/5] Dify 源码已存在，跳过克隆"
fi

cd dify-src/docker

# 2. 复制 seekdb 配置
echo "[2/5] 配置 seekdb 为元数据库+向量数据库..."
cp ../../.env .env

# 3. 验证配置
echo "[3/5] 验证配置..."
echo "DB_TYPE=$(grep '^DB_TYPE=' .env | cut -d= -f2)"
echo "VECTOR_STORE=$(grep '^VECTOR_STORE=' .env | cut -d= -f2)"
echo "COMPOSE_PROFILES=$(grep '^COMPOSE_PROFILES=' .env | cut -d= -f2)"

# 4. 启动服务
echo "[4/5] 启动 Dify + seekdb..."
docker compose up -d

echo ""
echo "[5/5] 等待服务启动（约 1-2 分钟）..."
echo "查看迁移日志：docker logs -f docker-api-1"
echo "看到 'Database migration successful!' 即表示成功"
echo ""
echo "=== 部署完成 ==="
echo "Dify 控制台: http://localhost"
echo "初始化管理员: http://localhost/install"
echo ""
echo "验证 seekdb 连接:"
echo "  docker exec -it docker-seekdb-1 seekdb-client -h127.0.0.1 -P2881 -uroot -e 'SHOW DATABASES;'"

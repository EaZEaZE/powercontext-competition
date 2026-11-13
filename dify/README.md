# Dify × OceanBase seekdb 部署

## 赛题要求

部署 Dify，并配置 OceanBase seekdb 同时作为**元数据库（MetaDB）**和**向量数据库（VectorStore）**，构建一个 AI 知识库问答应用。

## 部署方案

### 方案一：Docker Compose（推荐）

前置条件：已安装 Docker & Docker Compose

```bash
# 1. 克隆 Dify v1.10.1+
git clone --depth 1 -b v1.10.1 https://github.com/langgenius/dify.git
cd dify/docker

# 2. 复制配置（seekdb 同时作为 MetaDB + VectorStore）
cp ../../.env .env   # 使用本目录的 .env

# 3. 启动
docker compose up -d

# 4. 等待迁移完成（约 1-2 分钟）
docker logs -f docker-api-1  # 看到 "Database migration successful!" 即可
```

### 关键配置（.env）

```env
# MetaDB: seekdb (MySQL 协议)
DB_TYPE=mysql
DB_USERNAME=root
DB_PASSWORD=
DB_HOST=seekdb
DB_PORT=2881
DB_DATABASE=test

# Vector Store: OceanBase seekdb
VECTOR_STORE=oceanbase
OCEANBASE_VECTOR_HOST=seekdb
OCEANBASE_VECTOR_PORT=2881
OCEANBASE_VECTOR_USER=root
OCEANBASE_VECTOR_PASSWORD=
OCEANBASE_VECTOR_DATABASE=test

# 只启用 seekdb profile
COMPOSE_PROFILES=seekdb
```

## 验证步骤

1. 访问 `http://<host>/install` 创建管理员账号
2. 配置模型 API Key（设置 → 模型供应商）
3. 创建知识库（知识库 → 创建知识库 → 上传文档 → 高质量索引）
4. 创建对话应用（工作室 → 创建应用 → 对话型）
5. 在应用中关联知识库
6. 发布应用，获取公开访问链接

## 提交内容

- Dify 应用的公开访问链接
- 本目录的配置文件（.env）

## 备选方案

如果当前实例无法运行 Docker（iptables/bridge 权限受限），可：
1. 在本地机器（Windows/Mac + Docker Desktop）部署
2. 使用支持 Docker 的云服务器
3. 或在比赛平台申请 Docker 实例
